"""Headless, instrumented simulation runner for analysis.

This is the analysis counterpart to the live server: it wires the *same*
navigator + controller + simulator + simulated devices into a deterministic,
hardware-free closed loop, steps it forward, and records a full time series of
both ground truth and what the controller *perceived* every physics tick.

The result -- a :class:`SimLog` -- is what :mod:`vanchor.analysis.metrics` and
:mod:`vanchor.analysis.report` turn into numbers and pictures. Scenarios are
plain data (start, environment, timed commands, optional gain overrides) so
experiments and tuning sweeps are easy to express and reproduce.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields, replace

from ..controller.controller import Controller, Helm
from ..controller.modes import AnchorConfig, DriftConfig, WaypointConfig
from ..controller.safety import SafetyConfig
from ..core.geo import haversine_m
from ..core.models import BoatState, Environment, GeoPoint
from ..core.pid import PID
from ..core.state import NavigationState
from ..nav.navigator import Navigator
from ..sim.devices import SimCompass, SimGps
from ..sim.fossen import FossenParams
from ..sim.simulator import Simulator

NAN = float("nan")


@dataclass(frozen=True)
class Command:
    """A controller/sim command issued at a given simulated time."""

    t: float
    command: dict


@dataclass
class Scenario:
    """A fully-specified, reproducible simulation experiment."""

    name: str
    start: GeoPoint = field(default_factory=lambda: GeoPoint(59.66275, 13.32247))
    environment: Environment = field(default_factory=Environment)
    model: str = "fossen"
    duration_s: float = 180.0
    physics_dt: float = 0.05
    gps_hz: float = 1.0
    compass_hz: float = 5.0
    control_hz: float = 5.0
    commands: list[Command] = field(default_factory=list)
    # Optional control-loop overrides, so a tuning sweep is one field change.
    anchor_config: AnchorConfig | None = None
    waypoint_config: WaypointConfig | None = None
    helm_pid: PID | None = None
    cruise_pid: PID | None = None
    drift_config: DriftConfig | None = None
    max_steer_angle_deg: float = 185.0

    # --- Vectored / wide-azimuth station-keeping (#35) support ------------ #
    # All OFF/None by default, so every existing scenario builds the exact same
    # SIM boat + controller as before. Set these to let the analysis harness
    # exercise and score the opt-in vectored anchor hold, which needs the motor's
    # full mechanical swing (the default sim boat only swings +/-35 deg, so a
    # Scenario otherwise cannot command a wide azimuth).
    #
    # ``sim_max_steer_angle_deg``: physical head swing (deg) the SIM boat's motor
    # can rotate to. None keeps the model default (~35 deg). When set, the helm's
    # autopilot band is re-scaled to its tuned 35 deg and the steering slew is
    # held to a physical ~50 deg/s, exactly as the live app wires a wide head --
    # so the +/-35 baseline is unchanged and only the vectored law reaches wider.
    sim_max_steer_angle_deg: float | None = None
    station_keep_vectored: bool = False        # enable the vectored anchor law
    station_keep_azimuth_deg: float = 35.0     # vectored azimuth authority (deg)
    # Sim thruster longitudinal arm (+ bow, - stern). None = model default (bow).
    # Its sign drives the helm/anchor ``steer_sign`` (stern mounts steer/yaw the
    # opposite way), so a stern-mount hold can be scored too.
    thruster_x_m: float | None = None


@dataclass
class Sample:
    """One recorded instant of the closed loop."""

    t: float
    mode: str
    # Ground truth (what the boat actually did).
    truth_lat: float
    truth_lon: float
    truth_heading: float
    truth_speed_mps: float
    # Perceived (what the controller saw from noisy GPS/compass).
    perc_lat: float
    perc_lon: float
    perc_heading: float
    sog_knots: float
    # Command + diagnostics.
    thrust: float
    steering: float
    steer_angle_deg: float
    target_heading: float
    dist_anchor_truth_m: float
    dist_anchor_perc_m: float
    cross_track_m: float
    dist_waypoint_m: float
    anchor_radius_m: float

    def row(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


class SimLog:
    """The recorded time series of a scenario, with convenience accessors."""

    def __init__(self, scenario: Scenario, samples: list[Sample]) -> None:
        self.scenario = scenario
        self.samples = samples

    def __len__(self) -> int:
        return len(self.samples)

    def times(self) -> list[float]:
        return [s.t for s in self.samples]

    def series(self, key: str) -> list[float]:
        return [getattr(s, key) for s in self.samples]

    def tail(self, seconds: float) -> "SimLog":
        if not self.samples:
            return SimLog(self.scenario, [])
        cutoff = self.samples[-1].t - seconds
        return SimLog(self.scenario, [s for s in self.samples if s.t >= cutoff])

    def since(self, t0: float) -> "SimLog":
        return SimLog(self.scenario, [s for s in self.samples if s.t >= t0])


def _dispatch(runtime_state, sim: Simulator, controller: Controller, command: dict) -> None:
    """Route a command the way the live Runtime does (sim-only vs controller)."""
    ctype = command.get("type")
    if ctype == "set_environment":
        env = sim.environment
        for key in ("current_speed", "current_dir", "wind_speed", "wind_dir"):
            if key in command:
                setattr(env, key, float(command[key]))
    elif ctype == "teleport":
        sim.boat.state.point = GeoPoint(float(command["lat"]), float(command["lon"]))
    else:
        controller.handle_command(command)


def run_scenario(scenario: Scenario) -> SimLog:
    """Run a scenario deterministically and return its recorded :class:`SimLog`."""
    # Wide-azimuth / vectored station-keeping (#35): when the scenario asks for a
    # physical head swing, build the SIM boat with that swing (+ optional stern
    # mount) and a matching helm/safety/anchor config -- mirroring how the live
    # app (app.py) wires a wide head -- so the harness can score the vectored law
    # against the +/-35 baseline. Left None, everything below is inert and the
    # boat + controller are byte-identical to before.
    sim_params: FossenParams | None = None
    helm = Helm(scenario.helm_pid) if scenario.helm_pid else None
    safety_config: SafetyConfig | None = None
    anchor_config = scenario.anchor_config
    state_max_steer = scenario.max_steer_angle_deg
    if scenario.sim_max_steer_angle_deg is not None:
        swing = scenario.sim_max_steer_angle_deg
        x = scenario.thruster_x_m
        sign = 1.0 if (x is None or x >= 0.0) else -1.0
        fp_kwargs: dict = {"max_steer_angle_deg": swing}
        if x is not None:
            fp_kwargs["thruster_x_m"] = x
        sim_params = FossenParams(**fp_kwargs)
        # The controller must know the same physical swing the sim maps commands
        # onto (the vectored law normalizes its azimuth by it).
        state_max_steer = swing
        # Keep the autopilot band at its tuned 35 deg and the slew at ~50 deg/s
        # of PHYSICAL rotation even though the head can now swing far wider, and
        # flip the steer sign for a stern mount.
        helm = Helm(
            scenario.helm_pid,
            autopilot_steer_scale=35.0 / swing if swing > 0 else 1.0,
            steer_sign=sign,
        )
        safety_config = SafetyConfig(
            max_steer_slew_per_s=50.0 / swing if swing > 0 else 1.4
        )
        # Overlay the vectored knobs onto whatever anchor config the scenario set
        # (the mount sign is mirrored so the physical azimuth survives the helm).
        anchor_config = replace(
            anchor_config or AnchorConfig(),
            vectored=scenario.station_keep_vectored,
            vector_azimuth_deg=scenario.station_keep_azimuth_deg,
            steer_sign=sign,
        )

    sim = Simulator(
        start=BoatState(point=scenario.start, heading_deg=0.0),
        params=sim_params,
        environment=scenario.environment,
        model=scenario.model,
    )
    state = NavigationState()
    state.max_steer_angle_deg = state_max_steer
    nav = Navigator(state, bus=None)
    controller = Controller(
        state,
        sim.motor,
        bus=None,
        tick_hz=scenario.control_hz,
        helm=helm,
        anchor_config=anchor_config,
        waypoint_config=scenario.waypoint_config,
        drift_config=scenario.drift_config,
        safety_config=safety_config,
        cruise_pid=scenario.cruise_pid,
    )
    gps = SimGps(sim.truth, bus=None, update_hz=scenario.gps_hz)
    compass = SimCompass(sim.truth, bus=None, update_hz=scenario.compass_hz)

    # Prime perceived state with one fix + heading.
    nav.handle_sentence(gps.sample(sim.truth()))
    nav.handle_sentence(compass.sample(sim.truth()))

    pending = sorted(scenario.commands, key=lambda c: c.t)
    dt = scenario.physics_dt
    gps_period = 1.0 / scenario.gps_hz
    compass_period = 1.0 / scenario.compass_hz
    ctrl_period = 1.0 / scenario.control_hz

    t = 0.0
    next_gps = next_compass = next_ctrl = 0.0
    samples: list[Sample] = []

    while t < scenario.duration_s:
        # Fire any commands due at or before now.
        while pending and pending[0].t <= t:
            _dispatch(state, sim, controller, pending.pop(0).command)

        sim.step(dt)
        if t >= next_gps:
            nav.handle_sentence(gps.sample(sim.truth()))
            next_gps += gps_period
        if t >= next_compass:
            nav.handle_sentence(compass.sample(sim.truth()))
            next_compass += compass_period
        if t >= next_ctrl:
            controller.control_tick(ctrl_period)
            next_ctrl += ctrl_period

        samples.append(_record(t, state, sim))
        t += dt

    return SimLog(scenario, samples)


def _record(t: float, state: NavigationState, sim: Simulator) -> Sample:
    truth = sim.truth()
    perc = state.position
    anchor = state.anchor
    dist_truth = haversine_m(truth.point, anchor) if anchor else NAN
    cmd = state.motor_command
    return Sample(
        t=round(t, 4),
        mode=state.mode.value,
        truth_lat=truth.point.lat,
        truth_lon=truth.point.lon,
        truth_heading=truth.heading_deg,
        truth_speed_mps=truth.speed_mps,
        perc_lat=perc.lat if perc else NAN,
        perc_lon=perc.lon if perc else NAN,
        perc_heading=state.heading_deg,
        sog_knots=state.sog_knots,
        thrust=cmd.thrust,
        steering=cmd.steering,
        steer_angle_deg=cmd.steering * state.max_steer_angle_deg
        + (180.0 if cmd.thrust < 0 else 0.0),
        target_heading=state.target_heading,
        dist_anchor_truth_m=dist_truth,
        dist_anchor_perc_m=state.distance_to_anchor_m,
        cross_track_m=state.cross_track_m,
        dist_waypoint_m=state.distance_to_waypoint_m,
        anchor_radius_m=state.anchor_radius_m,
    )
