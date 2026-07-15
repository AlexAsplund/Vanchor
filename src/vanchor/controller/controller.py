"""The controller: the heart of the event-driven system.

Responsibilities:
  * Own the active control mode and the steering helm.
  * On every control tick, ask the mode for a setpoint, turn it into a concrete
    :class:`MotorCommand` via the helm, and hand it to the motor controller.
  * Translate inbound commands (from the UI/bus) into mode/state changes.

The control logic is exposed as a synchronous ``control_tick(dt)`` so it can be
driven deterministically by tests, and wrapped by an async ``run`` loop for the
live system.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import cast

from ..core import events
from ..core.capabilities import DEVICE_LABEL, missing_devices
from ..core.config import SafetyFloor
from ..core.events import EventBus
from ..core.geo import angle_difference, destination_point, normalize_deg
from ..core.models import (
    ControlModeName,
    GeoPoint,
    GuidedSetpoint,
    ManualSetpoint,
    MotorCommand,
    Setpoint,
    Waypoint,
)
from ..core.pid import PID
from ..core.state import NavigationState
from ..hardware.interfaces import MotorController
from ..nav.track import TrackRecorder
from .estimator import EstimatorConfig, WindCurrentEstimator
from .modes import (
    AnchorConfig,
    AnchorHoldMode,
    ContourConfig,
    ContourFollowMode,
    ControlMode,
    DriftConfig,
    DriftMode,
    FollowApbConfig,
    FollowApbMode,
    HeadingHoldMode,
    ManualMode,
    OrbitConfig,
    OrbitMode,
    TrollingConfig,
    TrollingMode,
    WaypointConfig,
    WaypointMode,
    WorkAreaConfig,
    WorkAreaMode,
)
from .safety import SafetyConfig, SafetyGovernor, SafetyStatus

logger = logging.getLogger("vanchor.controller")

# Modes whose throttle Cruise Control may take over (an underway speed hold makes
# no sense for manual or station-keeping).
_CRUISING_MODES = frozenset(
    {
        ControlModeName.HEADING_HOLD,
        ControlModeName.WAYPOINT,
        ControlModeName.FOLLOW_APB,
        ControlModeName.CONTOUR_FOLLOW,
        ControlModeName.ORBIT,
        ControlModeName.TROLLING,
    }
)

# Boat-relative jog directions -> heading offset (degrees).
_JOG_OFFSETS = {"forward": 0.0, "back": 180.0, "left": -90.0, "right": 90.0}


def _wp_speed(w: dict) -> tuple[float | None, float | None]:
    """Optional per-waypoint speed attributes from a goto waypoint dict:
    ``(throttle_pct, speed_kn)``. Engine-% wins if both are present; malformed
    or non-positive values are dropped (no speed change at that mark)."""
    tp: float | None
    kn: float | None
    try:
        raw = w.get("throttle_pct")
        tp = min(100.0, float(raw)) if raw is not None and float(raw) > 0.0 else None
    except (TypeError, ValueError):
        tp = None
    try:
        raw = w.get("speed_kn")
        kn = float(raw) if raw is not None and float(raw) > 0.0 else None
    except (TypeError, ValueError):
        kn = None
    return (tp, None) if tp is not None else (None, kn)

# Modes that actively station-keep on ``state.anchor`` (they refresh
# ``state.distance_to_anchor_m`` every tick) -- the hold quality metric is
# accumulated only while one of these is holding, so PID anchor-hold and the
# learned anchor_ml can be compared on the same yardstick.
_HOLD_MODES = frozenset({ControlModeName.ANCHOR_HOLD, ControlModeName.ANCHOR_ML,
                         ControlModeName.ANCHOR_LEIF})

# Command types that engage a control mode -> the mode they enter, so the
# device-availability gate can refuse one whose required device is Not connected.
_CTYPE_MODE = {
    "manual": ControlModeName.MANUAL,
    "anchor_hold": ControlModeName.ANCHOR_HOLD,
    "anchor_ml": ControlModeName.ANCHOR_ML,
    "anchor_leif": ControlModeName.ANCHOR_LEIF,
    "heading_hold": ControlModeName.HEADING_HOLD,
    "goto": ControlModeName.WAYPOINT,
    "load_route": ControlModeName.WAYPOINT,
    "work_area": ControlModeName.WORK_AREA,
    "follow_apb": ControlModeName.FOLLOW_APB,
    "drift": ControlModeName.DRIFT,
    "contour_follow": ControlModeName.CONTOUR_FOLLOW,
    "orbit": ControlModeName.ORBIT,
    "trolling": ControlModeName.TROLLING,
}


# Below this |thrust| the trolling motor has no meaningful steering authority
# (a prop that isn't turning can't vector the boat), so we stop moving the
# steering actuator to avoid jittering / straining the servo for no effect.
STEER_EPS = 0.03


@dataclass
class GainSchedule:
    """SOG-keyed multiplier on the helm's steering (proportional) gain (#31).

    A single steerable thruster vectors the boat with its prop wash, so steering
    AUTHORITY scales with thrust / boat speed. That makes one fixed heading ``kp``
    brittle across the speed range: slow => weak authority => the loop wants MORE
    gain; fast => strong authority => the same gain oscillates => wants LESS gain.
    So the physically-correct schedule has ``mult_lo >= mult_hi`` (more gain when
    slow, verified against how thrust-scaled steering authority works here -- see
    ``STEER_EPS`` above, where a barely-turning prop has no authority at all).

    ``multiplier(sog)`` is a linear interpolation between ``mult_lo`` (at/below
    ``sog_lo_kn``) and ``mult_hi`` (at/above ``sog_hi_kn``), held flat outside
    that band and clamped to ``[mult_min, mult_max]``. The default (all
    multipliers 1.0) is NEUTRAL: it returns exactly 1.0 for every SOG, so an
    unconfigured schedule leaves the tuned gain untouched.
    """

    sog_lo_kn: float = 0.3
    sog_hi_kn: float = 2.0
    mult_lo: float = 1.0
    mult_hi: float = 1.0
    mult_min: float = 0.1
    mult_max: float = 5.0

    def multiplier(self, sog_knots: float) -> float:
        lo, hi = self.sog_lo_kn, self.sog_hi_kn
        if hi <= lo or sog_knots <= lo:
            m = self.mult_lo
        elif sog_knots >= hi:
            m = self.mult_hi
        else:
            frac = (sog_knots - lo) / (hi - lo)
            m = self.mult_lo + frac * (self.mult_hi - self.mult_lo)
        return max(self.mult_min, min(self.mult_max, m))

    @property
    def is_neutral(self) -> bool:
        """True when the schedule leaves the base gain unchanged everywhere."""
        return self.mult_lo == 1.0 and self.mult_hi == 1.0


class HoldQuality:
    """Rolling anchor hold quality metric (#34): RMS radial error (m) and % of
    time within the anchor radius, over an exponentially-weighted trailing
    window of ``window_s`` seconds.

    Mode-agnostic: it is fed ``state.distance_to_anchor_m`` each control tick
    while ANY anchor mode is holding, so users can compare the PID hold and the
    learned hold on identical numbers. Cheap by construction -- two rolling
    accumulators (EMA of the squared error and of the in-radius indicator),
    no stored history. The first sample seeds the accumulators directly so the
    early readout tracks the truth instead of being diluted by a zero seed.
    """

    def __init__(self, window_s: float = 60.0) -> None:
        self.window_s = window_s
        self.reset()

    def reset(self) -> None:
        self._sq_m2 = 0.0     # EMA of (radial error)^2
        self._in = 0.0        # EMA of the in-radius indicator (0/1)
        self.elapsed_s = 0.0  # holding time accumulated (caps at window_s)
        self._seeded = False

    def update(self, distance_m: float, radius_m: float, dt: float) -> None:
        if dt <= 0.0:
            return
        inside = 1.0 if distance_m <= radius_m else 0.0
        if not self._seeded:
            self._sq_m2 = distance_m * distance_m
            self._in = inside
            self._seeded = True
        else:
            alpha = dt / (self.window_s + dt)
            self._sq_m2 += (distance_m * distance_m - self._sq_m2) * alpha
            self._in += (inside - self._in) * alpha
        self.elapsed_s = min(self.elapsed_s + dt, self.window_s)

    @property
    def rms_m(self) -> float:
        return math.sqrt(max(0.0, self._sq_m2))

    @property
    def pct_in_radius(self) -> float:
        return 100.0 * min(1.0, max(0.0, self._in))


class Helm:
    """Turns a heading intent into a steering command via one shared PID.

    This is the "autopilot" inner loop: every guided mode benefits from a single
    well-tuned heading controller instead of re-implementing steering.
    """

    def __init__(self, pid: PID | None = None, steer_tau: float = 0.6,
                 autopilot_steer_scale: float = 1.0, steer_sign: float = 1.0,
                 thrust_yaw_ff: float = 0.0,
                 gain_schedule: "GainSchedule | None" = None) -> None:
        self.pid = pid or PID(kp=0.035, ki=0.0, kd=0.012, output_min=-1.0, output_max=1.0)
        # Optional SOG-keyed schedule on the steering (proportional) gain (#31).
        # ``None`` (the default) leaves ``pid.kp`` as-is; a neutral schedule
        # (multiplier 1.0 everywhere) is numerically identical to that. The base
        # gain remains ``pid.kp`` -- the schedule only scales it transiently while
        # computing each tick's proportional term, so external code (auto-tune,
        # per-boat gains) keeps reading/writing ``pid.kp`` as the base.
        self.gain_schedule = gain_schedule
        # Last effective steering gain actually used (base * schedule multiplier),
        # exposed for telemetry/tests. Seeded from the base gain.
        self.kp_eff = self.pid.kp
        # First-order low-pass time constant (s) on the steering command. The
        # raw PID output chases ~1deg compass noise; without smoothing the motor
        # would slew back and forth constantly. ~0.6 s removes that jitter with
        # little control lag. Set to 0 to disable.
        self.steer_tau = steer_tau
        # The head can mechanically swing far (manual uses the full range), but
        # the autopilot only needs a fraction of it for course control. This
        # scales the guided (autopilot) steering so its *physical* deflection
        # stays at the tuned authority even when max_steer_angle_deg is large
        # (= autopilot_steer_deg / max_steer_angle_deg). Manual is unscaled.
        self.autopilot_steer_scale = autopilot_steer_scale
        # +1 for a bow-mounted thruster, -1 for stern. A stern thruster yaws the
        # boat the OPPOSITE way for the same steering deflection (the lever arm
        # sign flips), so without this the autopilot would steer backwards and
        # never converge. Derived from the boat's thruster mount and updated when
        # the boat profile changes; calibration can also set it from a measured
        # turn. Applied to BOTH manual and guided so a given command always turns
        # the boat the same way regardless of mount.
        self.steer_sign = 1.0 if steer_sign >= 0 else -1.0
        # Thrust-yaw feed-forward: a constant steering bias (in command units,
        # i.e. a fraction of the full mechanical swing) that pre-cancels the yaw
        # a laterally-offset thruster induces under straight thrust. Geometry sets
        # the magnitude; it is applied INSIDE ``steer_sign`` (so a stern mount,
        # whose deflection yaws the boat the opposite way, gets the opposite
        # physical deflection automatically) and only while making way. It is the
        # same sign in forward and reverse because both the bias and the steering
        # term scale with thrust, so the cancelling geometry is thrust-independent.
        self.thrust_yaw_ff = thrust_yaw_ff
        self._raw_hold = 0.0  # last raw PID steering (held when thrust ~0)
        self._filtered = 0.0  # low-passed output actually commanded

    def reset(self) -> None:
        self.pid.reset()
        self._raw_hold = 0.0
        self._filtered = 0.0

    def compute(self, setpoint: Setpoint, state: NavigationState, dt: float) -> MotorCommand:
        if isinstance(setpoint, ManualSetpoint):
            # Add the thrust-yaw feed-forward so a hands-off (steering=0) helm
            # tracks straight even with an off-centre motor; only while making way.
            ff = self.thrust_yaw_ff if abs(setpoint.thrust) >= STEER_EPS else 0.0
            return MotorCommand(
                thrust=setpoint.thrust,
                steering=(setpoint.steering + ff) * self.steer_sign,
            ).clamped()

        assert isinstance(setpoint, GuidedSetpoint)
        state.target_heading = setpoint.target_heading

        if abs(setpoint.thrust) < STEER_EPS:
            # No thrust => no steering authority: hold the raw target (the
            # low-pass below then just keeps the actuator where it is).
            raw = self._raw_hold
        else:
            # Positive error => target is to starboard => steer right (positive).
            error = angle_difference(state.heading_deg, setpoint.target_heading)
            raw = self._pid_update_scheduled(error, state.sog_knots, dt)
            # A single steerable thruster reverses its steering authority when
            # the prop runs in reverse, so flip the command to stay stable.
            if setpoint.thrust < 0:
                raw = -raw
        self._raw_hold = raw

        # Low-pass the command so the steering head isn't driven by noise.
        if self.steer_tau > 0.0:
            alpha = dt / (self.steer_tau + dt)
            self._filtered += (raw - self._filtered) * alpha
        else:
            self._filtered = raw
        # Thrust-yaw feed-forward: a constant deflection that pre-cancels the
        # lateral-offset yaw bias, so the loop doesn't have to fight it (which a
        # PD helm could only do at a steady-state heading error). Applied only
        # while making way and inside ``steer_sign`` like the rest of the command.
        ff = self.thrust_yaw_ff if abs(setpoint.thrust) >= STEER_EPS else 0.0
        steering = (self._filtered * self.autopilot_steer_scale + ff) * self.steer_sign
        return MotorCommand(thrust=setpoint.thrust, steering=steering).clamped()

    def _pid_update_scheduled(self, error: float, sog_knots: float, dt: float) -> float:
        """Run the heading PID with an SOG-scheduled proportional gain (#31).

        ``kp_eff = pid.kp * schedule.multiplier(sog)`` is applied for THIS update
        only, then ``pid.kp`` is restored to its base value -- so the schedule
        scales the effective steering gain without permanently mutating the tuned
        base (auto-tune / per-boat gains keep owning ``pid.kp``). With no schedule
        (or a neutral one) ``kp_eff == pid.kp`` and this is identical to a plain
        ``pid.update_error`` call.
        """
        if self.gain_schedule is None:
            self.kp_eff = self.pid.kp
            return self.pid.update_error(error, dt)
        base = self.pid.kp
        self.kp_eff = base * self.gain_schedule.multiplier(sog_knots)
        self.pid.kp = self.kp_eff
        try:
            return self.pid.update_error(error, dt)
        finally:
            self.pid.kp = base


class Controller:
    def __init__(
        self,
        state: NavigationState,
        motor: MotorController,
        bus: EventBus | None = None,
        *,
        tick_hz: float = 5.0,
        helm: Helm | None = None,
        anchor_config: AnchorConfig | None = None,
        waypoint_config: WaypointConfig | None = None,
        follow_apb_config: FollowApbConfig | None = None,
        drift_config: DriftConfig | None = None,
        contour_config: ContourConfig | None = None,
        orbit_config: OrbitConfig | None = None,
        trolling_config: TrollingConfig | None = None,
        safety_config: SafetyConfig | None = None,
        estimator_config: EstimatorConfig | None = None,
        cruise_pid: PID | None = None,
        jog_increment_m: float = 1.5,
        track_min_distance_m: float = 5.0,
        mono_fn=time.monotonic,
        safety_floor: SafetyFloor | None = None,
    ) -> None:
        self.state = state
        self.motor = motor
        # Device connectivity (kind -> bool), set by the Runtime. A "Not connected"
        # device (source "none") disables the modes that need it; empty = all
        # connected (fail-open). See vanchor.core.capabilities.
        self.device_connected: dict[str, bool] = {}
        self.bus = bus
        # Non-negotiable safety-floor lockout (#50), enforced HERE at the actual
        # mutation site so a command delivered via the bus "command" topic (which
        # reaches handle_command directly, bypassing Runtime.handle_command's
        # check) still can't weaken a failsafe -- defense in depth. When one isn't
        # injected, capture it from the startup safety_config so the floor is the
        # config's values (a later tighten is allowed; a weakening is refused).
        self.safety_floor = safety_floor or SafetyFloor.from_config(
            safety_config or SafetyConfig()
        )
        self.tick_hz = tick_hz
        # MONOTONIC clock seam for the sensor-staleness ages fed to the governor.
        # Injectable so it can be driven deterministically (matches Runtime's
        # mono_fn, which is what the navigator stamps receive-times with).
        self._mono_fn = mono_fn
        self.helm = helm or Helm()

        # Persistent wind/current (drift) estimator: ONE instance, fed every
        # control tick in EVERY mode, so the environmental drift estimate is
        # always warm. It NEVER resets on a mode change -- so anchor hold, waypoint
        # crab feed-forward and drift mode all engage already knowing the set,
        # instead of relearning it. Its estimate is published onto ``state`` for
        # any mode (and the HUD) to read.
        if estimator_config is None:
            # Keep the estimator's thrust-decoupling boat speed in step with the
            # boat spec app.py configured on the anchor config, so it doesn't need
            # its own tuning path.
            estimator_config = EstimatorConfig()
            if anchor_config is not None:
                estimator_config.boat_max_speed_mps = anchor_config.boat_max_speed_mps
        self.estimator = WindCurrentEstimator(estimator_config)

        # Cruise Control: an optional SOG (speed-over-ground) PID that takes over
        # the throttle of guided "underway" modes when a target speed is set.
        self.cruise_pid = cruise_pid or PID(
            kp=0.64, ki=0.25, kd=0.0, output_min=0.0, output_max=1.0
        )
        self.cruise_knots: float | None = None
        # Throttle % override (#49): when set (0..1), guided modes use this as
        # their thrust magnitude instead of their built-in default. ``None`` =
        # no override.
        self.throttle_override: float | None = None
        # Pause/Resume (#50): a snapshot of the active guided mode + its
        # parameters captured on pause, restored on resume. ``None`` = nothing
        # suspended.
        self.suspended: dict | None = None
        self.jog_increment_m = jog_increment_m
        self.track = TrackRecorder(track_min_distance_m)

        self.manual = ManualMode()
        # Share one WaypointConfig between waypoint + work-area travel so boat-spec
        # tuning (app._apply_boat_specs) applies to both.
        wp_cfg = waypoint_config or WaypointConfig()
        self.modes: dict[ControlModeName, ControlMode] = {
            ControlModeName.MANUAL: self.manual,
            ControlModeName.ANCHOR_HOLD: AnchorHoldMode(anchor_config),
            ControlModeName.HEADING_HOLD: HeadingHoldMode(),
            ControlModeName.WAYPOINT: WaypointMode(wp_cfg),
            ControlModeName.WORK_AREA: WorkAreaMode(
                WorkAreaConfig(), waypoint_config=wp_cfg, anchor_config=anchor_config
            ),
            ControlModeName.FOLLOW_APB: FollowApbMode(follow_apb_config),
            ControlModeName.DRIFT: DriftMode(drift_config),
            ControlModeName.CONTOUR_FOLLOW: ContourFollowMode(contour_config),
            ControlModeName.ORBIT: OrbitMode(orbit_config),
            ControlModeName.TROLLING: TrollingMode(trolling_config),
        }
        # Learned anchor hold (optional): registered only if the shipped tiny-NN
        # model loads, so a missing/invalid model never breaks startup -- the
        # mode simply isn't offered. It mirrors the helm's steer_sign (thruster
        # mount polarity) so it stays mount-aware; app._apply_boat_specs keeps
        # both in sync when the boat profile changes.
        try:
            from .anchor_ml import AnchorMLMode

            # AnchorMLMode is duck-typed (name/activate/update) but not a
            # ControlMode subclass, so mypy flags the assignment.
            self.modes[ControlModeName.ANCHOR_ML] = AnchorMLMode(  # type: ignore[assignment]
                steer_sign=self.helm.steer_sign
            )
        except Exception as exc:  # noqa: BLE001 - any load error -> mode absent
            logger.warning("anchor_ml mode unavailable: %s", exc)
        try:
            from .anchor_ml import AnchorLeifMode

            self.modes[ControlModeName.ANCHOR_LEIF] = AnchorLeifMode(  # type: ignore[assignment]  # duck-typed mode, not a ControlMode subclass
                steer_sign=self.helm.steer_sign
            )
        except Exception as exc:  # noqa: BLE001 - any load error -> mode absent
            logger.warning("anchor_leif mode unavailable: %s", exc)
        # Hold quality metric (#34): rolling RMS radial error + % time in
        # radius while an anchor mode is holding. Reset whenever the anchor mark
        # changes; paused when not station-keeping.
        self.hold_quality = HoldQuality(window_s=60.0)
        self._hold_anchor: GeoPoint | None = None
        self.safety = SafetyGovernor(safety_config)
        self.safety_status = SafetyStatus()
        self._last_fix_seq = state.fix_seq
        self._running = False
        # Consecutive failed control ticks (reset on any clean tick). Drives a
        # small backoff so a persistently-throwing tick doesn't spin the CPU.
        self._consecutive_faults = 0

        if bus is not None:
            bus.subscribe("command", self._on_command)

    # ------------------------------------------------------------------ #
    # Core control logic (synchronous, deterministic)
    # ------------------------------------------------------------------ #
    def control_tick(self, dt: float) -> MotorCommand:
        # Update the persistent drift estimate FIRST, so the active mode sees a
        # fresh ``state.est_drift_*`` this tick. It decouples our own propulsion
        # using the PREVIOUS tick's applied command (state.motor_command), exactly
        # as the old mode-local estimator did.
        self.estimator.update(self.state, dt)
        mode = self.modes[self.state.mode]
        setpoint = mode.update(self.state, dt)
        # Per-waypoint speed: WaypointMode posts a request when the boat ARRIVES
        # at a mark carrying a speed attribute. Adopt it as the new default by
        # routing it into the same channels a manual speed command uses -- so a
        # later manual set_throttle/cruise overrides it, and the next
        # speed-carrying waypoint overrides that in turn.
        req = self.state.route_speed_request
        if req is not None:
            self.state.route_speed_request = None
            kind, value = req
            if kind == "speed_kn":
                self.throttle_override = None
                self._set_cruise(value)
            else:
                self.cruise_knots = None
                self._set_throttle(value)
        setpoint = self._apply_cruise(setpoint, dt)
        setpoint = self._apply_throttle_override(setpoint)
        command = self.helm.compute(setpoint, self.state, dt)

        # A fresh fix is one whose sequence number advanced since last tick.
        fix_is_fresh = self.state.fix_seq != self._last_fix_seq
        self._last_fix_seq = self.state.fix_seq

        # Sensor-staleness ages (seconds since each input last arrived). ``None``
        # when never stamped -> the governor treats those as fresh (so a harness
        # that never advances the clock can't be false-tripped).
        heading_age_s, depth_age_s = self._sensor_ages()

        command, self.safety_status = self.safety.govern(
            command,
            self.state,
            dt,
            fix_is_fresh,
            heading_age_s=heading_age_s,
            depth_age_s=depth_age_s,
        )
        self.state.motor_command = command
        self.motor.apply(command)

        # Hold quality (#34): accumulate while an anchor mode is holding
        # (the mode refreshed state.distance_to_anchor_m just above).
        self._update_hold_quality(dt)

        # Breadcrumb the boat's path if a track recording is in progress.
        self.track.maybe_record(self.state.position)

        # Fire the route's on-arrival action once, after it completes.
        if (
            self.state.mode == ControlModeName.WAYPOINT
            and self.state.route_complete
            and self.state.route_on_arrival in ("anchor", "stop")
        ):
            action = self.state.route_on_arrival
            self.state.route_on_arrival = "none"  # one-shot
            logger.info("route complete -> on-arrival action: %s", action)
            if action == "anchor":
                self.handle_command({"type": "anchor_hold"})
            else:
                self.handle_command({"type": "stop"})
        return command

    def _update_hold_quality(self, dt: float) -> None:
        """Feed the hold quality tracker (#34) from the shared state.

        Accumulates only while an anchor mode is actively holding a mark (both
        the PID hold and the learned hold, so they are directly comparable);
        RESETS when the anchor is cleared or moved (a jog / a new drop starts a
        fresh measurement); PAUSES (keeps the last numbers) when the boat is in
        another mode with an anchor still set -- distance_to_anchor_m goes stale
        there and must not pollute the metric.
        """
        st = self.state
        if st.anchor is None:
            if self._hold_anchor is not None:
                self.hold_quality.reset()
                self._hold_anchor = None
                st.hold_rms_m = 0.0
                st.hold_pct_in_radius = 0.0
                st.hold_holding_s = 0.0
            return
        if st.mode not in _HOLD_MODES or st.position is None:
            return  # paused: anchor set but not actively station-keeping
        if st.anchor != self._hold_anchor:
            self.hold_quality.reset()
            self._hold_anchor = st.anchor
        self.hold_quality.update(st.distance_to_anchor_m, st.anchor_radius_m, dt)
        st.hold_rms_m = self.hold_quality.rms_m
        st.hold_pct_in_radius = self.hold_quality.pct_in_radius
        st.hold_window_s = self.hold_quality.window_s
        st.hold_holding_s = self.hold_quality.elapsed_s

    def _sensor_ages(self) -> tuple[float | None, float | None]:
        """(heading_age_s, depth_age_s) since each input was last ingested, or
        ``None`` when it has never been stamped. Uses the injected monotonic
        clock -- the same seam the navigator stamps receive-times with."""
        now = self._mono_fn()
        h = self.state.heading_received_mono
        d = self.state.depth_received_mono
        return (
            (now - h) if h is not None else None,
            (now - d) if d is not None else None,
        )

    def _apply_cruise(self, setpoint: Setpoint, dt: float) -> Setpoint:
        """When Cruise Control is on, replace a guided mode's fixed throttle with
        the output of the SOG PID so the boat holds a target speed over ground."""
        if (
            self.cruise_knots is None
            or not isinstance(setpoint, GuidedSetpoint)
            or self.state.mode not in _CRUISING_MODES
        ):
            return setpoint
        thrust = self.cruise_pid.update(self.state.sog_knots, dt)
        # The cruise PID's output is unsigned (output_min=0.0): it only knows a
        # *speed* target, not a direction. Preserve the mode's intended thrust
        # SIGN so a reverse manoeuvre (e.g. WaypointMode backing toward a mark
        # that's close behind) still drives astern instead of being flipped to a
        # forward push that drives the boat away from the mark. A zero setpoint
        # stays forward-neutral (copysign of 0.0 is +).
        if setpoint.thrust < 0.0:
            thrust = math.copysign(thrust, setpoint.thrust)
        return GuidedSetpoint(target_heading=setpoint.target_heading, thrust=thrust)

    def _apply_throttle_override(self, setpoint: Setpoint, dt: float = 0.0) -> Setpoint:
        """Replace a guided mode's thrust *magnitude* with the % override.

        The mode keeps full control of *direction* (sign of thrust -- e.g. an
        anchor recovery backing up, or drift braking in reverse) and of whether
        it wants thrust at all (a zero stays zero); only the magnitude of a
        non-zero demand is scaled to the user's engine power. Cruise (a KNOTS
        speed hold) is left untouched -- it has already produced its setpoint and
        owns the throttle for cruising modes when active.
        """
        if (
            self.throttle_override is None
            or not isinstance(setpoint, GuidedSetpoint)
            or setpoint.thrust == 0.0
        ):
            return setpoint
        # Don't fight an active cruise (knots) hold on a cruising mode.
        if self.cruise_knots is not None and self.state.mode in _CRUISING_MODES:
            return setpoint
        magnitude = self.throttle_override
        thrust = math.copysign(magnitude, setpoint.thrust)
        return GuidedSetpoint(target_heading=setpoint.target_heading, thrust=thrust)

    def set_mode(self, mode: ControlModeName) -> None:
        changed = mode != self.state.mode
        if changed:
            logger.info("mode: %s -> %s", self.state.mode.value, mode.value)
        self.state.mode = mode
        # Only tear down the inner loops on a REAL mode change. Re-issuing the
        # current mode (e.g. a remote-helm button re-sending {"type":"manual"}
        # every press) must NOT reset the helm/governor: a governor reset zeroes
        # the slew anchors, so a re-sent command would ramp the prop from 0 again
        # (surge) and bypass the reverse interlock. On a genuine change we DO
        # reset, but seed the governor's slew anchors from the last applied motor
        # command so thrust/steering ramp from where the boat actually is rather
        # than snapping through zero.
        if changed:
            self.helm.reset()
            last = self.state.motor_command
            self.safety.reset(thrust=last.thrust, steering=last.steering)
            # Vectored station-keeping telemetry (#35) is written only by the
            # anchor hold while it runs; clear it so it can't go stale in
            # another mode. The hold re-asserts it on its first tick.
            self.state.stationkeep_vectored = False
            self.state.stationkeep_azimuth_deg = 0.0
        self.modes[mode].activate(self.state)

    # ------------------------------------------------------------------ #
    # Command handling
    # ------------------------------------------------------------------ #
    def handle_command(self, command: dict) -> None:
        """Apply a command dict. Shape: ``{"type": ..., ...}``."""
        ctype = command.get("type")
        # Device-availability gate: refuse to engage a mode whose required device
        # is "Not connected" (backstop for a stale/API command; the UI already
        # greys these out). STOP and non-mode commands are never gated.
        target_mode = _CTYPE_MODE.get(ctype) if isinstance(ctype, str) else None
        if target_mode is not None:
            miss = missing_devices(target_mode, self.device_connected)
            if miss:
                reason = " + ".join(DEVICE_LABEL.get(d, d) for d in miss) + " not connected"
                logger.warning("refusing mode %s: %s", ctype, reason)
                return
        try:
            if ctype == "manual":
                bearing = command.get("steer_bearing")
                course = command.get("steer_course")
                if course is not None:
                    # Course hold: follow the ground-track line drawn from the
                    # engage position along the bearing (XTE-corrected).
                    self.manual.set_course(
                        float(command.get("thrust", 0.0)), float(course),
                        self.state.position,
                    )
                elif bearing is not None:
                    # Absolute steering: hold the motor head on a compass
                    # bearing (0=N, 180=S) while the boat yaws underneath.
                    self.manual.set_bearing(
                        float(command.get("thrust", 0.0)), float(bearing)
                    )
                else:
                    self.manual.set(
                        float(command.get("thrust", 0.0)),
                        float(command.get("steering", 0.0)),
                    )
                self.set_mode(ControlModeName.MANUAL)
            elif ctype == "anchor_hold":
                anchor = command.get("anchor")
                if anchor:
                    self.state.anchor = GeoPoint(float(anchor["lat"]), float(anchor["lon"]))
                elif self.state.position is not None:
                    self.state.anchor = self.state.position
                if "radius_m" in command:
                    self.state.anchor_radius_m = float(command["radius_m"])
                # Optional per-drop opt-in/out of vectored station-keeping (#35);
                # absent = keep the configured setting.
                if "vectored" in command:
                    hold = self.modes[ControlModeName.ANCHOR_HOLD]
                    if hasattr(hold, "config"):
                        hold.config.vectored = bool(command["vectored"])
                # Record the heading at the moment of dropping (for display); the boat
                # holds this passively once on station (no active heading slew).
                self.state.anchor_heading = self.state.heading_deg
                self.set_mode(ControlModeName.ANCHOR_HOLD)
            elif ctype == "anchor_ml":
                # Learned anchor hold: same mark-setting as anchor_hold, but driven by
                # the tiny NN. Falls back to the PID mode if the model isn't loaded.
                anchor = command.get("anchor")
                if anchor:
                    self.state.anchor = GeoPoint(float(anchor["lat"]), float(anchor["lon"]))
                elif self.state.position is not None:
                    self.state.anchor = self.state.position
                if "radius_m" in command:
                    self.state.anchor_radius_m = float(command["radius_m"])
                self.state.anchor_heading = self.state.heading_deg
                self.set_mode(
                    ControlModeName.ANCHOR_ML
                    if ControlModeName.ANCHOR_ML in self.modes
                    else ControlModeName.ANCHOR_HOLD
                )
            elif ctype == "anchor_leif":
                # "Leif": the pure full-azimuth learned station-keeper. Same
                # mark-setting as anchor_hold; falls back to the hybrid, then PID,
                # if the Leif model isn't loaded.
                anchor = command.get("anchor")
                if anchor:
                    self.state.anchor = GeoPoint(float(anchor["lat"]), float(anchor["lon"]))
                elif self.state.position is not None:
                    self.state.anchor = self.state.position
                if "radius_m" in command:
                    self.state.anchor_radius_m = float(command["radius_m"])
                self.state.anchor_heading = self.state.heading_deg
                self.set_mode(
                    ControlModeName.ANCHOR_LEIF
                    if ControlModeName.ANCHOR_LEIF in self.modes
                    else (ControlModeName.ANCHOR_ML
                          if ControlModeName.ANCHOR_ML in self.modes
                          else ControlModeName.ANCHOR_HOLD)
                )
            elif ctype == "heading_hold":
                heading = command.get("heading")
                self.state.target_heading = (
                    float(heading) if heading is not None else self.state.heading_deg
                )
                if "throttle" in command:
                    cast(
                        HeadingHoldMode, self.modes[ControlModeName.HEADING_HOLD]
                    ).throttle = float(command["throttle"])
                self.set_mode(ControlModeName.HEADING_HOLD)
            elif ctype == "goto":
                wps = command.get("waypoints", [])
                self.state.waypoints = [
                    Waypoint(
                        name=str(w.get("name", f"WP{i}")),
                        point=GeoPoint(float(w["lat"]), float(w["lon"])),
                        throttle_pct=_wp_speed(w)[0],
                        speed_kn=_wp_speed(w)[1],
                    )
                    for i, w in enumerate(wps)
                ]
                if "throttle" in command:
                    cast(
                        WaypointMode, self.modes[ControlModeName.WAYPOINT]
                    ).config.throttle = float(command["throttle"])
                # "active" present => a LIVE EDIT of the running route (the user
                # dragged/inserted/deleted/reordered a committed waypoint and the UI
                # re-sent it). Resume from the given index (clamped) instead of
                # restarting at 0, and leave the route's mode/flags/progress intact so
                # an edit doesn't make the boat start over. Absent => a fresh start.
                resume = command.get("active")
                if resume is None:
                    self.state.active_waypoint = 0
                    # What to do when the route finishes: "anchor", "stop", or "none".
                    self.state.route_on_arrival = str(command.get("on_arrival", "none"))
                    # Closed-loop route (e.g. "around island"): circle continuously.
                    self.state.route_loop = bool(command.get("loop", False))
                    # Patrol: at each end, reverse and run the route back.
                    self.state.route_patrol = bool(command.get("patrol", False))
                    self.set_mode(ControlModeName.WAYPOINT)
                else:
                    n = len(self.state.waypoints)
                    self.state.active_waypoint = max(0, min(int(resume), n - 1)) if n else 0
                    if self.state.mode != ControlModeName.WAYPOINT:
                        self.set_mode(ControlModeName.WAYPOINT)
            elif ctype == "load_route":
                # Waypoints already parsed (from GPX) and placed on the state by the
                # runtime; just (re)start waypoint navigation.
                self.state.active_waypoint = 0
                self.state.route_loop = bool(command.get("loop", False))
                self.state.route_patrol = bool(command.get("patrol", False))
                if "throttle" in command:
                    cast(
                        WaypointMode, self.modes[ControlModeName.WAYPOINT]
                    ).config.throttle = float(command["throttle"])
                self.set_mode(ControlModeName.WAYPOINT)
            elif ctype == "work_area":
                # Work Area: spots = waypoints (each with optional hold heading); visit
                # each, hold position, advance on the "next spot" button and/or a
                # dwell timer. loop/patrol cycle the spots like a route.
                wps = command.get("waypoints", [])
                self.state.waypoints = [
                    Waypoint(
                        name=str(w.get("name", f"Spot {i + 1}")),
                        point=GeoPoint(float(w["lat"]), float(w["lon"])),
                        heading=(float(w["heading"]) if w.get("heading") is not None else None),
                    )
                    for i, w in enumerate(wps)
                ]
                self.state.active_waypoint = 0
                self.state.route_loop = bool(command.get("loop", False))
                self.state.route_patrol = bool(command.get("patrol", False))
                wa = cast(WorkAreaMode, self.modes[ControlModeName.WORK_AREA]).config
                if "dwell_s" in command:
                    wa.dwell_s = max(0.0, float(command["dwell_s"]))
                if "advance" in command:
                    wa.advance = "timed" if str(command["advance"]) == "timed" else "manual"
                if "throttle" in command:
                    wa.throttle = float(command["throttle"])
                self.set_mode(ControlModeName.WORK_AREA)
            elif ctype == "next_spot":
                # The big on-screen "Go to next spot" button (Work Area mode).
                self.state.work_next_requested = True
            elif ctype == "follow_apb":
                if "throttle" in command:
                    cast(
                        FollowApbMode, self.modes[ControlModeName.FOLLOW_APB]
                    ).config.throttle = float(command["throttle"])
                self.set_mode(ControlModeName.FOLLOW_APB)
            elif ctype == "drift":
                heading = command.get("heading")
                self.state.target_heading = (
                    float(heading) if heading is not None else self.state.heading_deg
                )
                if "knots" in command:
                    self.state.drift_target_knots = float(command["knots"])
                self.set_mode(ControlModeName.DRIFT)
            elif ctype == "contour_follow":
                self.state.contour_target_depth_m = float(command.get("target_depth_m", 0.0))
                self.state.contour_side = str(command.get("side", "deep"))
                self._apply_speed_knots(command.get("speed_knots"))
                self.set_mode(ControlModeName.CONTOUR_FOLLOW)
            elif ctype == "orbit":
                self.state.orbit_center = GeoPoint(
                    float(command["center_lat"]), float(command["center_lon"])
                )
                self.state.orbit_radius_m = float(command.get("radius_m", 20.0))
                self.state.orbit_direction = str(command.get("direction", "cw"))
                self._apply_speed_knots(command.get("speed_knots"))
                self.set_mode(ControlModeName.ORBIT)
            elif ctype == "trolling":
                base = command.get("base_heading")
                self.state.trolling_base_heading = (
                    float(base) if base is not None else self.state.heading_deg
                )
                self.state.trolling_amplitude_deg = float(command.get("amplitude_deg", 20.0))
                self.state.trolling_period_s = float(command.get("period_s", 20.0))
                self._apply_speed_knots(command.get("speed_knots"))
                self.set_mode(ControlModeName.TROLLING)
            elif ctype == "jog":
                self._jog(command)
            elif ctype == "cruise":
                self._set_cruise(command.get("knots"))
            elif ctype == "set_throttle":
                self._set_throttle(command.get("percent"))
            elif ctype == "pause_nav":
                self._pause_nav()
            elif ctype == "resume_nav":
                self._resume_nav()
            elif ctype in ("record", "replay", "backtrack"):
                self._track_command(ctype, command)
            elif ctype == "set_nogo_zones":
                self.safety.set_nogo_zones(
                    [
                        [(float(p[0]), float(p[1])) for p in ring]
                        for ring in command.get("zones", [])
                    ]
                )
                logger.info("no-go zones set: %d", self.safety.nogo_zone_count)
            elif ctype == "set_min_depth":
                self.set_min_depth(float(command.get("min_depth_m", 0.0)))
            elif ctype == "set_fix_failsafe":
                self.set_fix_failsafe(bool(command.get("enabled", False)))
            elif ctype == "set_launch":
                self._set_launch()
            elif ctype == "mob":
                self._mob()
            elif ctype == "mob_clear":
                self._mob_clear()
            elif ctype == "stop":
                self.suspended = None  # a hard stop clears any paused nav
                self.manual.set(0.0, 0.0)
                self.set_mode(ControlModeName.MANUAL)
            else:
                logger.warning("unknown command: %r", command)
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("handle_command %r: malformed payload: %s", ctype, exc)

    # -- Safety-floor-guarded failsafe mutations (#50) ------------------- #
    def set_min_depth(self, min_depth_m: float) -> None:
        """Set the shallow-water auto-stop depth, clamped to the safety floor.

        The floor (#50) refuses a value BELOW the startup min-depth (which would
        weaken the stop); it can always be RAISED. Enforced here so a command
        arriving over the bus "command" topic -- which reaches this method
        directly, bypassing Runtime.handle_command -- still can't weaken it."""
        allowed = self.safety_floor.enforce_min_depth(float(min_depth_m))
        self.safety.config.min_depth_m = allowed
        logger.info("min depth set: %.1f m", self.safety.config.min_depth_m)

    def set_fix_failsafe(self, enabled: bool) -> None:
        """Enable/disable the loss-of-fix failsafe, clamped to the safety floor.

        The floor (#50) refuses DISABLING a failsafe that was locked ON at
        startup. Enforced here so a bus-delivered command can't turn it off."""
        allowed = self.safety_floor.enforce_fix_failsafe(bool(enabled))
        self.safety.config.fix_failsafe_enabled = allowed
        logger.info("loss-of-fix failsafe %s",
                    "ON" if self.safety.config.fix_failsafe_enabled else "OFF")

    # -- Tier-1 features ------------------------------------------------- #
    def _jog(self, command: dict) -> None:
        """Anchor jog: nudge the anchor boat-relative (fwd/back/left/right)."""
        if self.state.anchor is None:
            logger.warning("jog ignored: no anchor set")
            return
        direction = str(command.get("direction", "forward"))
        if direction not in _JOG_OFFSETS:
            logger.warning("jog: unknown direction %r", direction)
            return
        distance = float(command.get("distance_m", self.jog_increment_m))
        bearing = normalize_deg(self.state.heading_deg + _JOG_OFFSETS[direction])
        self.state.anchor = destination_point(self.state.anchor, distance, bearing)
        logger.info("jog %s %.1f m -> anchor moved", direction, distance)

    def _apply_speed_knots(self, knots: float | None) -> None:
        """For the guided pattern modes (contour/orbit/trolling): if a
        ``speed_knots`` is supplied, hold it via the existing Cruise Control
        (SOG) loop so the boat keeps that speed over ground; if it is ``None``
        the mode falls back to its own default thrust (cruise left as-is)."""
        if knots is not None:
            self._set_cruise(knots)

    def _set_cruise(self, knots: float | None) -> None:
        """Enable/disable Cruise Control. ``knots`` <= 0 or None turns it off."""
        if knots is None or float(knots) <= 0.0:
            self.cruise_knots = None
            logger.info("cruise off")
            return
        self.cruise_knots = float(knots)
        self.cruise_pid.setpoint = self.cruise_knots
        self.cruise_pid.reset()
        logger.info("cruise on: %.1f kn", self.cruise_knots)

    def _set_throttle(self, percent: float | None) -> None:
        """Set/clear the guided-mode throttle % override (#49). ``None`` or 0
        clears it; otherwise a 0..100 percent of engine power."""
        if percent is None or float(percent) <= 0.0:
            self.throttle_override = None
            logger.info("throttle override cleared")
            return
        pct = max(0.0, min(100.0, float(percent)))
        self.throttle_override = pct / 100.0
        logger.info("throttle override: %.0f%%", pct)

    # -- Pause / Resume navigation (#50) --------------------------------- #
    def _pause_nav(self) -> None:
        """Remember the active guided mode + its parameters, then hold position
        (anchor-hold at the current spot)."""
        if self.state.mode == ControlModeName.MANUAL:
            logger.info("pause_nav ignored: not navigating")
            return
        self.suspended = {
            "mode": self.state.mode,
            "waypoints": list(self.state.waypoints),
            "active_waypoint": self.state.active_waypoint,
            "route_on_arrival": self.state.route_on_arrival,
            "route_loop": self.state.route_loop,
            "route_patrol": self.state.route_patrol,
            "target_heading": self.state.target_heading,
            "anchor": self.state.anchor,
            "anchor_radius_m": self.state.anchor_radius_m,
            "anchor_heading": self.state.anchor_heading,
            "drift_target_knots": self.state.drift_target_knots,
            "cruise_knots": self.cruise_knots,
            "throttle_override": self.throttle_override,
        }
        logger.info("nav paused (was %s); holding position", self.state.mode.value)
        # Hold position: anchor-hold at the current spot.
        if self.state.position is not None:
            self.state.anchor = self.state.position
        self.state.anchor_heading = self.state.heading_deg
        self.set_mode(ControlModeName.ANCHOR_HOLD)

    def _resume_nav(self) -> None:
        """Restore the previously suspended mode + all its parameters."""
        snap = self.suspended
        if snap is None:
            logger.info("resume_nav ignored: nothing suspended")
            return
        self.suspended = None
        self.state.waypoints = list(snap["waypoints"])
        self.state.active_waypoint = snap["active_waypoint"]
        self.state.route_on_arrival = snap["route_on_arrival"]
        self.state.route_loop = snap.get("route_loop", False)
        self.state.route_patrol = snap.get("route_patrol", False)
        self.state.target_heading = snap["target_heading"]
        self.state.anchor = snap["anchor"]
        self.state.anchor_radius_m = snap["anchor_radius_m"]
        self.state.anchor_heading = snap["anchor_heading"]
        self.state.drift_target_knots = snap["drift_target_knots"]
        self.cruise_knots = snap["cruise_knots"]
        self.throttle_override = snap["throttle_override"]
        logger.info("nav resumed -> %s", snap["mode"].value)
        self.set_mode(snap["mode"])

    def _track_command(self, ctype: str, command: dict) -> None:
        if ctype == "record":
            action = str(command.get("action", "start"))
            if action == "start":
                self.track.start(self.state.position)
            elif action == "stop":
                self.track.stop()
            elif action == "clear":
                self.track.clear()
            return
        # replay (forward) / backtrack (reverse) -> feed WaypointMode.
        waypoints = self.track.as_waypoints(reverse=(ctype == "backtrack"))
        if not waypoints:
            logger.warning("%s ignored: no recorded track", ctype)
            return
        self.state.waypoints = waypoints
        self.state.active_waypoint = 0
        if "throttle" in command:
            cast(
                WaypointMode, self.modes[ControlModeName.WAYPOINT]
            ).config.throttle = float(command["throttle"])
        self.set_mode(ControlModeName.WAYPOINT)
        logger.info("%s: navigating %d recorded points", ctype, len(waypoints))

    # -- Return-to-Launch (#61) ------------------------------------------ #
    def _set_launch(self) -> None:
        """Record the launch/home point at the current position."""
        if self.state.position is None or self.state.position.is_null():
            logger.warning("set_launch ignored: no position fix")
            return
        self.state.launch = self.state.position
        logger.info("launch point set to current position")

    def maybe_record_launch(self) -> None:
        """Auto-record the launch point on the first good fix (idempotent)."""
        if self.state.launch is None:
            pos = self.state.position
            if pos is not None and not pos.is_null():
                self.state.launch = pos
                logger.info("launch point auto-recorded (first fix)")

    # -- Man-overboard (#63) --------------------------------------------- #
    def _mob(self) -> None:
        """Mark the current position as MOB and navigate straight back to it."""
        pos = self.state.position
        if pos is None or pos.is_null():
            logger.warning("mob ignored: no position fix")
            return
        self.state.mob = pos
        self.state.mob_active = True
        # Return to the mark as a single-waypoint route, stopping on arrival so
        # the boat holds near the casualty.
        self.state.waypoints = [Waypoint(name="MOB", point=pos)]
        self.state.active_waypoint = 0
        self.state.route_on_arrival = "stop"
        self.set_mode(ControlModeName.WAYPOINT)
        logger.info("MOB: returning to %.5f, %.5f", pos.lat, pos.lon)

    def _mob_clear(self) -> None:
        """Cancel a man-overboard return."""
        self.state.mob_active = False
        logger.info("MOB cleared")

    async def _on_command(self, command: dict) -> None:
        self.handle_command(command)

    # ------------------------------------------------------------------ #
    # Async runtime
    # ------------------------------------------------------------------ #
    async def _tick_once(self, dt: float) -> None:
        """Run a single supervised control iteration.

        The body is wrapped so that ANY exception (in a mode, the helm, the
        governor, or the motor) cannot silently kill the loop task -- which would
        leave the motor stuck on its last command (in sim, the boat runs away).
        On a fault we log with a traceback, best-effort zero the motor, record a
        fault indicator on the state, and return so the caller can keep looping.
        A clean tick clears the fault flag.
        """
        try:
            command = self.control_tick(dt)
            await self.motor.flush()
            if self.bus is not None:
                await self.bus.publish(events.MOTOR_COMMAND, command)
            self.state.controller_fault = None
            self._consecutive_faults = 0
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            self._consecutive_faults += 1
            logger.exception(
                "control tick failed (%d consecutive); zeroing motor",
                self._consecutive_faults,
            )
            self.state.controller_fault = f"{type(exc).__name__}: {exc}"
            # Best-effort STOP: never let a fault leave the prop running. Guard
            # this in its own try so a motor that is itself faulting can't escape.
            try:
                neutral = MotorCommand(thrust=0.0, steering=0.0)
                self.motor.apply(neutral)
                await self.motor.flush()
                self.state.motor_command = neutral
            except Exception:  # noqa: BLE001
                logger.exception("failed to zero motor after control fault")

    async def run(self) -> None:
        self._running = True
        period = 1.0 / self.tick_hz
        logger.info("controller loop started at %.1f Hz", self.tick_hz)
        last = time.monotonic()
        while self._running:
            now = time.monotonic()
            # Measure the REAL elapsed time and clamp it to a sane band so a
            # scheduling hiccup (a long GC pause, a debugger breakpoint) can't
            # feed the PIDs a pathological dt. Below 0.5x period there's nothing
            # to gain; above 3x we cap the integral/derivative kick.
            dt = min(max(now - last, 0.5 * period), 3.0 * period)
            last = now
            self.state.controller_last_tick_monotonic = now

            await self._tick_once(dt)

            # Sleep out the remainder of the period after subtracting the work we
            # just did, so the loop holds ~tick_hz instead of drifting slower.
            elapsed = time.monotonic() - now
            sleep_s = max(0.0, period - elapsed)
            # After repeated consecutive failures, add a small capped backoff so
            # a hard-faulting tick doesn't hot-spin -- but NEVER exit the loop.
            if self._consecutive_faults >= 3:
                sleep_s = max(sleep_s, min(self._consecutive_faults * period, 2.0))
            await asyncio.sleep(sleep_s)

    def stop(self) -> None:
        self._running = False
