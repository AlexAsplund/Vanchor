"""Application wiring and entrypoint.

``Runtime`` builds the whole event-driven system from interchangeable parts and
starts every async loop. It is driven by an :class:`AppConfig` so the same code
runs the simulator, real serial hardware, or a network-fed setup -- selected by
configuration, not by code changes.

Run it with::

    python -m vanchor.app                       # serve the UI on :8000 (sim)
    python -m vanchor.app --config my.yaml       # load a config file
    python -m vanchor.app --hardware             # use real serial devices
    python -m vanchor.app --nmea-tcp             # also accept phone NMEA over TCP
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import json
import logging
import math
import os
import time

from .controller.calibration import CalibrationRunner
from .controller.controller import Controller, GainSchedule, Helm
from .controller.modes import AnchorConfig, DriftConfig, FollowApbConfig, WaypointConfig
from .controller.safety import BatteryLadder, SafetyConfig
from .core import events, observability
from dataclasses import asdict

from .core.config import (
    AppConfig,
    HardwareConfig,
    NmeaTcpConfig,
    SafetyFloor,
    SimMotorConfig,
    _merge_into,
    apply_device_overrides,
    load,
    save_device_overrides,
)
from .core.events import EventBus
from .core.models import BoatState, ControlModeName, Environment, GeoPoint, Waypoint
from .core.pid import PID
from .core.state import NavigationState
from .nav.depth import DepthMap
from .nav.guard import SensorGuardConfig
from .nav.navigator import Navigator
from .nav.trip import TripLog
from .hardware import registry
from .hardware.drivers import load_drivers
from .hardware.watchdog import HardwareWatchdog
from .sim.bathymetry import Bathymetry
from .sim.devices import SimCompass, SimDepthSounder, SimGps
from .sim.simulator import Simulator

logger = logging.getLogger("vanchor.app")

# Populate the pluggable device-driver registry (self-registering modules under
# hardware/drivers/). A new driver adds itself here just by existing.
load_drivers()

# Populate the pluggable connector registry (self-registering modules under
# connectors/). A new connector adds itself here just by existing.
from .connectors import load_connectors  # noqa: E402

load_connectors()

# Modes that count as "underway / making way" for the lost-connection failsafe
# (#64): every guided behaviour except idle manual and station-keeping anchor.
_UNDERWAY_MODES = frozenset(
    {
        ControlModeName.HEADING_HOLD,
        ControlModeName.WAYPOINT,
        ControlModeName.FOLLOW_APB,
        ControlModeName.DRIFT,
        ControlModeName.CONTOUR_FOLLOW,
        ControlModeName.ORBIT,
        ControlModeName.TROLLING,
        ControlModeName.WORK_AREA,
    }
)

# In MANUAL, |commanded thrust| above this counts as "driving" (making way) for
# the lost-connection failsafe (#64) -- below it the boat is effectively idle.
_MANUAL_UNDERWAY_THRUST_EPS = 0.02


async def _start_motor(motor) -> None:
    """Open a motor controller's lifecycle if it has one.

    The real ``SerialMotorController`` opens its transport (and starts the
    feedback reader) in ``start()``; without this its first ``flush()`` raises
    on a never-opened port. The sim motor (and a bare ``_TeeMotor``) has no
    ``start`` and is a no-op. Raises on failure so callers can roll back."""
    start = getattr(motor, "start", None)
    if start is None:
        return
    res = start()
    if hasattr(res, "__await__"):
        await res


async def _stop_motor(motor) -> None:
    """Best-effort stop of a motor controller (sends the shutdown CMD 0 and
    closes the port on the serial controller). Swallows errors -- a shutdown /
    device-swap must never be blocked by a motor that won't close cleanly. A
    motor with no ``stop`` (sim motor) is a no-op."""
    stop = getattr(motor, "stop", None)
    if stop is None:
        return
    try:
        res = stop()
        if hasattr(res, "__await__"):
            await res
    except Exception:  # noqa: BLE001 - shutdown/swap must not be blocked
        logger.debug("motor stop failed (best-effort)")


def _mask_connector_settings(schema: list, stored: dict) -> dict:
    """Build a display-safe settings dict from ``schema`` and ``stored`` values.

    For each field in ``schema``:
    - The value is taken from ``stored`` if present, else from the field's
      ``default``.
    - Secret fields (``secret: True``) are masked: ``"•••"`` when the stored
      value is non-empty, ``""`` when it is empty/absent.
    - Internal runtime keys (``data_dir``, ``user_edited``) are never included.

    Returns a ``{key: value}`` dict covering every schema field.
    """
    result: dict = {}
    for field in schema:
        key = field.get("key")
        if not key:
            continue
        default = field.get("default", "")
        val = stored.get(key, default)
        if field.get("secret"):
            result[key] = "•••" if val else ""
        else:
            result[key] = val
    return result


def _overlay_menu_values(schema: dict, saved: dict) -> dict:
    """Return a copy of a device-menu ``schema`` with each setting's ``value``
    replaced by the saved value for that key (when present) -- so the UI shows
    persisted choices, not just factory defaults."""
    settings = []
    for s in schema.get("settings", []):
        s = dict(s)
        if s.get("key") in saved:
            s["value"] = saved[s["key"]]
        settings.append(s)
    return {**schema, "settings": settings, "actions": list(schema.get("actions", []))}


def _build_boat_params(cfg: AppConfig):
    """Build the physics-model parameters for the configured boat geometry."""
    bc = cfg.boat
    if cfg.sim.model == "fossen":
        from .sim.fossen import FossenParams

        return FossenParams(
            length=bc.length_m,
            beam=bc.beam_m,
            mass=bc.mass_kg,
            max_thrust_n=bc.max_thrust_n,
            reverse_efficiency=bc.reverse_efficiency,
            max_speed_mps=bc.max_speed_mps,
            thruster_x_m=bc.thruster_x_m(),
            thruster_y_m=bc.thruster_y_m,
            max_steer_angle_deg=bc.max_steer_angle_deg,
            hull_tracking=bc.hull_tracking,
        )
    from .sim.boat import BoatParams

    return BoatParams(
        max_speed_mps=bc.max_speed_mps,
        max_turn_rate_deg=bc.max_turn_rate_deg,
        reverse_efficiency=bc.reverse_efficiency,
    )


def _thrust_yaw_ff_norm(cfg: AppConfig) -> float:
    """Thrust-yaw feed-forward as a steering-command fraction.

    The boat config gives the cancelling deflection in radians; the helm command
    is a fraction of the full mechanical swing (``max_steer_angle_deg``, the same
    range the sim maps the command onto), so normalise by that. ``steer_sign`` is
    applied by the helm, not here.
    """
    bc = cfg.boat
    if bc.max_steer_angle_deg <= 0:
        return 0.0
    return bc.thrust_yaw_ff_angle() / math.radians(bc.max_steer_angle_deg)


def _make_fusion():
    """A GNSS/INS complementary fusion filter (M9N UBX + HWT901B IMU)."""
    from .nav.fusion import NavFusion
    return NavFusion()


def _make_gps_filter():
    """An accuracy-weighted GPS position low-pass (nav.gps_filter)."""
    from .nav.gps_filter import GpsPositionFilter
    return GpsPositionFilter()


def _build_battery_config(cfg: AppConfig):
    """Map the app `battery:` config onto the sim battery model (#60)."""
    from .sim.battery import BatteryConfig as SimBatteryConfig

    b = cfg.battery
    return SimBatteryConfig(
        capacity_ah=b.capacity_ah,
        nominal_v=b.nominal_v,
        reserve_pct=b.reserve_pct,
        # Pass the recent-draw smoothing time constant through so YAML tuning of
        # the range/time-to-empty estimate actually takes effect (#10); without
        # this the sim battery silently kept its default draw_tau_s.
        draw_tau_s=b.draw_tau_s,
    )


class _NeutralChannelMotor:
    """Hold a disabled channel at neutral (0.0) before delegating to the inner
    motor controller.

    Used for combined-plan configs where one channel source is ``"none"`` while
    the other rides the shared serial/sim board.  The combined controller still
    transmits both ``thrust`` and ``steering`` fields in every frame; this adapter
    ensures the disabled field is always 0.0 regardless of what the control loop
    computes, honouring the docstring promise in
    :func:`~vanchor.hardware.link_plan.plan_motor_links`.

    Duck-typed to the ``MotorController`` interface.
    """

    def __init__(self, inner, neutral_channel: str) -> None:
        self._inner = inner
        self._neutral = neutral_channel  # "steering" or "thrust"

    def apply(self, command) -> None:
        import dataclasses
        command = dataclasses.replace(command, **{self._neutral: 0.0})
        self._inner.apply(command)

    async def flush(self) -> None:
        flush = getattr(self._inner, "flush", None)
        if flush is None:
            return
        res = flush()
        if hasattr(res, "__await__"):
            await res

    async def start(self) -> None:
        await _start_motor(self._inner)

    async def stop(self) -> None:
        await _stop_motor(self._inner)

    def debug(self) -> str:
        try:
            inner_dbg = self._inner.debug()
        except Exception:  # noqa: BLE001
            inner_dbg = repr(self._inner)
        return f"NeutralChannel({self._neutral}=0) -> {inner_dbg}"


class _TeeMotor:
    """Fan one ``MotorCommand`` out to several motor controllers at once — e.g.
    drive the simulated boat AND a real steering servo for bench testing.
    Duck-typed to the ``MotorController`` interface (sync ``apply`` + ``flush``,
    which may be sync or async)."""

    def __init__(self, motors) -> None:
        self._motors = [m for m in motors if m is not None]

    def apply(self, command) -> None:
        for m in self._motors:
            m.apply(command)

    async def flush(self) -> None:
        for m in self._motors:
            flush = getattr(m, "flush", None)
            if flush is None:
                continue
            res = flush()
            if hasattr(res, "__await__"):
                await res

    async def start(self) -> None:
        # Open every inner motor that has a lifecycle (e.g. the real serial
        # controller opens its port + feedback task here). The sim motor has no
        # start() and is skipped.
        for m in self._motors:
            await _start_motor(m)

    async def stop(self) -> None:
        # Best-effort stop of every inner motor (sends CMD 0 + closes the port on
        # the serial controller). Never let one failure block the others.
        for m in self._motors:
            await _stop_motor(m)


class _SimChannelState:
    """Shared mutable state for a pair of sim split-motor channel adapters.

    Both :class:`_SimThrustChannel` and :class:`_SimSteeringChannel` hold a
    reference to the same state object so that either channel's flush can
    reconstruct the full :class:`~vanchor.core.models.MotorCommand` that the
    :class:`~vanchor.sim.devices.SimMotorController` expects.
    """

    __slots__ = ("thrust", "steering")

    def __init__(self) -> None:
        self.thrust: float = 0.0
        self.steering: float = 0.0


class _SimThrustChannel:
    """A split :class:`~vanchor.hardware.split_motor.MotorChannel` that drives
    the thrust axis of a :class:`~vanchor.sim.devices.SimMotorController`.

    ``set_normalized`` records the commanded thrust; ``flush`` applies the
    combined (thrust + steering) command to the underlying sim motor so the
    physics simulation sees the correct full command. Shares its
    :class:`_SimChannelState` with a sibling :class:`_SimSteeringChannel`.
    """

    def __init__(self, sim_motor, state: _SimChannelState) -> None:
        self._sim = sim_motor
        self._state = state

    def set_normalized(self, value: float) -> None:
        self._state.thrust = max(-1.0, min(1.0, value))

    async def flush(self) -> None:
        from .core.models import MotorCommand
        self._sim.apply(MotorCommand(
            thrust=self._state.thrust, steering=self._state.steering))

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def debug(self) -> str:
        return f"SimThrustChannel: thrust={self._state.thrust:+.3f}"

    @property
    def healthy(self) -> bool | None:
        return None  # sim: health not applicable


class _SimSteeringChannel:
    """A split :class:`~vanchor.hardware.split_motor.MotorChannel` that drives
    the steering axis of a :class:`~vanchor.sim.devices.SimMotorController`.

    Symmetric counterpart to :class:`_SimThrustChannel`; flush applies the
    combined command (so both channels' flushes are idempotent — the second
    just re-applies the same already-complete command).
    """

    def __init__(self, sim_motor, state: _SimChannelState) -> None:
        self._sim = sim_motor
        self._state = state

    def set_normalized(self, value: float) -> None:
        self._state.steering = max(-1.0, min(1.0, value))

    async def flush(self) -> None:
        from .core.models import MotorCommand
        self._sim.apply(MotorCommand(
            thrust=self._state.thrust, steering=self._state.steering))

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def debug(self) -> str:
        return f"SimSteeringChannel: steering={self._state.steering:+.3f}"

    @property
    def healthy(self) -> bool | None:
        return None  # sim: health not applicable


# Environment fields persisted across restarts (environment.json): the base
# weather the Simulator panel sets. Derived live values (wind_gust_now) and
# tuning constants (gust_tau_s) stay out.
_ENV_PERSIST_KEYS = (
    "current_speed", "current_dir", "wind_speed", "wind_dir",
    "gust_amplitude_mps", "wind_variability", "current_variability",
)


class Runtime:
    """Owns every component and the background tasks that drive them."""

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        now_fn=time.time,
        mono_fn=time.monotonic,
    ) -> None:
        self.config = config or AppConfig()
        cfg = self.config

        # --- Non-negotiable safety-floor lockout (#50) ------------------- #
        # Capture the locked safety values from the BASE/startup config NOW, so
        # every later apply path (the persisted safety geometry below, a runtime
        # Settings edit, a backup-restore) can be routed through it and can only
        # ratchet the failsafes TIGHTER -- never weaker. Must be set before
        # _apply_safety_geometry() runs (it applies persisted min-depth/failsafe).
        self.safety_floor = SafetyFloor.from_config(cfg.safety)

        # --- Low-battery thrust-derating ladder (#49) -------------------- #
        # Pure SoC->thrust-cap ladder, evaluated by the ~1 Hz supervisor and
        # pushed into the governor as a soft thrust cap. One-shot flag so the
        # lowest-stage RTL hand-off engages once (cleared when SoC recovers).
        self._battery_ladder = BatteryLadder.from_config(cfg.safety)
        self._battery_rtl_engaged = False

        # Two injectable clock seams so both can be driven deterministically in
        # tests. ``_now_fn`` is WALL-CLOCK -- used only for timestamps that are
        # displayed or persisted (trip start times, created_at). ``_mono_fn`` is
        # MONOTONIC -- used for DURATION timers (the lost-connection failsafe,
        # #64) so an NTP/GPS clock step on an RTC-less Pi can't shift them.
        self._now_fn = now_fn
        self._mono_fn = mono_fn

        observability.setup_logging(getattr(cfg, "log_level", "INFO"))
        self.bus = EventBus()
        observability.wiretap(self.bus)
        self.recorder = observability.TelemetryRecorder(ring_size=600)

        # Debug session recorder + replay (records telemetry/nmea/commands/logs
        # to a gzipped NDJSON file for download + replay).
        from .core.debug_recorder import DebugRecorder, ReplayPlayer

        self.debug = DebugRecorder(cfg.data_dir)
        self.replay = ReplayPlayer()
        self.bus.subscribe(events.NMEA_IN, self._record_nmea)
        # NOTE: the debug recorder attaches its OWN log handler to the ROOT
        # logger for the duration of a recording (see DebugRecorder.start), which
        # already captures every ``vanchor.*`` line. We deliberately do NOT add a
        # second handler on the ``vanchor`` logger here -- doing so recorded each
        # line twice in a debug session (review finding L3).

        # Command audit ring (#26): a bounded, in-app record of every command the
        # runtime was asked to run, tagged with WHO sent it (helm/observer/rest)
        # and the OUTCOME (accepted/denied/error). Recorded from the command entry
        # points in ui/server.py (the WS handler + REST /api/command), NOT from
        # handle_command itself -- only the entry points know the source/role.
        # Surfaced at GET /api/audit for the in-app audit view; oldest first,
        # newest last (chronological). Pings are never recorded.
        self._command_audit: collections.deque = collections.deque(maxlen=200)

        self.state = NavigationState()
        self.state.anchor_radius_m = cfg.control.anchor_radius_m
        self.state.max_steer_angle_deg = cfg.boat.max_steer_angle_deg
        self.state.drift_target_knots = cfg.control.drift_default_knots

        environment = Environment(
            current_speed=cfg.environment.current_speed,
            current_dir=cfg.environment.current_dir,
            wind_speed=cfg.environment.wind_speed,
            wind_dir=cfg.environment.wind_dir,
            gust_amplitude_mps=cfg.environment.gust_amplitude_mps,
            gust_tau_s=cfg.environment.gust_tau_s,
            wind_variability=cfg.environment.wind_variability,
            current_variability=cfg.environment.current_variability,
        )

        # Persisted Simulator-panel weather beats the config defaults, so a
        # restart resumes the same conditions instead of silently going calm
        # while the UI sliders still show the old values.
        self._env_path = os.path.join(cfg.data_dir, "environment.json")
        try:
            with open(self._env_path, encoding="utf-8") as fh:
                saved = json.load(fh)
            for k in _ENV_PERSIST_KEYS:
                if k in saved:
                    setattr(environment, k, float(saved[k]))
            logger.info("restored sim environment from %s", self._env_path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("could not restore %s: %s", self._env_path, exc)

        # --- devices: simulated and/or real serial hardware (per-device) -- #
        # Built via _construct_devices so the SAME logic powers a live reload
        # (reload_devices) when the device config changes — no process restart.
        self._environment = environment      # reused when devices are rebuilt live
        self._sim_task: "asyncio.Task | None" = None
        dev = self._construct_devices(cfg)
        self.simulator = dev["simulator"]
        self.gps = dev["gps"]
        self.compass = dev["compass"]
        self.depth_sounder = dev["depth_sounder"]
        # Battery monitor (#42): the registry-driven 4th device kind. ``sim`` is a
        # read-view of the simulator's pack (identical telemetry to before); an
        # ``ina226`` source is a real shunt driver built via the #43 capability API.
        self.battery_monitor = dev["battery_monitor"]
        motor = dev["motor"]

        # Accumulates depth soundings for the auto depth-map overlay.
        self.depth_map = DepthMap()
        self._depth_map_path = os.path.join(cfg.data_dir, "depthmap.json")
        self._depth_chart_path = os.path.join(cfg.data_dir, "depthchart.json")
        self.depth_map.load(self._depth_map_path, self._depth_chart_path)
        self._depth_saved_n = len(self.depth_map.points)

        # --- navigator + controller (identical for sim or hardware) ------- #
        self.navigator = Navigator(
            self.state,
            self.bus,
            SensorGuardConfig(
                position_jump_max_m=cfg.sensors.position_jump_max_m,
                heading_jump_max_deg=cfg.sensors.heading_jump_max_deg,
            ),
            mono_fn=self._mono_fn,
            # AUTO (None) = full WMM at the current position, the default. But the
            # SIMULATOR is a zero-declination true-heading world, so a sim compass
            # is pinned to 0.0 (a manual float in config still overrides both).
            declination_deg=(
                0.0 if cfg.sensors.magnetic_declination_deg is None
                and cfg.hardware.source("compass") == "sim"
                else cfg.sensors.magnetic_declination_deg
            ),
            # GNSS/INS fusion (additive) when enabled -- fills the state.fusion_*
            # fields from whatever sensors are present; None disables the filter.
            fusion=(_make_fusion() if cfg.sensors.fusion_enabled else None),
            gps_filter=(_make_gps_filter() if cfg.sensors.gps_position_filter else None),
        )
        # Apply a persisted fusion calibration (still-capture system-ID), if any.
        from .nav.calibration import load_calibration
        self._fusion_cal = load_calibration(cfg.data_dir)
        if self._fusion_cal is not None:
            self.navigator.apply_calibration(self._fusion_cal)
        self.controller = Controller(
            self.state,
            motor,
            self.bus,
            tick_hz=cfg.control.tick_hz,
            helm=Helm(
                PID(
                    kp=cfg.control.heading_kp,
                    ki=cfg.control.heading_ki,
                    kd=cfg.control.heading_kd,
                    output_min=-1.0,
                    output_max=1.0,
                ),
                steer_tau=cfg.control.steer_tau,
                autopilot_steer_scale=(
                    cfg.boat.autopilot_steer_deg / cfg.boat.max_steer_angle_deg
                    if cfg.boat.max_steer_angle_deg > 0 else 1.0
                ),
                # Stern mounts yaw the opposite way -> flip the steering sign so
                # the autopilot turns the boat the right way.
                steer_sign=1.0 if cfg.boat.thruster_x_m() >= 0 else -1.0,
                # Pre-cancel the yaw a laterally-offset motor makes under thrust.
                thrust_yaw_ff=_thrust_yaw_ff_norm(cfg),
                # SOG-keyed steering-gain schedule (#31). Neutral by default
                # (both multipliers 1.0) so the tuned gain is unchanged until a
                # non-flat schedule is configured (per config or a boat profile).
                gain_schedule=GainSchedule(
                    sog_lo_kn=cfg.control.steer_gain_sog_lo_kn,
                    sog_hi_kn=cfg.control.steer_gain_sog_hi_kn,
                    mult_lo=cfg.control.steer_gain_mult_lo,
                    mult_hi=cfg.control.steer_gain_mult_hi,
                    mult_min=cfg.control.steer_gain_mult_min,
                    mult_max=cfg.control.steer_gain_mult_max,
                ),
            ),
            anchor_config=AnchorConfig(
                kp=cfg.control.anchor_kp,
                kd=cfg.control.anchor_kd,
                idle_deadband_m=cfg.control.anchor_idle_deadband_m,
                boat_max_speed_mps=cfg.boat.max_speed_mps,
                # Vectored / azimuth station-keeping (#35): opt-in wide-azimuth
                # anchor hold. Defaults (False + 35 deg) keep behaviour unchanged.
                vectored=cfg.control.station_keep_vectored,
                vector_azimuth_deg=cfg.control.station_keep_azimuth_deg,
                # Mirror the helm's mount polarity so the vectored law's physical
                # azimuth survives the helm's steer_sign flip.
                steer_sign=1.0 if cfg.boat.thruster_x_m() >= 0 else -1.0,
            ),
            waypoint_config=WaypointConfig(
                arrival_radius_m=cfg.control.waypoint_arrival_m,
                throttle=cfg.control.waypoint_throttle,
                xte_gain=cfg.control.waypoint_xte_gain,
            ),
            follow_apb_config=FollowApbConfig(throttle=cfg.control.waypoint_throttle),
            drift_config=DriftConfig(kp=cfg.control.drift_kp, ki=cfg.control.drift_ki),
            safety_config=SafetyConfig(
                max_thrust_slew_per_s=cfg.safety.max_thrust_slew_per_s,
                # Steering can't rotate faster than the head physically does.
                max_steer_slew_per_s=cfg.boat.max_steer_rate_dps / cfg.boat.max_steer_angle_deg,
                reverse_delay_s=cfg.safety.reverse_delay_s,
                fix_timeout_s=cfg.safety.fix_timeout_s,
                fix_failsafe_enabled=cfg.safety.fix_failsafe_enabled,
                heading_stale_s=cfg.safety.heading_stale_s,
                depth_stale_s=cfg.safety.depth_stale_s,
                drag_alarm_factor=cfg.safety.drag_alarm_factor,
                min_depth_m=cfg.safety.min_depth_m,
                nogo_lookahead_m=cfg.safety.nogo_lookahead_m,
            ),
            cruise_pid=PID(
                kp=cfg.control.cruise_kp,
                ki=cfg.control.cruise_ki,
                kd=0.0,
                output_min=0.0,
                output_max=1.0,
            ),
            jog_increment_m=cfg.control.jog_increment_m,
            track_min_distance_m=cfg.control.track_min_distance_m,
            mono_fn=self._mono_fn,
            # Safety-floor lockout (#50) enforced at the controller's mutation
            # site too, so a bus "command"-topic set_min_depth/set_fix_failsafe
            # can't weaken a failsafe by bypassing Runtime.handle_command.
            safety_floor=self.safety_floor,
        )
        # Device-availability gating: a "Not connected" device disables the modes
        # that need it (UI greys them out with the reason; the controller refuses
        # to engage them). See vanchor.core.capabilities.
        self.controller.device_connected = self._device_connected_map(cfg)

        # --- Connector framework (consent-gated bus bridges) -------------- #
        # Load persisted grants (connectors.json) and prepare the running set.
        # Back-compat: if cfg.nmea_tcp.enabled is set and no explicit grant for
        # 'nmea-tcp' exists, auto-arm it once (write the grant) so the old
        # devices.json flag keeps working without a user re-consent step.
        from .connectors.registry import (
            armed as _conn_armed,
            load_grants as _load_grants,
            needs_reconsent as _conn_needs_reconsent,
            save_grants as _save_grants,
            spec as _conn_spec,
        )
        from .connectors.base import manifest_hash as _manifest_hash

        self._connector_grants: dict = _load_grants(cfg.data_dir)
        # Running connectors (successfully started): name -> Connector instance.
        self.connectors: dict = {}

        if cfg.nmea_tcp.enabled and "nmea-tcp" not in self._connector_grants:
            sp = _conn_spec("nmea-tcp")
            if sp is not None:
                try:
                    _tmp = sp.build(
                        {"host": cfg.nmea_tcp.host, "port": cfg.nmea_tcp.port}
                    )
                    self._connector_grants["nmea-tcp"] = {
                        "enabled": True,
                        "manifest_hash": _manifest_hash(_tmp.manifest),
                        "settings": {
                            "host": cfg.nmea_tcp.host,
                            "port": cfg.nmea_tcp.port,
                        },
                    }
                    _save_grants(cfg.data_dir, self._connector_grants)
                    logger.info(
                        "nmea-tcp connector auto-armed (legacy nmea_tcp.enabled=True)"
                    )
                except Exception:
                    logger.exception(
                        "failed to auto-arm nmea-tcp connector; legacy TCP disabled"
                    )

        # Re-sync nmea-tcp host/port from cfg if a grant already exists but
        # its settings differ (e.g. nmea_tcp.port changed in devices.json
        # after the grant was first written at auto-arm time).  Grant settings
        # are written once and never updated otherwise, so a cfg edit + restart
        # would silently use the stale port without this re-sync.
        # The enabled flag is intentionally left untouched: an explicit user
        # disable must survive across restarts.
        if "nmea-tcp" in self._connector_grants:
            _g = self._connector_grants["nmea-tcp"]
            _g_settings = _g.get("settings", {})
            # Only resync host/port from cfg when the grant was NOT explicitly
            # edited via the settings API (user_edited=True means the user
            # intentionally chose different values; don't clobber them on restart).
            if not _g_settings.get("user_edited") and (
                _g_settings.get("host") != cfg.nmea_tcp.host
                or _g_settings.get("port") != cfg.nmea_tcp.port
            ):
                _old_host = _g_settings.get("host")
                _old_port = _g_settings.get("port")
                _new_settings = {**_g_settings, "host": cfg.nmea_tcp.host, "port": cfg.nmea_tcp.port}
                self._connector_grants["nmea-tcp"] = {**_g, "settings": _new_settings}
                _save_grants(cfg.data_dir, self._connector_grants)
                logger.info(
                    "nmea-tcp grant host/port resynced from cfg "
                    "(was %s:%s, now %s:%s)",
                    _old_host,
                    _old_port,
                    cfg.nmea_tcp.host,
                    cfg.nmea_tcp.port,
                )

        # --- Trip log (#66): per-outing track + stats, persisted to disk. - #
        self.trip = TripLog(
            cfg.data_dir,
            min_distance_m=cfg.control.trip_min_distance_m,
            auto=cfg.control.auto_trip,
            start_speed_kn=cfg.control.trip_start_speed_kn,
            idle_timeout_s=cfg.control.trip_idle_timeout_s,
        )

        self._tasks: list[asyncio.Task] = []
        self.calibration = CalibrationRunner(self)

        # --- Named boat profiles (#75, #89): persisted, selectable spec bundles.
        # On first run (no boats.json) seed a small set of ready-to-pick presets
        # with the bow trolling motor active; never clobber a user's saved
        # profiles. Then apply whichever profile is marked active so a saved
        # selection survives a restart.
        from .core.boat_profiles import BoatProfileStore

        self.boats = BoatProfileStore(cfg.data_dir)
        active = self.boats.active()
        if active is not None:
            self._apply_boat_specs(active["specs"])

        # --- Per-boat saved gain profiles (#31) -------------------------- #
        # Controller gains (helm/anchor/cruise/drift PIDs + the steering-gain
        # schedule) that a boat profile can carry, persisted in a sidecar
        # ``<data_dir>/boat_gains.json`` keyed by profile id. Kept separate from
        # boats.json so the boat-profile store stays a pure spec bundle; applied
        # on top of the profile's specs whenever a profile becomes active. A
        # profile with no saved gains leaves the current/default gains standing.
        self._boat_gains_path = os.path.join(cfg.data_dir, "boat_gains.json")
        self._boat_gains: dict = self._load_boat_gains()
        self._apply_active_boat_gains()

        # --- Server-persisted safety geometry (#23) ---------------------- #
        # No-go zones / min-depth / fix-failsafe live on the SERVER now, not just
        # the browser's localStorage. The governor is the live authority; this
        # store is the persistence layer. Load + APPLY at startup so a Pi restart
        # with NO client connected keeps the operator's zones/min-depth/failsafe.
        from .core.prefs import PrefsStore, SafetyGeometryStore

        self.safety_geometry = SafetyGeometryStore(cfg.data_dir)
        self._apply_safety_geometry()
        # Generic UI-preferences KV store (browser-as-cache mechanism).
        self.prefs = PrefsStore(cfg.data_dir)

        # --- Lost-connection failsafe (#64) ------------------------------ #
        # Number of connected UI clients and the last time one was seen alive.
        self._ui_clients = 0
        self._last_client_seen: float | None = None
        # True once the failsafe has auto-engaged (so we don't repeatedly
        # re-engage it; cleared on reconnect), plus what it DID:
        # "continue" | "hold" | "stop" | None (for telemetry/alerts).
        self._link_failsafe_engaged = False
        self._link_failsafe_action: str | None = None
        # Auto Follow-APB (opt-in): latched once auto-engaged so leaving the
        # mode by hand isn't instantly overridden; re-arms when the APB feed
        # goes stale. ``engaged`` drives the UI banner.
        self._auto_apb_latched = False
        # Land guard water chart: next cache-lookup time + the bbox currently
        # loaded, so the supervisor only re-queries when the boat nears an edge.
        self._land_water_next = 0.0
        self._land_water_bbox: tuple | None = None
        # Route-planning cancellation flag (#54): set by cancel_route_plan(),
        # reset at the start of every plan_route() call.
        self._route_plan_cancelled = False
        # True while an auto-RTL plan is in flight, so the periodic evaluator
        # doesn't launch duplicate concurrent RTL plans (#61).
        self._rtl_in_flight = False
        # True while a depth-map save is running in a worker thread, so the
        # supervisor never launches an overlapping save (finding M3): the save
        # is offloaded off the event loop and must not stack up.
        self._depth_save_in_flight = False

        # --- Always-on black-box flight recorder (#20) ------------------- #
        # A bounded, low-rate ring of control-loop snapshots (desired vs applied
        # motor command + alarms) that dumps its pre-trigger history off the loop
        # on ANY alarm transition -- so incidents are captured even without the
        # opt-in debug recorder running. Wired at the governor boundary (below),
        # the one place the DESIRED and APPLIED commands are both visible.
        self._build_blackbox(cfg)

        # --- External hardware watchdog heartbeat (#44) ------------------ #
        # A GPIO line the ~1 Hz supervisor must keep toggling or an external relay
        # cuts the motor supply -- covering a Pi hard-hang the firmware watchdog
        # cannot. OFF by default and a no-op until started, so building it here is
        # free. Uses the MONOTONIC clock so an RTC step can't skew the cadence.
        from .core.config import WatchdogConfig

        self.watchdog = HardwareWatchdog.from_config(
            getattr(cfg, "watchdog", None) or WatchdogConfig(),
            now_fn=self._mono_fn,
        )

    def _build_blackbox(self, cfg: AppConfig) -> None:
        """Construct the black-box recorder and install its governor hook.

        Sizes the ring to hold ``blackbox_window_s`` of low-rate history plus one
        full post-trigger tail. A disabled recorder is a cheap no-op: no ring,
        and the governor hook is not installed (zero hot-path cost)."""
        from .obs.blackbox import BlackBox

        obs = getattr(cfg, "obs", None)
        if obs is None:  # pragma: no cover - defensive for partial configs
            from .core.config import ObsConfig

            obs = ObsConfig()
        sample_hz = max(0.01, float(obs.blackbox_sample_hz))
        tick_hz = max(0.01, float(cfg.control.tick_hz))
        window_frames = int(math.ceil(max(0.0, obs.blackbox_window_s) * sample_hz))
        post_frames = int(round(max(0.0, obs.blackbox_post_trigger_s) * tick_hz))
        self.blackbox = BlackBox(
            cfg.data_dir,
            enabled=bool(obs.blackbox_enabled),
            capacity=window_frames + post_frames + 8,
            sample_period_s=1.0 / sample_hz,
            post_trigger_frames=post_frames,
            now_fn=self._now_fn,
        )
        self._install_blackbox_hook()

    def _install_blackbox_hook(self) -> None:
        """Wrap the safety governor's ``govern`` so every control tick feeds the
        black box the DESIRED (pre-governor) and APPLIED (post-governor) command
        plus the resulting alarms. The wrapper returns the governor's result
        bit-for-bit and swallows any recorder error, so it can NEVER change or
        break the governed command -- it only observes."""
        bb = self.blackbox
        if not bb.enabled:
            return
        gov = self.controller.safety
        orig_govern = gov.govern
        state = self.state
        runtime = self

        def govern(command, *args, **kwargs):
            applied, status = orig_govern(command, *args, **kwargs)
            bb.observe(
                command,
                applied,
                status,
                state,
                controller_fault=state.controller_fault is not None,
                link_failsafe=runtime._link_failsafe_engaged,
            )
            return applied, status

        gov.govern = govern

    # ------------------------------------------------------------------ #
    # Black-box flight recorder (#20) -- read API for the UI
    # ------------------------------------------------------------------ #
    def blackbox_dumps(self) -> dict:
        """List recent black-box dump files (newest first) + whether it's on."""
        return {"enabled": self.blackbox.enabled, "dumps": self.blackbox.dumps()}

    def blackbox_path_for(self, file_name: str) -> str | None:
        """Resolve a dump file name to a safe on-disk path (or ``None``)."""
        return self.blackbox.path_for(file_name)

    # ------------------------------------------------------------------ #
    # Server-persisted safety geometry (#23)
    # ------------------------------------------------------------------ #
    def _apply_safety_geometry(self) -> None:
        """Apply the persisted safety geometry to the live governor.

        Called at startup (and after a restore) so no-go zones / min-depth /
        fix-failsafe survive a restart with no client connected. Only values the
        operator actually set are applied -- ``min_depth_m`` / ``fix_failsafe``
        left as ``None`` in the store leave the config defaults standing."""
        geo = self.safety_geometry
        gov = self.controller.safety
        if geo.nogo_zones:
            gov.set_nogo_zones(
                [[(float(p[0]), float(p[1])) for p in ring] for ring in geo.nogo_zones]
            )
        # Safety-floor lockout (#50): the persisted geometry (which a backup can
        # replace) may make these SAFER but never weaker than the startup floor,
        # so a restored/edited store can't silently disable a failsafe or lower
        # the min-depth stop.
        if geo.min_depth_m is not None:
            gov.config.min_depth_m = self.safety_floor.enforce_min_depth(geo.min_depth_m)
        if geo.fix_failsafe_enabled is not None:
            gov.config.fix_failsafe_enabled = self.safety_floor.enforce_fix_failsafe(
                geo.fix_failsafe_enabled
            )
        if geo.auto_follow_apb is not None:
            self.config.safety.auto_follow_apb = geo.auto_follow_apb
        if geo.land_guard_enabled is not None:
            gov.config.land_guard_enabled = geo.land_guard_enabled
        if geo.land_guard_margin_m is not None:
            gov.config.land_guard_margin_m = max(1.0, geo.land_guard_margin_m)
        logger.info(
            "safety geometry applied: %d no-go zones, min_depth=%s, fix_failsafe=%s",
            len(geo.nogo_zones), geo.min_depth_m, geo.fix_failsafe_enabled,
        )

    # ------------------------------------------------------------------ #
    # Boat profile (Init-boat wizard)
    # ------------------------------------------------------------------ #
    def boat_profile(self) -> dict:
        b = self.config.boat
        return {
            "length_m": b.length_m,
            "beam_m": b.beam_m,
            "mass_kg": b.mass_kg,
            "max_speed_mps": b.max_speed_mps,
            "max_thrust_n": b.max_thrust_n,
            "thruster_mount": b.thruster_mount,
            "thruster_offset_m": b.thruster_offset_m,
            "thruster_y_m": b.thruster_y_m,
            "thrust_yaw_ff": b.thrust_yaw_ff,
            "thrust_yaw_ff_trim": b.thrust_yaw_ff_trim,
            "max_steer_angle_deg": b.max_steer_angle_deg,
            "max_turn_rate_deg": b.max_turn_rate_deg,
            "hull_tracking": b.hull_tracking,
            "shaft_dia_mm": b.shaft_dia_mm,
            "steer_range_deg": b.steer_range_deg,
            "steer_reduction": b.steer_reduction,
            "sonar_cone_deg": b.sonar_cone_deg,
            # The currently-active named profile (#75) so the UI can highlight it.
            "active_boat_id": getattr(self, "boats", None) and self.boats.active_id,
        }

    def _apply_boat_specs(self, specs: dict) -> None:
        """Write a spec dict onto ``config.boat`` and make every live-applicable
        field take effect on the running sim/controller.

        Numeric/string fields are coerced to ``BoatConfig``'s declared types.
        After writing the config we *rebuild* the simulator's physics params via
        :func:`_build_boat_params` (not just poke ``max_speed_mps``) so changing
        mass, thrust, geometry etc. actually changes the boat's behaviour -- the
        Fossen model derives its damping + mass matrices from these at build
        time, so an in-place tweak alone would be ignored.
        """
        b = self.config.boat
        for key, val in specs.items():
            if hasattr(b, key) and val is not None:
                cur = getattr(b, key)
                setattr(b, key, type(cur)(val) if isinstance(cur, (int, float)) else val)

        # Steering authority / slew limits + hull-character control tuning.
        self.state.max_steer_angle_deg = b.max_steer_angle_deg
        # The hull character (directional stability) biases the AUTOPILOT TUNING,
        # so a boat starts sensibly tuned even on real hardware (where it can't
        # change the physics): a stiff, tracking hull (high hull_tracking) resists
        # turning -> use MORE steering authority and less command smoothing; a
        # loose, skittish hull -> LESS authority and more smoothing to avoid
        # hunting. This is a PRIOR -- the auto-calibration drive then measures the
        # real boat and refines from here. At hull_tracking == 1.0 it is a no-op.
        ht = min(3.0, max(0.25, b.hull_tracking))
        if b.max_steer_angle_deg > 0:
            self.controller.safety.config.max_steer_slew_per_s = (
                b.max_steer_rate_dps / b.max_steer_angle_deg
            )
            auth_deg = min(b.autopilot_steer_deg * ht, b.max_steer_angle_deg)
            self.controller.helm.autopilot_steer_scale = auth_deg / b.max_steer_angle_deg
        self.controller.helm.steer_tau = (
            self.config.control.steer_tau * min(1.8, max(0.6, ht ** -0.5))
        )
        # A bow vs stern mount flips which way a steering deflection turns the
        # boat -- keep the helm's sign in step so switching profiles never leaves
        # the autopilot steering backwards.
        self.controller.helm.steer_sign = 1.0 if b.thruster_x_m() >= 0 else -1.0
        # The learned anchor mode mirrors the mount sign too (the Helm still owns
        # the actual command flip; the mode uses this for mount awareness +
        # telemetry) -- keep it in step on every profile change.
        ml = self.controller.modes.get(ControlModeName.ANCHOR_ML)
        if ml is not None and hasattr(ml, "steer_sign"):
            ml.steer_sign = self.controller.helm.steer_sign
        # "Leif" (pure full-azimuth learned mode) mirrors the mount sign too. Both
        # learned modes rescale their wide-azimuth steering to the boat's mechanical
        # range live from state.max_steer_angle_deg, so no azimuth sync is needed.
        leif = self.controller.modes.get(ControlModeName.ANCHOR_LEIF)
        if leif is not None and hasattr(leif, "steer_sign"):
            leif.steer_sign = self.controller.helm.steer_sign
        # Lateral-offset thrust-yaw feed-forward follows the geometry/trim live so
        # changing the offset (or the calibrated trim) updates compensation now.
        self.controller.helm.thrust_yaw_ff = _thrust_yaw_ff_norm(self.config)
        # Anchor mode caps thrust by the boat's top speed; keep it in step. The
        # vectored station-keeping law (#35) also mirrors the mount polarity so
        # a profile switch can't leave its azimuth mirrored.
        anchor = self.controller.modes.get(ControlModeName.ANCHOR_HOLD)
        if anchor is not None and hasattr(anchor, "config"):
            anchor.config.boat_max_speed_mps = b.max_speed_mps
            anchor.config.steer_sign = self.controller.helm.steer_sign
        # Waypoint mode's forward/reverse decision needs the boat's measured
        # speed, reverse efficiency, and turn rate.
        wp = self.controller.modes.get(ControlModeName.WAYPOINT)
        if wp is not None and hasattr(wp, "config"):
            wp.config.reverse_efficiency = b.reverse_efficiency
            wp.config.turn_rate_dps = b.max_turn_rate_deg
            wp.config.boat_speed_mps = b.max_speed_mps

        # Rebuild the live physics params so mass/thrust/geometry changes bite.
        self._rebuild_boat_physics()

    def _rebuild_boat_physics(self) -> None:
        """Swap the simulator boat's physics params for freshly-built ones.

        The Fossen model precomputes its mass + damping matrices (and the
        derived surge drag / yaw inertia) in ``__post_init__``/``_build_matrices``
        from the params, so we replace ``params`` wholesale and re-derive rather
        than mutating fields in place. The simple kinematic model has no derived
        state, so swapping the dataclass is enough."""
        if self.simulator is None:
            return
        boat = self.simulator.boat
        params = _build_boat_params(self.config)
        boat.params = params
        # Re-derive the Fossen matrices for the new params (no-op for "simple").
        rebuild = getattr(boat, "_build_matrices", None)
        if callable(rebuild):
            rebuild()

    def update_boat(self, fields: dict) -> dict:
        """Update the boat profile and apply what can change live.

        Also persists the change back into the active named profile (#75) so the
        existing ``POST /api/boat`` path and the profile store stay in sync."""
        self._apply_boat_specs(fields)
        # Write the edited specs back into the active profile so they persist.
        if getattr(self, "boats", None) is not None:
            from .core.boat_profiles import specs_from_boat

            self.boats.save(self.boats.active_id, None, specs_from_boat(self.config.boat))
        logger.info("boat profile updated: %s", fields)
        return self.boat_profile()

    # ------------------------------------------------------------------ #
    # Named boat profiles (#75)
    # ------------------------------------------------------------------ #
    def boat_profiles_list(self) -> dict:
        """``{active_id, profiles:[{id,name,...specs}, ...]}``."""
        return self.boats.to_dict()

    def boat_profiles_create(self, name: str, specs: dict | None = None) -> dict:
        """Create a profile (specs default to the current active boat). Returns
        ``{id, ...}`` of the new profile."""
        from .core.boat_profiles import specs_from_boat

        if specs is None:
            specs = specs_from_boat(self.config.boat)
        pid = self.boats.create(name, specs)
        return self.boats.get(pid) or {"id": pid}

    def boat_profiles_update(
        self, profile_id: str, name: str | None = None, specs: dict | None = None
    ) -> dict | None:
        """Update a profile's name/specs. If the edited profile is the active
        one, also apply the new specs live. Returns the updated profile or None
        if the id is unknown."""
        if not self.boats.save(profile_id, name, specs):
            return None
        if profile_id == self.boats.active_id:
            active = self.boats.active()
            if active is not None:
                self._apply_boat_specs(active["specs"])
            self._apply_active_boat_gains()
        return self.boats.get(profile_id)

    def boat_profiles_activate(self, profile_id: str) -> dict | None:
        """Make a profile active and apply its specs to the live sim. Returns
        the applied boat profile dict, or None if the id is unknown."""
        if not self.boats.set_active(profile_id):
            return None
        active = self.boats.active()
        if active is not None:
            self._apply_boat_specs(active["specs"])
        # Apply this profile's saved gains on top of its specs (else keep current).
        self._apply_active_boat_gains()
        logger.info("activated boat profile %s", profile_id)
        return self.boat_profile()

    def boat_profiles_delete(self, profile_id: str) -> bool:
        """Delete a profile (refuses the last one). If the deleted profile was
        active, apply whatever profile is active afterwards."""
        if not self.boats.delete(profile_id):
            return False
        active = self.boats.active()
        if active is not None:
            self._apply_boat_specs(active["specs"])
        self._apply_active_boat_gains()
        return True

    # ------------------------------------------------------------------ #
    # Per-boat saved gain profiles (#31)
    # ------------------------------------------------------------------ #
    def _load_boat_gains(self) -> dict:
        """Read the per-profile gains sidecar (``boat_gains.json``); ``{}`` when
        absent or unreadable, so a missing/corrupt file never breaks startup."""
        try:
            with open(self._boat_gains_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_boat_gains_file(self) -> None:
        """Persist the per-profile gains map atomically."""
        os.makedirs(self.config.data_dir, exist_ok=True)
        tmp = self._boat_gains_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._boat_gains, fh, indent=2)
        os.replace(tmp, self._boat_gains_path)

    def current_gains(self) -> dict:
        """Snapshot the live controller gains as a gains block (the shape stored
        per boat profile). Covers the helm heading PID, anchor/cruise/drift gains
        and the steering-gain schedule."""
        c = self.controller
        block: dict = {
            "heading": {
                "kp": c.helm.pid.kp,
                "ki": c.helm.pid.ki,
                "kd": c.helm.pid.kd,
            },
            "cruise": {"kp": c.cruise_pid.kp, "ki": c.cruise_pid.ki},
        }
        anchor = c.modes.get(ControlModeName.ANCHOR_HOLD)
        if anchor is not None and hasattr(anchor, "config"):
            block["anchor"] = {
                "kp": anchor.config.kp,
                "kd": anchor.config.kd,
                "idle_deadband_m": anchor.config.idle_deadband_m,
            }
        drift = c.modes.get(ControlModeName.DRIFT)
        if drift is not None and hasattr(drift, "pid"):
            block["drift"] = {"kp": drift.pid.kp, "ki": drift.pid.ki}
        sched = c.helm.gain_schedule
        if sched is not None:
            block["steer_schedule"] = {
                "sog_lo_kn": sched.sog_lo_kn,
                "sog_hi_kn": sched.sog_hi_kn,
                "mult_lo": sched.mult_lo,
                "mult_hi": sched.mult_hi,
                "mult_min": sched.mult_min,
                "mult_max": sched.mult_max,
            }
        return block

    def _apply_gains_block(self, gains: dict) -> None:
        """Apply a (partial) gains block to the live controllers. Missing
        sections/fields are left untouched, so a profile can carry just the gains
        it cares about."""
        if not isinstance(gains, dict):
            return
        c = self.controller
        h = gains.get("heading")
        if isinstance(h, dict):
            for attr in ("kp", "ki", "kd"):
                if h.get(attr) is not None:
                    setattr(c.helm.pid, attr, float(h[attr]))
            c.helm.pid.reset()
        a = gains.get("anchor")
        anchor = c.modes.get(ControlModeName.ANCHOR_HOLD)
        if isinstance(a, dict) and anchor is not None and hasattr(anchor, "config"):
            for attr in ("kp", "kd", "idle_deadband_m"):
                if a.get(attr) is not None:
                    setattr(anchor.config, attr, float(a[attr]))
        cr = gains.get("cruise")
        if isinstance(cr, dict):
            for attr in ("kp", "ki"):
                if cr.get(attr) is not None:
                    setattr(c.cruise_pid, attr, float(cr[attr]))
            c.cruise_pid.reset()
        dr = gains.get("drift")
        drift = c.modes.get(ControlModeName.DRIFT)
        if isinstance(dr, dict) and drift is not None and hasattr(drift, "pid"):
            for attr in ("kp", "ki"):
                if dr.get(attr) is not None:
                    setattr(drift.pid, attr, float(dr[attr]))
            drift.pid.reset()
        s = gains.get("steer_schedule")
        sched = c.helm.gain_schedule
        if isinstance(s, dict) and sched is not None:
            for attr in ("sog_lo_kn", "sog_hi_kn", "mult_lo", "mult_hi",
                         "mult_min", "mult_max"):
                if s.get(attr) is not None:
                    setattr(sched, attr, float(s[attr]))
        logger.info("applied boat gains: %s", gains)

    def _apply_active_boat_gains(self) -> None:
        """Apply the active profile's saved gains (if any) to the controllers."""
        if getattr(self, "boats", None) is None:
            return
        gains = self._boat_gains.get(self.boats.active_id)
        if gains:
            self._apply_gains_block(gains)

    def save_boat_gains(self, profile_id: str | None = None) -> dict:
        """Persist the CURRENTLY-applied controller gains into a boat profile
        (defaults to the active one), closing the "persist applied gains back to
        a config file" debt. Returns the saved gains block."""
        if getattr(self, "boats", None) is None:
            return {}
        pid = profile_id or self.boats.active_id
        block = self.current_gains()
        self._boat_gains[pid] = block
        self._save_boat_gains_file()
        logger.info("saved applied gains into boat profile %s", pid)
        return block

    def boat_gains(self, profile_id: str | None = None) -> dict:
        """Return the saved gains block for a profile (active one by default), or
        ``{}`` if that profile carries none."""
        pid = profile_id or (
            self.boats.active_id if getattr(self, "boats", None) is not None else ""
        )
        return dict(self._boat_gains.get(pid, {}))

    # ------------------------------------------------------------------ #
    # Versioned backup / restore of all persistent state
    # ------------------------------------------------------------------ #
    def create_backup(self, client: dict | None = None, *, created_at: str | None = None) -> bytes:
        """Build a versioned backup ZIP of this runtime's ``data_dir`` (boats,
        depth map, devices, trips) plus the UI's ``client`` localStorage slice.

        ``created_at`` is an ISO8601 string the caller supplies (the endpoint
        passes the request time); when omitted we use the injected clock to make
        a UTC timestamp -- the backup module itself never calls ``datetime.now``.
        Returns the raw ``.zip`` bytes."""
        from .core import backup

        if created_at is None:
            created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._now_fn()))
        return backup.create_backup(
            self.config.data_dir, client=client, created_at=created_at
        )

    def restore_backup(self, zip_bytes: bytes) -> dict:
        """Restore a backup ZIP into ``data_dir`` and reload what it can LIVE.

        Extracts the archive (overwriting the on-disk files), then refreshes the
        in-memory state it can without a restart: re-loads the boat profiles +
        the depth map from disk and re-applies the active profile, and reloads
        the device config. Anything that can't be refreshed live sets
        ``restart_required``. Returns the backup-module result dict plus
        ``restart_required``. Raises :class:`ValueError` (-> 400) on a bad zip."""
        from .core import backup

        result = backup.restore_backup(self.config.data_dir, zip_bytes)
        restart_required = False

        # Boat profiles: rebuild the store from the restored boats.json and
        # re-apply the active profile so the live physics follow it.
        try:
            from .core.boat_profiles import BoatProfileStore

            self.boats = BoatProfileStore(self.config.data_dir)
            active = self.boats.active()
            if active is not None:
                self._apply_boat_specs(active["specs"])
            # Per-boat gains (#31) live in a sidecar; reload + re-apply too.
            self._boat_gains = self._load_boat_gains()
            self._apply_active_boat_gains()
        except Exception:  # pragma: no cover - defensive
            logger.exception("restore: reloading boat profiles failed")
            restart_required = True

        # Depth map: reload the restored soundings from disk.
        try:
            self.depth_map = DepthMap()
            self.depth_map.load(self._depth_map_path, self._depth_chart_path)
            self._depth_saved_n = len(self.depth_map.points)
        except Exception:  # pragma: no cover - defensive
            logger.exception("restore: reloading depth map failed")
            restart_required = True

        # Safety geometry (#23): rebuild the store from the restored safety.json
        # and re-apply it to the live governor + refresh prefs.
        try:
            from .core.prefs import PrefsStore, SafetyGeometryStore

            self.safety_geometry = SafetyGeometryStore(self.config.data_dir)
            self._apply_safety_geometry()
            self.prefs = PrefsStore(self.config.data_dir)
        except Exception:  # pragma: no cover - defensive
            logger.exception("restore: reloading safety geometry failed")
            restart_required = True

        # Device config: re-read the restored devices.json into the live config
        # and rebuild the device set (no restart). reload_devices is async, so
        # schedule it; if there's no running loop, defer to a restart.
        apply_device_overrides(self.config)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.reload_devices())
        except RuntimeError:
            # No event loop (e.g. a synchronous restore in a test) -> the new
            # device config will take effect on the next start/restart.
            restart_required = True

        result["restart_required"] = restart_required
        logger.info("backup restored (restart_required=%s)", restart_required)
        return result

    # ------------------------------------------------------------------ #
    # Device / hardware config (persisted, editable over the API)
    # ------------------------------------------------------------------ #
    # The valid values for each source field (used to validate edits + to tell
    # the UI which options to offer). Sensors share one set; the motor adds
    # "both" (drive the sim boat AND mirror to a real servo).
    _SENSOR_SOURCES = ("sim", "serial", "nmea", "none")
    _MOTOR_SOURCES = ("sim", "serial", "both", "none")
    # Per-channel split sources: "both" makes no sense for an individual channel
    # (it is a combined concept); serial channels are Task 3 (placeholders only).
    _CHANNEL_SOURCES = ("sim", "serial", "none")
    # Battery is the registry-driven 4th device kind (#42): the built-in "sim" +
    # "none" baselines plus any registered/pack battery driver (e.g. "ina226").
    _BATTERY_SOURCES = ("sim", "none")

    def _battery_sources(self) -> tuple:
        """Built-in battery sources + any registered driver sources (e.g.
        ``ina226``), discovered from the registry so a pack driver needs no edit
        here."""
        from .hardware import registry
        return self._BATTERY_SOURCES + tuple(registry.sources("battery"))

    def _compass_sources(self) -> tuple:
        """Built-in compass sources + any registered driver sources (e.g.
        ``hwt901b``). Registered drivers are discovered from the plugin registry,
        so a new compass driver adds itself here without editing this file."""
        from .hardware import registry
        return self._SENSOR_SOURCES + tuple(registry.sources("compass"))

    def _gps_sources(self) -> tuple:
        """Built-in GPS sources + any registered GPS driver sources (e.g.
        ``ublox`` = the UBX M9N driver)."""
        from .hardware import registry
        return self._SENSOR_SOURCES + tuple(registry.sources("gps"))

    def list_serial_ports(self) -> list[dict]:
        """Bindable serial ports for the device-config UI to suggest.

        For each device we surface BOTH ways to bind it, so the user can pick
        what suits: the **stable** ``/dev/serial/by-id/...`` symlink (survives
        reboots + replugging -- recommended) AND the raw ``/dev/ttyUSB0`` path
        (simple, but the kernel can renumber it). Each entry is
        ``{path, description, stable}``, stable (by-id) first. Best-effort: falls
        back to a glob if pyserial is unavailable, and never raises."""
        import glob
        import os
        import re

        # A path is STABLE if it's a /dev/serial/by-id | by-path symlink (USB) or a
        # /dev/serialN alias (the Pi's on-board GPIO UART) -- all survive reboots.
        stable_re = re.compile(r"^/dev/serial(/|\d+$)")

        def _onboard(dev: str) -> bool:
            b = os.path.basename(dev)
            return b.startswith(("ttyAMA", "ttyS", "ttyO", "ttymxc", "ttySC")) \
                or dev.startswith("/dev/serial")

        # (path, description) candidates. STABLE links first so they win the dedup
        # below; then pyserial's richly-described USB ports; then a broad glob of
        # on-board UART + USB device nodes pyserial may not enumerate.
        candidates: list[tuple[str, str]] = []
        for pat in ("/dev/serial/by-id/*", "/dev/serial[0-9]", "/dev/serial/by-path/*"):
            for link in sorted(glob.glob(pat)):
                try:
                    target = os.path.basename(os.path.realpath(link))
                except OSError:
                    target = os.path.basename(link)
                tag = " - on-board UART" if target.startswith(("ttyAMA", "ttyS", "ttyO", "ttymxc")) else ""
                candidates.append((link, f"{target}{tag} (stable)"))
        try:
            from serial.tools import list_ports
            for p in list_ports.comports():
                desc = (p.description or "").strip()
                if not desc or desc == "n/a":
                    desc = os.path.basename(p.device)
                if _onboard(p.device) and "UART" not in desc:
                    desc += " - on-board UART"
                candidates.append((p.device, desc))
        except Exception:  # noqa: BLE001 - pyserial absent -> the glob below covers it
            pass
        for pat in ("/dev/ttyAMA[0-9]*", "/dev/ttyS[0-9]*", "/dev/ttyO[0-9]*",
                    "/dev/ttymxc[0-9]*", "/dev/ttySC[0-9]*", "/dev/ttyUSB[0-9]*",
                    "/dev/ttyACM[0-9]*", "/dev/tty.*", "/dev/cu.*"):  # last two: macOS
            for dev in sorted(glob.glob(pat)):
                tag = " - on-board UART" if _onboard(dev) else ""
                candidates.append((dev, os.path.basename(dev) + tag))

        out: list[dict] = []
        seen: set[str] = set()
        for path, desc in candidates:
            if path and path not in seen:
                seen.add(path)
                out.append({"path": path, "description": desc,
                            "stable": bool(stable_re.match(path))})
        out.sort(key=lambda e: (not e["stable"], e["path"]))
        return out

    # -- fusion calibration (still-capture system-ID; see nav.calibration) --- #
    def fusion_calibration(self) -> dict:
        """Saved calibration + live capture status (for GET)."""
        from .nav.calibration import interference_recommendations
        capturing, samples, seconds = self.navigator.capture_status()
        cal = self._fusion_cal
        score = cal.motor_interference_score if cal else None
        return {
            "calibration": cal.to_dict() if cal else None,
            "capturing": capturing,
            "capture_samples": samples,
            "capture_seconds": seconds,
            "enabled": self.navigator.fusion is not None,
            "recommendations": interference_recommendations(score),
            # experimental motor-interference remedy state
            "interference_comp_enabled": bool(cal.interference_comp_enabled) if cal else False,
            "has_interference_model": bool(cal and cal.motor_interference_slope is not None),
        }

    def set_interference_compensation(self, enabled: bool) -> dict:
        """EXPERIMENTAL: toggle the real-time motor-interference heading remedy
        (needs an interference calibration to have any effect)."""
        from .nav.calibration import FusionCalibration, save_calibration
        cal = self._fusion_cal or FusionCalibration()
        cal.interference_comp_enabled = bool(enabled)
        save_calibration(self.config.data_dir, cal)
        self._fusion_cal = cal
        self.navigator.apply_calibration(cal)
        return {"ok": True, "enabled": bool(enabled),
                "has_model": cal.motor_interference_slope is not None}

    def start_fusion_capture(self, mode: str = "still") -> dict:
        from .nav.calibration import CAPTURE_MODES
        if self.navigator.fusion is None:
            return {"ok": False, "error": "fusion is disabled"}
        if mode not in CAPTURE_MODES:
            return {"ok": False, "error": f"unknown mode {mode!r}"}
        self._capture_mode = mode
        self.navigator.start_capture()
        return {"ok": True, "capturing": True, "mode": mode}

    def stop_fusion_capture(self) -> dict:
        from .nav.calibration import tune
        buf = self.navigator.stop_capture()
        if buf is None:
            return {"ok": False, "error": "no capture was running"}
        from .nav.calibration import interference_recommendations
        mode = getattr(self, "_capture_mode", "still")
        cal, warnings = tune(buf, mode)
        out = {"ok": True, "mode": mode, "calibration": cal.to_dict(), "warnings": warnings}
        if mode == "interference":
            out["recommendations"] = interference_recommendations(cal.motor_interference_score)
        return out

    def save_fusion_calibration(self, data: dict) -> dict:
        from .nav.calibration import FusionCalibration, save_calibration
        # Merge into the existing calibration so each capture mode updates only
        # what it measured (still -> gains, align -> offset, ...).
        incoming = FusionCalibration.from_dict(data)
        merged = (self._fusion_cal or FusionCalibration()).merged_with(incoming)
        save_calibration(self.config.data_dir, merged)
        self._fusion_cal = merged
        self.navigator.apply_calibration(merged)
        return {"ok": True}

    def reset_fusion_calibration(self) -> dict:
        from .nav.calibration import FusionCalibration, clear_calibration
        clear_calibration(self.config.data_dir)
        self._fusion_cal = None
        self.navigator.apply_calibration(FusionCalibration())
        return {"ok": True}

    def device_config(self) -> dict:
        """Current device/hardware config + the selectable options.

        Shape matches what :meth:`set_device_config` persists, plus ``options``
        (for the UI's selects) and ``restart_required`` (always ``False`` on a
        plain read; a POST returns ``True`` because devices are rebuilt only on
        restart, not hot-swapped)."""
        return {
            "hardware": asdict(self.config.hardware),
            "nmea_tcp": asdict(self.config.nmea_tcp),
            "sim_motor": asdict(self.config.sim_motor),  # actuation shaping (#36)
            "options": {
                "sensor": list(self._SENSOR_SOURCES),
                "gps": list(self._gps_sources()),
                "compass": list(self._compass_sources()),
                "motor": list(self._MOTOR_SOURCES),
                "battery": list(self._battery_sources()),
                # Per-channel split sources ("both" is a combined concept, not a
                # per-channel one; split channels use sim | serial | none).
                "steering": list(self._CHANNEL_SOURCES),
                "thrust": list(self._CHANNEL_SOURCES),
            },
            "menus": self._device_menus(),
            "driver_menus": self._driver_menus(),
            "restart_required": False,
        }

    def sim_motor_config(self) -> dict:
        """Current simulated-motor actuation-shaping config (#36) as a plain dict.

        A small standalone reader kept separate from :meth:`device_config` so the
        (frozen) device-config response shape is unchanged; the values are still
        editable through :meth:`set_device_config`'s ``sim_motor`` block."""
        return asdict(self.config.sim_motor)

    def set_device_config(self, payload: dict) -> dict:
        """Validate, persist, and apply a device-config edit.

        ``payload`` is ``{"hardware": {...}, "nmea_tcp": {...},
        "sim_motor": {...}}`` (every key optional). Validates source values +
        field types, writes ``devices.json``, and updates the in-memory
        ``config.hardware`` / ``config.nmea_tcp`` / ``config.sim_motor`` so a
        subsequent read reflects it. Hardware/NMEA devices are NOT hot-swapped
        (the change applies on the next restart, so ``restart_required`` is
        ``True``); the ``sim_motor`` actuation shaping (#36) IS applied live to
        the running simulated motor when there is one. Raises :class:`ValueError`
        on a bad payload (the endpoint maps it to a 400)."""
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        hw_in = payload.get("hardware") or {}
        nmea_in = payload.get("nmea_tcp") or {}
        motor_in = payload.get("sim_motor") or {}
        if not isinstance(hw_in, dict) or not isinstance(nmea_in, dict) or not isinstance(motor_in, dict):
            raise ValueError("'hardware', 'nmea_tcp' and 'sim_motor' must be objects")

        # Build validated copies off the *current* config (so an edit can be
        # partial). Sources: sensors sim|serial|nmea, motor sim|serial|both.
        hw = HardwareConfig(**asdict(self.config.hardware))
        for dev in ("gps", "compass", "depth"):
            key = f"{dev}_source"
            if dev == "compass":
                allowed = self._compass_sources()
            elif dev == "gps":
                allowed = self._gps_sources()
            else:
                allowed = self._SENSOR_SOURCES
            if hw_in.get(key) is not None and hw_in[key] not in allowed:
                raise ValueError(
                    f"{key} must be one of {allowed} (got {hw_in[key]!r})"
                )
        if hw_in.get("motor_source") is not None and hw_in["motor_source"] not in self._MOTOR_SOURCES:
            raise ValueError(
                f"motor_source must be one of {self._MOTOR_SOURCES} (got {hw_in['motor_source']!r})"
            )
        # Per-channel split sources (steering / thrust).
        for ch in ("steering", "thrust"):
            key = f"{ch}_source"
            if hw_in.get(key) is not None and hw_in[key] not in self._CHANNEL_SOURCES:
                raise ValueError(
                    f"{key} must be one of {self._CHANNEL_SOURCES} (got {hw_in[key]!r})"
                )
        batt_allowed = self._battery_sources()
        if hw_in.get("battery_source") is not None and hw_in["battery_source"] not in batt_allowed:
            raise ValueError(
                f"battery_source must be one of {batt_allowed} (got {hw_in['battery_source']!r})"
            )
        # Ports are strings; baudrate is an int. Coerce/validate via the merge.
        for key in ("gps_port", "compass_port", "motor_port",
                    "steering_port", "thrust_port"):
            if key in hw_in and hw_in[key] is not None and not isinstance(hw_in[key], str):
                raise ValueError(f"{key} must be a string")
        for key, src in (("baudrate", hw_in), ("port", nmea_in)):
            if key in src and src[key] is not None:
                try:
                    int(src[key])
                except (TypeError, ValueError):
                    raise ValueError(f"{key} must be an integer") from None
        # Per-device serial framing: baud (int), bytesize 5-8, parity N/E/O/M/S,
        # stopbits 1/1.5/2. Normalise parity to an upper-case letter.
        for dev in ("gps", "compass", "motor", "steering", "thrust"):
            baud = hw_in.get(f"{dev}_baud")
            if baud is not None:
                try:
                    if int(baud) <= 0:
                        raise ValueError
                except (TypeError, ValueError):
                    raise ValueError(f"{dev}_baud must be a positive integer") from None
            bs = hw_in.get(f"{dev}_bytesize")
            if bs is not None and int(bs) not in (5, 6, 7, 8):
                raise ValueError(f"{dev}_bytesize must be 5, 6, 7 or 8")
            par = hw_in.get(f"{dev}_parity")
            if par is not None:
                par = str(par).upper()
                if par not in ("N", "E", "O", "M", "S"):
                    raise ValueError(f"{dev}_parity must be one of N/E/O/M/S")
                hw_in[f"{dev}_parity"] = par
            sb = hw_in.get(f"{dev}_stopbits")
            if sb is not None and float(sb) not in (1.0, 1.5, 2.0):
                raise ValueError(f"{dev}_stopbits must be 1, 1.5 or 2")

        # Sim-motor actuation shaping (#36): non-negative floats.
        for key in ("reverse_delay_s", "thrust_slew_per_s", "thrust_lag_tau_s"):
            if key in motor_in and motor_in[key] is not None:
                try:
                    if float(motor_in[key]) < 0.0:
                        raise ValueError
                except (TypeError, ValueError):
                    raise ValueError(f"sim_motor.{key} must be a non-negative number") from None

        nmea = NmeaTcpConfig(**asdict(self.config.nmea_tcp))
        motor = SimMotorConfig(**asdict(self.config.sim_motor))
        _merge_into(hw, hw_in)
        _merge_into(nmea, nmea_in)
        _merge_into(motor, motor_in)
        # _merge_into keeps the current value on a present-but-null field (right
        # for ports/baud), but for the SOURCE fields null is a real value: "Auto"
        # (follow mode). Apply those explicitly so selecting Auto actually resets
        # a source that was set to sim/serial/none.
        for k in ("gps_source", "compass_source", "depth_source", "motor_source",
                  "battery_source", "steering_source", "thrust_source"):
            if k in hw_in:
                setattr(hw, k, hw_in[k])

        # Validate the proposed motor link plan BEFORE persisting: catch any
        # same-port framing conflicts and surface them as a 400 (ValueError).
        from .hardware.link_plan import plan_motor_links
        try:
            plan_motor_links(hw)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        save_device_overrides(self.config.data_dir, hw, nmea, motor)
        # Reflect the edit in the live config so a subsequent GET shows it.
        self.config.hardware = hw
        self.config.nmea_tcp = nmea
        self.config.sim_motor = motor
        # Apply the shaping to the LIVE sim motor immediately (no restart): the
        # simulated motor exposes ``configure``; a real/tee motor doesn't, so this
        # is a safe no-op off-sim.
        sim_motor = getattr(self.simulator, "motor", None)
        if sim_motor is not None and hasattr(sim_motor, "configure"):
            sim_motor.configure(
                reverse_delay_s=motor.reverse_delay_s,
                thrust_slew_per_s=motor.thrust_slew_per_s,
                thrust_lag_tau_s=motor.thrust_lag_tau_s,
            )
        logger.info("device config updated: %s", payload)
        # Hardware/NMEA devices rebuild on the next start; sim_motor applied live.
        return {"ok": True, "restart_required": True}

    # --- GPS baud capacity constants (used for the link-saturation warning) ---
    # Assume RMC + GGA per fix; each sentence is ≤ 82 bytes; 10 bits per byte
    # (UART: 8 data + 1 start + 1 stop, no parity at these rates).
    _GPS_BYTES_PER_SENTENCE: int = 82
    _GPS_SENTENCES_PER_FIX: int = 2
    _BAUD_WARN_FRACTION: float = 0.70   # warn when estimated load exceeds 70 %

    def _build_serial_gps(self, cfg: AppConfig):
        from .hardware.serial_devices import SerialGps
        from .hardware.serial_link import PySerialTransport
        hw = cfg.hardware
        baud = hw.gps_baud
        gps_hz = cfg.sensors.gps_hz
        required_bps = (
            gps_hz
            * self._GPS_SENTENCES_PER_FIX
            * self._GPS_BYTES_PER_SENTENCE
            * 10  # bits per byte (UART framing)
        )
        capacity_bps = baud * self._BAUD_WARN_FRACTION
        if required_bps > capacity_bps:
            logger.warning(
                "gps_baud too low for %.0f Hz — expect growing fix lag; raise "
                "gps_baud (need ~%d bit/s, %.0f%% of %d baud). Set gps_baud: "
                "38400 (or higher) in your hardware config.",
                gps_hz,
                int(required_bps),
                100.0 * required_bps / baud,
                baud,
            )
        return SerialGps(PySerialTransport(
            hw.gps_port, baudrate=baud, bytesize=hw.gps_bytesize,
            parity=hw.gps_parity, stopbits=hw.gps_stopbits), self.bus)

    def _device_menus(self) -> list:
        """Collect device-specific menus (settings/actions) from the active
        devices that expose ``device_menu()`` -- surfaced to the UI so a driver
        can offer its own controls (e.g. the HWT901B compass)."""
        out: list = []
        for dev in (self.gps, self.compass, self.depth_sounder):
            fn = getattr(dev, "device_menu", None)
            if callable(fn):
                try:
                    out.append(fn())
                except Exception as exc:  # noqa: BLE001 - a bad menu can't break config
                    logger.warning("device_menu failed: %s", exc)
        return out

    def _driver_menus(self) -> dict:
        """Per-source device-menu SCHEMAS from registered drivers, with any saved
        settings overlaid -- so the UI can render a device's menu the moment its
        source is selected, before any instance exists. Keyed by source name."""
        out: dict = {}
        saved_all = self.config.hardware.device_settings or {}
        for kind in ("compass",):  # device kinds with pluggable driver menus
            for src, schema in registry.menus(kind).items():
                out[src] = _overlay_menu_values(schema, saved_all.get(kind, {}))
        return out

    def _device_by_kind(self, kind: str):
        return {"gps": self.gps, "compass": self.compass,
                "depth": self.depth_sounder}.get(kind)

    def apply_device_setting(self, kind: str, key: str, value) -> dict:
        """Persist a device-menu setting for ``kind`` and apply it live if the
        device is running. Persisted settings are read when the device is
        (re)built, so a choice sticks even when the device isn't active yet."""
        fn = getattr(self._device_by_kind(kind), "apply_setting", None)
        known = any(
            key in {s.get("key") for s in menu.get("settings", [])}
            for menu in registry.menus(kind).values()
        )
        if not known and not callable(fn):
            return {"ok": False, "message": f"no settings for device {kind!r}"}
        ds = self.config.hardware.device_settings
        ds.setdefault(kind, {})[key] = value
        try:
            save_device_overrides(self.config.data_dir, self.config.hardware,
                                  self.config.nmea_tcp, self.config.sim_motor)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"could not save: {exc}"}
        # Apply to the running device too, if there is one that accepts it.
        live = fn(key, value) if callable(fn) else None
        applied_live = bool(live and live.get("ok"))
        return {"ok": True, "saved": True, "applied_live": applied_live,
                "restart_required": not applied_live}

    def run_device_action(self, kind: str, name: str, params: dict | None = None) -> dict:
        """Run a device-menu action on the active device of ``kind``."""
        fn = getattr(self._device_by_kind(kind), "run_action", None)
        if not callable(fn):
            return {"ok": False, "message": f"no actions for device {kind!r}"}
        try:
            return fn(name, params or {})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": str(exc)}

    def _build_serial_compass(self, cfg: AppConfig):
        from .hardware.serial_devices import SerialCompass
        from .hardware.serial_link import PySerialTransport
        hw = cfg.hardware
        return SerialCompass(PySerialTransport(
            hw.compass_port, baudrate=hw.compass_baud, bytesize=hw.compass_bytesize,
            parity=hw.compass_parity, stopbits=hw.compass_stopbits), self.bus)

    def _build_serial_motor(self, cfg: AppConfig):
        from .hardware.serial_devices import SerialMotorController
        from .hardware.serial_link import PySerialTransport
        hw = cfg.hardware
        return SerialMotorController(PySerialTransport(
            hw.motor_port, baudrate=hw.motor_baud, bytesize=hw.motor_bytesize,
            parity=hw.motor_parity, stopbits=hw.motor_stopbits))

    def _build_split_channel(
        self,
        name: str,
        link: dict | None,
        sim_motor,
        sim_state: "_SimChannelState | None",
        cfg: AppConfig,
    ):
        """Build one split motor channel; returns ``None`` on failure (Constraint 4).

        ``name`` is "thrust" or "steering"; ``link`` is the resolved channel
        link dict from :func:`~vanchor.hardware.link_plan.plan_motor_links`
        (``None`` or ``source=="none"`` -> not connected).  A build exception is
        caught, logged, and surfaced as ``None`` so the other channel can still
        start up.
        """
        if link is None or link["source"] == "none":
            return None
        try:
            src = link["source"]
            if src == "sim":
                if sim_motor is None:
                    logger.warning(
                        "split %s channel needs a sim motor but none was created; "
                        "add a sim-capable device to the config", name)
                    return None
                if sim_state is None:
                    return None  # should not happen; guard anyway
                if name == "thrust":
                    return _SimThrustChannel(sim_motor, sim_state)
                return _SimSteeringChannel(sim_motor, sim_state)
            elif src == "both":
                # tee-per-channel (drive sim boat AND a physical board on the same
                # axis) is out of scope for Task 3. A combined "both" config uses
                # _TeeMotor in _construct_devices and never reaches this path; a
                # genuinely split "both" config downgrades to sim-only here.
                logger.warning(
                    "split %s channel: source 'both' (tee-per-channel) is not yet "
                    "implemented; downgrading to sim-only", name)
                if sim_motor is None:
                    logger.warning(
                        "split %s channel needs a sim motor but none was created; "
                        "add a sim-capable device to the config", name)
                    return None
                if sim_state is None:
                    return None  # should not happen; guard anyway
                if name == "thrust":
                    return _SimThrustChannel(sim_motor, sim_state)
                return _SimSteeringChannel(sim_motor, sim_state)
            elif src == "serial":
                from .hardware.serial_channels import (
                    SerialSteeringChannel,
                    SerialThrustChannel,
                )
                from .hardware.serial_link import PySerialTransport
                transport = PySerialTransport(
                    link["port"],
                    baudrate=link["baud"],
                    bytesize=link["bytesize"],
                    parity=link["parity"],
                    stopbits=link["stopbits"],
                )
                if name == "thrust":
                    return SerialThrustChannel(transport)
                # v2.1: the channel speaks DEGREES on the wire; the one scale
                # constant (max_steer_angle_deg) converts the normalized command.
                return SerialSteeringChannel(
                    transport, full_scale_deg=cfg.boat.max_steer_angle_deg)
            else:
                logger.warning(
                    "unknown source %r for split %s channel; skipping", src, name)
                return None
        except Exception as exc:  # noqa: BLE001 — Constraint 4: never crash startup
            logger.warning(
                "split %s channel could not be built (%s); running without it", name, exc)
            return None

    def _construct_devices(self, cfg: AppConfig) -> dict:
        """Build the device set (simulator + sensors + motor) for ``cfg.hardware``.
        Returns a dict and does NOT mutate ``self`` — so a live reload can build
        the new set first and swap it in only on success (see reload_devices)."""
        from .hardware.link_plan import plan_motor_links
        src = {n: cfg.hardware.source(n) for n in ("gps", "compass", "depth", "motor")}
        # Resolve the motor channel plan (pure, unit-testable). This determines
        # whether to build one combined controller or two independent channels.
        plan = plan_motor_links(cfg.hardware)

        # The sim boat exists whenever any device is simulated (sensors read its
        # truth; the sim motor drives it). Check both the legacy src and the new
        # per-channel links so a split config with one sim channel also creates
        # the simulator.
        _split_needs_sim = (
            plan.kind == "split" and any(
                ch is not None and ch["source"] in ("sim", "both")
                for ch in (plan.thrust, plan.steering)
            )
        )
        simulator = None
        if any(s in ("sim", "both") for s in src.values()) or _split_needs_sim:
            # On a LIVE reload, carry over the current boat state so a device
            # change doesn't teleport the simulated boat back to the start.
            prev = getattr(self, "simulator", None)
            start_state = (
                prev.truth() if prev is not None
                else BoatState(point=GeoPoint(cfg.sim.start_lat, cfg.sim.start_lon), heading_deg=0.0)
            )
            simulator = Simulator(
                start=start_state,
                params=_build_boat_params(cfg),
                environment=self._environment,
                physics_hz=cfg.sim.physics_hz,
                time_scale=cfg.sim.time_scale,
                model=cfg.sim.model,
                battery_config=_build_battery_config(cfg),
                # Actuation shaping (#36): default-zero => transparent passthrough.
                motor_reverse_delay_s=cfg.sim_motor.reverse_delay_s,
                motor_thrust_slew_per_s=cfg.sim_motor.thrust_slew_per_s,
                motor_thrust_lag_tau_s=cfg.sim_motor.thrust_lag_tau_s,
            )
        from .hardware.interfaces import NullMotor
        sim_motor = simulator.motor if simulator is not None else None

        if plan.kind == "combined":
            # --- COMBINED path (legacy-identical) ----------------------------
            # Every legacy config (channel keys unset) ALWAYS takes this path
            # (Constraint 3). The four cases below reproduce today's exact builds.
            if plan.source == "none":
                # Motor "Not connected": a safe no-op so the loop runs; motor
                # modes are disabled (see vanchor.core.capabilities).
                motor = NullMotor()
            elif plan.source == "serial":
                motor = self._build_serial_motor(cfg)
            elif plan.source == "both":
                motor = _TeeMotor([sim_motor, self._build_serial_motor(cfg)])
            else:
                # "sim" or any unrecognised source -> sim fallback, exactly
                # as today (the plan passes the source string through verbatim).
                motor = sim_motor if sim_motor is not None else NullMotor()
            # If one channel source is "none" while the other rides the shared
            # board, wrap the combined motor so the disabled field is always sent
            # at neutral (0.0).  Only applies when neutral_channel is set; the
            # plain combined path (no neutral_channel) is never wrapped.
            if plan.neutral_channel:
                motor = _NeutralChannelMotor(motor, plan.neutral_channel)
        else:
            # --- SPLIT path --------------------------------------------------
            # Two independent channels, each guarded (Constraint 4).
            from .hardware.split_motor import SplitMotor
            sim_state = _SimChannelState() if sim_motor is not None else None
            thrust_ch = self._build_split_channel(
                "thrust", plan.thrust, sim_motor, sim_state, cfg)
            steering_ch = self._build_split_channel(
                "steering", plan.steering, sim_motor, sim_state, cfg)
            motor = SplitMotor(thrust=thrust_ch, steering=steering_ch)
        # "nmea" (or anything not sim/serial) builds NO internal sensor: the
        # navigator is fed by external NMEA over the bridge/inject instead.
        gps = compass = depth = None
        if src["gps"] == "serial":
            gps = self._build_serial_gps(cfg)
        elif src["gps"] == "sim":
            # Multipath jitter profile (measured off a real stationary M9N indoors).
            _jitter = {"indoor": dict(walk_sigma_m=5.5, walk_tau_s=40.0,
                                      vel_bias_sigma_mps=0.35, vel_tau_s=8.0,
                                      reported_hacc_m=15.0)}.get(cfg.sensors.gps_jitter, {})
            # Sensor cadences are PER SIM-SECOND: scale by time_scale so a
            # sped-up sim doesn't starve the navigator of fixes relative to the
            # physics (sim-vs-real review 2026-07-15). At 1x this is a no-op;
            # the control loop itself still runs wall-clock, so time_scale != 1
            # remains a visualization tool, never a control-quality yardstick.
            _ts = max(0.01, cfg.sim.time_scale)
            gps = SimGps(simulator.truth, self.bus, update_hz=cfg.sensors.gps_hz * _ts,
                         position_noise_m=cfg.sensors.gps_noise_m,
                         emit_velocity=cfg.sensors.gps_velocity, **_jitter)
        elif registry.has("gps", src["gps"]):
            # A pluggable GPS driver (e.g. the UBX "ublox" M9N). Build eagerly but
            # resiliently -- a failure must not crash startup (mirrors compass).
            try:
                gps = registry.build_device("gps", src["gps"], self, cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "gps source %r could not be built (%s); running without GPS. "
                    "Change it in Settings -> Devices.", src["gps"], exc)
                gps = None
        if src["compass"] == "serial":
            compass = self._build_serial_compass(cfg)
        elif src["compass"] == "sim":
            # Deterministic sea-state model (#38) drives the sim IMU; Hs<=0 (the
            # default) leaves the flat-water IMU bit-for-bit unchanged.
            from .sim.sea_state import SeaState
            compass = SimCompass(simulator.truth, self.bus, update_hz=cfg.sensors.compass_hz * max(0.01, cfg.sim.time_scale),
                                 heading_noise_deg=cfg.sensors.compass_noise_deg,
                                 sea_state=SeaState.from_config(cfg.sea_state))
        elif registry.has("compass", src["compass"]):
            # A pluggable driver builds eagerly (may open a port / import an
            # optional lib), so a failure here must NOT crash startup -- skip it,
            # log why, and leave the UI reachable to fix the config (mirrors the
            # serial "unopenable device" resilience). The warning shows in
            # Settings -> View logs.
            try:
                compass = registry.build_device("compass", src["compass"], self, cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "compass source %r could not be built (%s); running without a "
                    "compass. Change it in Settings -> Devices.", src["compass"], exc)
                compass = None
        if src["depth"] == "sim":
            depth = SimDepthSounder(
                simulator.truth,
                Bathymetry(origin=GeoPoint(cfg.sim.start_lat, cfg.sim.start_lon)),
                self.bus, update_hz=cfg.sensors.depth_hz * max(0.01, cfg.sim.time_scale))
        battery_monitor = self._build_battery_monitor(cfg, simulator)
        return {"simulator": simulator, "gps": gps, "compass": compass,
                "depth_sounder": depth, "motor": motor,
                "battery_monitor": battery_monitor}

    def _driver_context(self, kind: str, source: str, config):
        """Build the NARROW, versioned capability object (#43) a pluggable driver
        is constructed with — publish a reading, report health, read its own
        config, a logger/clock, and coarse boat motion. Deliberately carries NO
        reference to the runtime, the motor, or the safety governor, so a driver
        (or a community pack) can never reach STOP/the deadman/the failsafes
        through it (see docs/community-plan.md — the safety floor is never a pack
        concern)."""
        def motion():
            st = getattr(self, "state", None)
            if st is None or st.fix is None:
                return None
            return (st.fix.cog_deg, st.sog_knots * 0.514444)  # knots -> m/s

        return registry.DriverContext(
            kind=kind, source=source, config=config,
            _bus=self.bus, _now=self._now_fn, _motion=motion,
        )

    def _build_battery_monitor(self, cfg: AppConfig, simulator):
        """Build the battery monitor (#42) — the reference registry-driven 4th
        device kind. ``sim`` presents the simulator's integrated pack (the
        baseline, identical telemetry to before); any other source is a pluggable
        driver built through the versioned capability API (#43). A driver that
        can't be built (missing lib, no hardware) is skipped with a warning — the
        rest of the boat still runs — mirroring the compass-driver resilience.

        Default: ``sim`` when a simulated boat exists (unchanged behaviour), else
        ``none`` (a real battery monitor is not implied by enabling serial GPS/
        compass/motor)."""
        source = cfg.hardware.battery_source or ("sim" if simulator is not None else "none")
        if source in ("none", None):
            return None
        if source == "sim":
            if simulator is None:
                return None
            from .hardware.drivers.battery import SimBatteryMonitor
            return SimBatteryMonitor(simulator.battery)
        if registry.uses_context("battery", source):
            try:
                ctx = self._driver_context("battery", source, cfg.battery)
                return registry.build_with_context("battery", source, ctx)
            except Exception as exc:  # noqa: BLE001 - a bad driver must not crash startup
                logger.warning(
                    "battery source %r could not be built (%s); running without a "
                    "battery monitor. Change it in Settings -> Devices.", source, exc)
                return None
        if registry.has("battery", source):  # legacy (runtime, cfg) driver
            try:
                return registry.build_device("battery", source, self, cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("battery source %r could not be built (%s).", source, exc)
                return None
        logger.warning("unknown battery source %r; running without a battery monitor.", source)
        return None

    async def reload_devices(self) -> dict:
        """Rebuild the device set LIVE (no process restart) so a device-config
        change applies immediately. Builds + starts the NEW set first, and only
        stops the old + swaps in if that succeeds — so a bad serial port leaves
        the current devices running and the autopilot uninterrupted. Returns
        ``{applied: bool, error?: str}``."""
        try:
            new = self._construct_devices(self.config)
        except Exception as exc:  # e.g. a serial port that doesn't exist
            logger.exception("device reload: build failed")
            return {"applied": False, "error": str(exc)}
        started: list = []
        try:
            for d in (new["gps"], new["compass"], new["depth_sounder"],
                      new["battery_monitor"]):
                if d is not None:
                    await d.start()
                    started.append(d)
            # Open the NEW motor before swapping it in: otherwise the first
            # flush() on a serial motor raises on the unopened port. If it fails
            # we roll back below without ever touching the running motor.
            await _start_motor(new["motor"])
            new_sim_task = (asyncio.ensure_future(new["simulator"].run())
                            if new["simulator"] is not None else None)
        except Exception as exc:
            logger.exception("device reload: start failed; rolling back")
            for d in started:
                with contextlib.suppress(Exception):
                    await d.stop()
            await _stop_motor(new["motor"])
            return {"applied": False, "error": str(exc)}
        # New set is live — now retire the old one and swap references.
        for d in (self.gps, self.compass, self.depth_sounder,
                  getattr(self, "battery_monitor", None)):
            if d is not None:
                with contextlib.suppress(Exception):
                    await d.stop()
        if self.simulator is not None:
            self.simulator.stop()
        if self._sim_task is not None:
            self._sim_task.cancel()
        self.gps, self.compass, self.depth_sounder = new["gps"], new["compass"], new["depth_sounder"]
        self.battery_monitor = new["battery_monitor"]
        self.simulator, self._sim_task = new["simulator"], new_sim_task
        # Swap the motor in, then stop the OLD one (closes its port + kills the
        # feedback task -> no port/task leak). Best-effort so a stubborn old
        # motor can't strand the reload.
        old_motor = self.controller.motor
        self.controller.motor = new["motor"]
        self.controller.device_connected = self._device_connected_map(self.config)
        await _stop_motor(old_motor)
        # Re-prime the navigator with a fresh fix/heading so the fix-lost failsafe
        # doesn't latch (and stop the motor) over the brief gap during the swap.
        for dev in (self.gps, self.compass):
            if dev is not None:
                with contextlib.suppress(Exception):
                    self.navigator.handle_sentence(dev.sample())
        logger.info("device config applied live")
        return {"applied": True}

    # ------------------------------------------------------------------ #
    # Commands (from the UI).
    # ------------------------------------------------------------------ #
    async def _record_nmea(self, sentence: str) -> None:
        if self.debug.active:
            self.debug.write("nmea", sentence, time.time())

    # ------------------------------------------------------------------ #
    # Debug session recording + replay
    # ------------------------------------------------------------------ #
    def start_debug(self, name: str | None = None) -> dict:
        nm = name or "session-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time()))
        return self.debug.start(nm, time.time())

    def stop_debug(self) -> dict:
        return self.debug.stop()

    def start_replay(self, file_name: str) -> bool:
        path = self.debug.path_for(file_name)
        if path is None:
            return False
        return self.replay.load(path, time.time())

    def stop_replay(self) -> None:
        self.replay.stop()

    # ------------------------------------------------------------------ #
    # Command audit log (#26)
    # ------------------------------------------------------------------ #
    def record_command(
        self, ctype: object, source: str, outcome: str, detail: str | None = None
    ) -> None:
        """Append one command to the bounded audit ring (#26).

        ``source`` is "helm"|"observer"|"rest"; ``outcome`` is
        "accepted"|"denied"|"error" (+ an optional short ``detail`` on error).
        Called from the command entry points (WS handler + REST /api/command).
        Pings (and typeless messages) are never recorded so the audit stays a
        log of real commands. Uses the wall clock so the timestamp is displayable.
        """
        if ctype in (None, "", "ping"):
            return
        entry = {
            "ts": self._now_fn(),
            "type": str(ctype),
            "source": source,
            "outcome": outcome,
        }
        if detail:
            entry["detail"] = str(detail)[:200]
        self._command_audit.append(entry)

    def command_audit(self, n: int = 50) -> dict:
        """Return the most recent ``n`` audited commands, oldest first / newest
        last. ``n`` is clamped to [1, 200] (the ring size)."""
        n = max(1, min(int(n), 200))
        return {"commands": list(self._command_audit)[-n:]}

    def handle_command(self, command: dict) -> None:
        ctype = command.get("type")
        if self.debug.active and ctype not in (None,):
            self.debug.write("command", command, time.time())
        if ctype == "set_environment" and self.simulator is not None:
            env = self.simulator.environment
            for key in (
                "current_speed",
                "current_dir",
                "wind_speed",
                "wind_dir",
                "gust_amplitude_mps",
                "wind_variability",
                "current_variability",
            ):
                if key in command:
                    setattr(env, key, float(command[key]))
            # Re-anchor the slow weather wander on the new base values.
            self.simulator.set_weather_base()
            self._save_environment()
        elif ctype == "weather_preset" and self.simulator is not None:
            self._apply_weather_preset(str(command.get("id", "")))
        elif ctype == "sim_fault":
            self.set_sim_fault(
                str(command.get("name", "")),
                bool(command.get("enabled", True)),
                **{k: v for k, v in command.items()
                   if k not in ("type", "name", "enabled")},
            )
        elif ctype == "teleport":
            self._teleport(command)
        elif ctype == "inject_nmea":
            asyncio.ensure_future(self.bus.publish(events.NMEA_IN, str(command["sentence"])))
        elif ctype == "set_gps_offset":
            # On a SIMULATED GPS, "adjust my position" must MOVE the boat, not
            # install an offset: the sim GPS has no bias to correct, so an
            # offset would shift the PERCEIVED frame away from the sim truth —
            # the depth sounder keeps sampling truth, and chart-relative
            # behaviours (contour follow, charted depth, divergence) then run
            # displaced by exactly the offset (field report: contour followed
            # "at its original unadjusted position"). Real GPS sources keep the
            # normal offset calibration — there the offset corrects a real
            # bias, so perception and physics align.
            if type(self.gps).__name__ == "SimGps" and self.simulator is not None:
                self.navigator.clear_gps_offset()
                self._teleport({
                    "lat": float(command["true_lat"]),
                    "lon": float(command["true_lon"]),
                })
                logger.info("gps offset on sim GPS -> teleported the sim boat instead")
            else:
                self.navigator.set_gps_offset(
                    float(command["true_lat"]), float(command["true_lon"])
                )
        elif ctype == "clear_gps_offset":
            self.navigator.clear_gps_offset()
        elif ctype == "set_land_guard":
            gov = self.controller.safety
            enabled = command.get("enabled")
            margin = command.get("margin_m")
            if enabled is not None:
                gov.config.land_guard_enabled = bool(enabled)
            if margin is not None:
                gov.config.land_guard_margin_m = max(1.0, min(200.0, float(margin)))
            self.safety_geometry.set_land_guard(
                None if enabled is None else bool(enabled),
                None if margin is None else gov.config.land_guard_margin_m,
            )
            logger.info("land guard: enabled=%s margin=%.0fm",
                        gov.config.land_guard_enabled,
                        gov.config.land_guard_margin_m)
        elif ctype == "set_auto_apb":
            enabled = bool(command.get("enabled", False))
            self.config.safety.auto_follow_apb = enabled
            self.safety_geometry.set_auto_follow_apb(enabled)
            logger.info("auto Follow-APB %s", "enabled" if enabled else "disabled")
        elif ctype == "load_route":
            self._load_route(command)
        elif ctype == "set_battery":
            self._set_battery(command.get("soc_pct"))
        elif ctype == "return_to_launch":
            self.return_to_launch()
        elif ctype == "trip_start":
            self.trip_start(command.get("name"))
        elif ctype == "trip_stop":
            self.trip_stop()
        elif ctype in ("set_nogo_zones", "set_min_depth", "set_fix_failsafe"):
            # Safety geometry: update the live governor (controller) AND persist
            # to the server-side store so it survives a restart (#23). The
            # governor stays the authority; we mirror its resulting state (for
            # min-depth/failsafe) and the raw command rings (for no-go zones,
            # which the governor keeps only as prepared shapely polygons).
            #
            # Safety-floor lockout (#50): a runtime Settings edit may make the
            # failsafes SAFER but never weaker -- clamp a disable / a lowered
            # min-depth back to the startup floor BEFORE it reaches the governor,
            # so the persisted mirror below also stores the floored value.
            if ctype == "set_min_depth":
                command = {
                    **command,
                    "min_depth_m": self.safety_floor.enforce_min_depth(
                        command.get("min_depth_m", 0.0)
                    ),
                }
            elif ctype == "set_fix_failsafe":
                command = {
                    **command,
                    "enabled": self.safety_floor.enforce_fix_failsafe(
                        command.get("enabled", False)
                    ),
                }
            self.controller.handle_command(command)
            if ctype == "set_nogo_zones":
                self.safety_geometry.set_nogo_zones(command.get("zones", []))
            elif ctype == "set_min_depth":
                self.safety_geometry.set_min_depth(self.controller.safety.config.min_depth_m)
            else:
                self.safety_geometry.set_fix_failsafe(
                    self.controller.safety.config.fix_failsafe_enabled
                )
        else:
            self.controller.handle_command(command)

    # ------------------------------------------------------------------ #
    # Sim fault injection (#37)
    # ------------------------------------------------------------------ #
    # Fault names -> (device attribute, device-level fault name). The prefix
    # selects which simulated sensor is degraded; the sensor's ``set_fault``
    # applies it. Kept as data so the set is easy to extend/introspect.
    _SIM_FAULTS: dict = {
        "gps_dropout": ("gps", "dropout"),
        "gps_eof": ("gps", "eof"),
        "gps_glitch": ("gps", "glitch"),
        "gps_garbage": ("gps", "garbage"),
        "nmea_garbage": ("gps", "garbage"),  # alias
        "gps_latency": ("gps", "latency"),
        "baud_saturation": ("gps", "latency"),  # alias
        "compass_freeze": ("compass", "freeze"),
        "compass_garbage": ("compass", "garbage"),
    }

    def set_sim_fault(self, name: str, enabled: bool = True, **params) -> dict:
        """Toggle a simulated-sensor fault at runtime (roadmap #37).

        ``name`` is one of :attr:`_SIM_FAULTS` (e.g. ``"gps_dropout"``,
        ``"nmea_garbage"``, ``"compass_freeze"``, ``"baud_saturation"``).
        Extra kwargs are passed through to the device (e.g. ``glitch_m``,
        ``latency_s``). **Guarded**: a no-op returning ``{"applied": False, ...}``
        whenever the simulator isn't running or the target isn't a simulated
        device (so hitting the trigger on real hardware can never degrade it).
        Returns ``{"applied": bool, "name": str, "enabled": bool}`` (plus a
        ``reason`` when it was a no-op)."""
        if self.simulator is None:
            return {"applied": False, "name": name, "reason": "no simulator"}
        target = self._SIM_FAULTS.get(name)
        if target is None:
            return {"applied": False, "name": name, "reason": "unknown fault"}
        attr, fault = target
        device = getattr(self, attr, None)
        setter = getattr(device, "set_fault", None)
        if not callable(setter):
            return {"applied": False, "name": name,
                    "reason": f"{attr} is not a simulated device"}
        ok = setter(fault, enabled, **params)
        if ok:
            logger.info("sim fault %s -> %s (%s)", name, enabled, params or "")
        return {"applied": bool(ok), "name": name, "enabled": bool(enabled)}

    def _teleport(self, command: dict) -> None:
        """Sim teleport (#90): instantly snap the simulated boat's ground truth to
        a new ``lat``/``lon`` (and optional ``heading``), zeroing its velocity so
        it doesn't keep coasting. A safe no-op on real hardware (no simulator)."""
        if self.simulator is None:
            logger.info("teleport ignored (no simulator)")
            return
        heading = command.get("heading")
        self.simulator.teleport(
            float(command["lat"]),
            float(command["lon"]),
            float(heading) if heading is not None else None,
        )
        # Re-prime the GPS spike-guard so the next fix at the new spot snaps
        # straight through instead of being rejected as a position jump.
        guard = self.navigator.guard
        guard._last_point = None
        guard._pending_point = None

    def _set_battery(self, soc_pct: object) -> None:
        """Set/reset the battery state-of-charge (#60). Sim-only: on real
        hardware the SOC comes from a battery monitor over the HAL."""
        if soc_pct is None or self.simulator is None:
            logger.info("set_battery ignored (no value or no sim battery)")
            return
        self.simulator.battery.set_soc(float(soc_pct))
        logger.info("battery SOC set to %.0f%%", float(soc_pct))

    # ------------------------------------------------------------------ #
    # Return-to-Launch (#61)
    # ------------------------------------------------------------------ #
    def return_to_launch(self) -> dict:
        """Plan a water route from the boat to its launch point and follow it,
        anchoring on arrival.

        Returns the plan result dict. Synchronous + CPU/IO-heavy (water fetch +
        routing); call it from an executor on the live path.
        """
        launch = self.state.launch
        if launch is None:
            return {"ok": False, "waypoints": [], "message": "No launch point set."}
        result = self.plan_route(launch.lat, launch.lon, mode="fastest")
        if not result.get("ok") or not result.get("waypoints"):
            return result
        self.state.waypoints = [
            Waypoint(name=str(w.get("name", "WP")), point=GeoPoint(w["lat"], w["lon"]))
            for w in result["waypoints"]
        ]
        self.controller.handle_command({"type": "load_route", "on_arrival": "anchor"})
        # load_route doesn't take on_arrival; set it explicitly on the state.
        self.state.route_on_arrival = "anchor"
        logger.info("Return-to-Launch engaged: %d waypoints home", len(self.state.waypoints))
        return result

    # ------------------------------------------------------------------ #
    # Trip log (#66)
    # ------------------------------------------------------------------ #
    def trip_start(self, name: str | None = None) -> dict:
        """Manually start a trip (overrides/replaces any active one)."""
        trip = self.trip.start(name, self._now_fn())
        return self.trip.snapshot(self._now_fn())

    def trip_stop(self) -> dict:
        """Manually stop + persist the active trip. No-op when none is active."""
        self.trip.stop(self._now_fn())
        return self.trip.snapshot(self._now_fn())

    def trip_list(self) -> list[dict]:
        return self.trip.list_trips()

    def trip_get(self, trip_id: str) -> dict | None:
        return self.trip.get_trip(trip_id)

    def trip_gpx(self, trip_id: str) -> str | None:
        return self.trip.gpx(trip_id)

    def trip_delete(self, trip_id: str) -> bool:
        return self.trip.delete_trip(trip_id)

    # ------------------------------------------------------------------ #
    # Battery (#60)
    # ------------------------------------------------------------------ #
    def battery_snapshot(self) -> dict:
        """Battery telemetry. From the active battery monitor (#42) — the sim
        pack or a real shunt driver — falling back to the sim battery directly,
        then to zeros when there is no battery source at all."""
        if getattr(self, "battery_monitor", None) is not None:
            return self.battery_monitor.snapshot()
        if self.simulator is not None:
            return self.simulator.battery.to_dict()
        return {
            "soc_pct": 0.0,
            "voltage_v": 0.0,
            "current_a": 0.0,
            "draw_w": 0.0,
            "range_m": 0.0,
            "time_to_empty_s": None,
        }

    # ------------------------------------------------------------------ #
    # Lost-connection failsafe (#64) + RTL auto-recommend (#61)
    # ------------------------------------------------------------------ #
    def client_connected(self) -> None:
        """A UI client connected; clear any link failsafe."""
        self._ui_clients += 1
        self._last_client_seen = self._mono_fn()
        if self._link_failsafe_engaged:
            logger.info("UI client reconnected; link failsafe cleared")
        self._link_failsafe_engaged = False
        self._link_failsafe_action = None

    def client_activity(self) -> None:
        """Mark the link alive (any inbound client traffic)."""
        self._last_client_seen = self._mono_fn()

    def client_disconnected(self) -> None:
        """A UI client disconnected."""
        self._ui_clients = max(0, self._ui_clients - 1)
        self._last_client_seen = self._mono_fn()

    def _underway(self) -> bool:
        """True when the boat is actively making way and a lost link must be
        caught -- i.e. NOT idle. Every guided/cruising mode counts, plus MANUAL
        while the operator is actually commanding thrust (driving by hand): a
        client loss there must not leave the boat motoring on forever (#64).
        Station-keeping anchor-hold is excluded (it is already holding)."""
        if self.state.mode in _UNDERWAY_MODES:
            return True
        if self.state.mode == ControlModeName.MANUAL:
            return abs(self.state.motor_command.thrust) > _MANUAL_UNDERWAY_THRUST_EPS
        return False

    def evaluate_link_failsafe(self, now: float | None = None) -> bool:
        """Engage the lost-link failsafe if no UI client has been seen for the
        timeout while underway. In a guided mode this holds position
        (anchor-hold); driving MANUALLY it STOPS (zero thrust) -- there is no
        target to hold to, so the safe action is to cut the motor. Returns True
        if it engaged on this call. Idempotent and clock-injectable (pass the
        MONOTONIC ``now`` in tests)."""
        if now is None:
            now = self._mono_fn()
        timeout = self.config.safety.link_loss_timeout_s
        connected = self._ui_clients > 0
        if connected or self._last_client_seen is None or self._link_failsafe_engaged:
            return False
        if not self._underway():
            return False
        if now - self._last_client_seen < timeout:
            return False
        if self.state.mode == ControlModeName.MANUAL:
            # Driving by hand with the link gone -> cut the motor (STOP). This
            # deadman is part of the safety floor and is NOT configurable.
            logger.warning("link lost %.0fs while driving manually; STOP (zero thrust)", timeout)
            self.controller.handle_command({"type": "stop"})
            self._link_failsafe_action = "stop"
        elif self.config.safety.link_loss_continue_mission:
            # Unsupervised missions (the default: a locked phone must not park
            # an active route): keep flying the guided mode; geofence/depth/
            # battery failsafes still apply. Logged + latched (fires once).
            logger.warning("link lost %.0fs while underway; continuing mission "
                           "(safety.link_loss_continue_mission)", timeout)
            self._link_failsafe_action = "continue"
        else:
            # Guided mode with continue-mission off -> hold position here.
            logger.warning("link lost %.0fs while underway; engaging hold-position", timeout)
            self.controller.handle_command({"type": "anchor_hold"})
            self._link_failsafe_action = "hold"
        self._link_failsafe_engaged = True
        return True

    def evaluate_auto_apb(self, now: float | None = None) -> bool:
        """Auto-engage Follow-APB when an external autopilot's APB feed is live
        (opt-in, ``safety.auto_follow_apb``). Engages ONLY from idle MANUAL --
        never hijacks an anchor hold / route / a hand on the throttle. Latched:
        leaving the mode by hand isn't overridden until the feed has been
        silent for >10 s and returns. Returns True when it engaged."""
        if not self.config.safety.auto_follow_apb:
            self._auto_apb_latched = False
            return False
        if now is None:
            now = self._mono_fn()
        st = self.state
        fresh = (st.apb_received_mono is not None
                 and now - st.apb_received_mono < 10.0)
        if not fresh:
            self._auto_apb_latched = False       # feed gone -> re-arm
            return False
        if st.mode == ControlModeName.FOLLOW_APB:
            self._auto_apb_latched = True        # (covers manual engagement too)
            return False
        if self._auto_apb_latched:
            return False                          # user left the mode on purpose
        if st.mode != ControlModeName.MANUAL or abs(st.motor_command.thrust) > 0.05:
            return False
        logger.warning("APB feed detected -- auto-engaging Follow-APB "
                       "(safety.auto_follow_apb)")
        self.controller.handle_command({"type": "follow_apb"})
        self._auto_apb_latched = True
        return True

    def refresh_land_guard_water(self) -> bool:
        """Keep the safety governor supplied with the water chart around the
        boat for the land guard. CACHE-ONLY (the offline chart the routing
        features already store) — the safety path never touches the network.
        Re-checks every 20 s, or sooner when the boat leaves the loaded bbox.
        Returns True when a (new) chart was handed to the governor."""
        gov = self.controller.safety
        if not gov.config.land_guard_enabled:
            return False
        pos = self.state.position
        if pos is None or pos.is_null():
            return False
        now = self._mono_fn()
        bb = self._land_water_bbox
        inside = (bb is not None and
                  bb[0] <= pos.lat <= bb[2] and bb[1] <= pos.lon <= bb[3])
        if now < self._land_water_next and inside and gov.has_water_geometry:
            return False
        self._land_water_next = now + 20.0
        try:
            from .nav import water as _water
            bbox = _water.bbox_around(pos.lat, pos.lon, pos.lat, pos.lon,
                                      pad_m=1500.0)
            cached = _water.WaterCache(self.config.data_dir).find_covering(bbox)
        except Exception:  # noqa: BLE001 — chart lookup must never hurt safety
            logger.exception("land guard water lookup failed")
            return False
        if cached is None or cached.is_empty:
            return False
        # Shrink the re-query trigger box so we reload BEFORE the edge.
        south, west, north, east = bbox
        mlat = (north - south) * 0.25
        mlon = (east - west) * 0.25
        self._land_water_bbox = (south + mlat, west + mlon, north - mlat, east - mlon)
        gov.set_water_geometry(cached)
        logger.info("land guard: water chart loaded around the boat")
        return True

    def evaluate_rtl_recommend(self) -> bool:
        """Set ``state.rtl_recommended`` when the battery range has dropped to
        within ``rtl_margin_m`` of the distance home (so the boat can *just* make
        it back). If ``auto_rtl`` is set, engage RTL. Returns the new flag."""
        launch = self.state.launch
        pos = self.state.position
        if launch is None or pos is None or pos.is_null():
            self.state.rtl_recommended = False
            return False
        range_m = self.battery_snapshot().get("range_m", 0.0)
        if range_m <= 0.0:
            # No usable range estimate yet (boat not making way) -> don't prompt.
            self.state.rtl_recommended = False
            return False
        from .core.geo import haversine_m

        dist_home = haversine_m(pos, launch)
        recommend = range_m <= dist_home + self.config.safety.rtl_margin_m
        self.state.rtl_recommended = recommend
        if recommend and self.config.safety.auto_rtl and self.state.mode != ControlModeName.WAYPOINT:
            logger.warning("auto_rtl: battery range near distance-home; engaging RTL")
            self._schedule_auto_rtl()
        return recommend

    def _schedule_auto_rtl(self) -> None:
        """Engage auto-RTL WITHOUT blocking the event loop.

        ``return_to_launch`` -> ``plan_route`` is synchronous and CPU/IO-heavy
        (Overpass fetch, up to two 60 s timeouts) and documented as executor-only.
        Calling it inline from the periodic telemetry tick would stall every
        async loop, so run it in the default executor. A single in-flight guard
        stops the evaluator (called every telemetry tick) from launching a pile
        of duplicate concurrent RTL plans."""
        if self._rtl_in_flight:
            return
        self._rtl_in_flight = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (e.g. a unit test off the live path) -> preserve
            # the old inline behaviour rather than silently doing nothing.
            try:
                self.return_to_launch()
            finally:
                self._rtl_in_flight = False
            return
        asyncio.ensure_future(self._run_auto_rtl(loop))

    async def _run_auto_rtl(self, loop) -> None:
        """Run the heavy RTL plan+engage in an executor; always clear the
        in-flight flag so a failure can't wedge future auto-RTL attempts."""
        try:
            result = await loop.run_in_executor(None, self.return_to_launch)
            if isinstance(result, dict) and not result.get("ok", True):
                logger.warning("auto_rtl planning failed: %s", result.get("message"))
        except Exception:
            logger.exception("auto_rtl planning failed")
        finally:
            self._rtl_in_flight = False

    # ------------------------------------------------------------------ #
    # Low-battery thrust-derating ladder (#49)
    # ------------------------------------------------------------------ #
    def evaluate_battery_ladder(self) -> float:
        """Push a soft thrust cap into the governor from the battery SoC, and hand
        off to the existing RTL at the lowest stage. Returns the applied cap
        (1.0 = no derate).

        The cap is a magnitude-only ceiling, so STOP and every failsafe still take
        precedence and are never blocked. Only runs when there is a real battery
        reading (a simulated pack or a battery monitor); with no battery source
        the cap is left at 1.0 so a boat with no gauge is never spuriously derated
        by the zeros fallback in :meth:`battery_snapshot`."""
        ladder = self._battery_ladder
        gov = self.controller.safety
        if not ladder.enabled or (
            getattr(self, "battery_monitor", None) is None and self.simulator is None
        ):
            gov.set_thrust_cap(1.0)
            return 1.0
        soc = self.battery_snapshot().get("soc_pct")
        if soc is None:
            gov.set_thrust_cap(1.0)
            return 1.0
        soc = float(soc)
        cap = ladder.cap_for(soc)
        gov.set_thrust_cap(cap)
        if ladder.at_rtl(soc):
            self._battery_rtl_handoff(soc)
        else:
            # Recovered above the RTL stage (e.g. a battery swap) -> re-arm.
            self._battery_rtl_engaged = False
        return cap

    def _battery_rtl_handoff(self, soc_pct: float) -> None:
        """At the lowest ladder stage, hand off to the EXISTING RTL/failsafe once.

        Idempotent via a one-shot flag (cleared when SoC recovers above the
        stage), and guarded so it only fires when a launch point exists to return
        to. The progressive derate above this stage still holds regardless."""
        if self._battery_rtl_engaged:
            return
        self._battery_rtl_engaged = True
        # Recommend-only unless the operator opted into autonomous RTL (#7): with
        # auto_rtl off the boat must NOT self-drive -- mirror evaluate_rtl_recommend
        # and only raise the low-battery RTL recommendation for the UI/alarm. The
        # progressive derate cap already applied above still stands.
        if not self.config.safety.auto_rtl:
            self.state.rtl_recommended = True
            logger.warning(
                "battery critically low (%.0f%%); recommending Return-to-Launch "
                "(auto_rtl off -- not self-driving)", soc_pct)
            return
        if self.state.launch is None:
            logger.warning(
                "battery critically low (%.0f%%) but no launch point recorded; "
                "holding the lowest derate cap (no RTL target)", soc_pct)
            return
        logger.warning(
            "battery critically low (%.0f%%); handing off to Return-to-Launch",
            soc_pct)
        self._schedule_auto_rtl()

    # ------------------------------------------------------------------ #
    # Periodic safety supervisor (1 Hz) + depth accumulation
    # ------------------------------------------------------------------ #
    def _supervise_once(self) -> None:
        """Run one periodic safety/bookkeeping pass -- the side effects that used
        to live in ``telemetry()`` (findings M2/H4/#7).

        Runs REGARDLESS of replay mode and connected-client count. Each step is
        isolated so a single failing evaluator can't stop the others (and, at the
        loop level, can't kill the supervisor)."""
        steps = (
            ("maybe_record_launch", self.controller.maybe_record_launch),
            ("evaluate_battery_ladder", self.evaluate_battery_ladder),
            ("evaluate_rtl_recommend", self.evaluate_rtl_recommend),
            ("evaluate_link_failsafe", self.evaluate_link_failsafe),
            ("evaluate_auto_apb", self.evaluate_auto_apb),
            ("refresh_land_guard_water", self.refresh_land_guard_water),
            ("trip_update", lambda: self.trip.update(
                self.state.position, self.state.sog_knots, self._now_fn())),
            # Hardware watchdog heartbeat (#44): pet the GPIO line every tick. If
            # the supervisor stalls this stops toggling -> the external relay
            # drops the motor supply. Last so a stalled step still lets it beat.
            ("watchdog_pump", self.watchdog.pump),
        )
        for name, step in steps:
            try:
                step()
            except Exception:  # noqa: BLE001 - one bad evaluator must not stop safety
                logger.exception("supervisor step %s failed; continuing", name)

    async def _run_supervisor(self, period_s: float = 1.0) -> None:
        """~1 Hz task driving the periodic safety evaluations + depth persistence.

        Exception-proof: the whole body is guarded so a raise (from a step or a
        save) only logs and continues -- the task NEVER exits on its own; it ends
        only on cancellation at shutdown."""
        while True:
            try:
                await asyncio.sleep(period_s)
                self._supervise_once()
                await self._maybe_persist_depth()
            except asyncio.CancelledError:
                raise  # shutdown -> let the cancellation propagate
            except Exception:  # noqa: BLE001 - supervisor must never die
                logger.exception("supervisor loop error -- will continue")

    def record_depth_sounding(self) -> None:
        """Accumulate one depth sounding at the boat's DRAWN position.

        Called by the WS broadcaster at the telemetry rate (~5 Hz) so soundings
        keep their original cadence now that ``telemetry()`` is a pure snapshot.
        A no-op during replay (replayed depth must not pollute the live map).

        Record each sounding at the SAME position the boat marker is drawn at, so
        the depth dots sit under the boat. In the sim the marker uses ground truth
        -- and the sounder samples the bottom at that true position too -- whereas
        the GPS fix carries noise that would offset the dots beside the boat. On
        real hardware there is no truth, so both use the GPS fix."""
        if self.replay.active:
            return
        sounding_pos = (
            self.simulator.truth().point
            if self.simulator is not None
            else self.state.position
        )
        # Sonar-vs-chart grounding-divergence alert (#45): compare the live
        # sounder depth against the charted depth at the boat BEFORE this sounding
        # is folded into the map (so the just-taken sample can't self-cancel the
        # comparison), then set the divergence state fields for telemetry/UI.
        self._update_depth_divergence(sounding_pos)
        self.depth_map.record(sounding_pos, self.state.depth_m)

    def _update_depth_divergence(self, position: "GeoPoint | None" = None) -> None:
        """Wire nav/sonar.py into the running app (#45): look up the charted depth
        from the ``DepthMap`` at the boat's position, compare it against the
        measured sounder depth (``state.depth_m``), and set the sonar/divergence
        state fields (``sonar_depth_m`` / ``charted_depth_m`` /
        ``depth_divergence_m`` / ``depth_divergence_alert``) so the shallow-side
        grounding alert can fire in telemetry.

        A clean no-op when there is no chart, no fix, or no live depth: a
        non-positive depth (lost bottom lock) or a null/absent position leaves the
        previous alert untouched rather than false-tripping."""
        from .nav import sonar

        state = self.state
        pos = position if position is not None else state.position
        depth = state.depth_m
        if pos is None or pos.is_null() or depth is None or float(depth) <= 0.0:
            return
        sonar.ingest(
            state, sonar.Sounding(depth_m=float(depth)), self.depth_map, position=pos
        )

    async def _maybe_persist_depth(self) -> None:
        """Checkpoint newly-accumulated soundings to disk OFF the event loop
        (finding M3), at most one save in flight at a time.

        ``depth_map.save`` does an atomic JSON write; on a large map that is a
        real blocking cost, so it runs in a worker thread. The in-flight guard
        stops the 1 Hz supervisor from stacking overlapping saves."""
        if self._depth_save_in_flight:
            return
        n = len(self.depth_map.points)
        if n - self._depth_saved_n < 25:
            return
        self._depth_save_in_flight = True
        try:
            await asyncio.to_thread(self.depth_map.save, self._depth_map_path)
            self._depth_saved_n = n
        except Exception:  # noqa: BLE001 - a failed checkpoint must not wedge saves
            logger.exception("depth map checkpoint failed")
        finally:
            self._depth_save_in_flight = False

    def _load_route(self, command: dict) -> None:
        from .nav.routes import parse_gpx

        try:
            if "gpx" in command:
                text = str(command["gpx"])
            elif "path" in command:
                with open(command["path"], "r", encoding="utf-8") as fh:
                    text = fh.read()
            else:
                logger.warning("load_route requires 'gpx' or 'path'")
                return
            waypoints = parse_gpx(text)
        except (ValueError, OSError) as exc:
            logger.warning("load_route failed: %s", exc)
            return
        self.state.waypoints = waypoints
        self.controller.handle_command(
            {"type": "load_route", "throttle": command.get("throttle")}
            if command.get("throttle") is not None
            else {"type": "load_route"}
        )
        logger.info("loaded route with %d waypoints", len(waypoints))

    # ------------------------------------------------------------------ #
    # Smart "Take me here" water routing (task #43)
    # ------------------------------------------------------------------ #
    def plan_route(
        self, dest_lat: float, dest_lon: float, mode: str = "fastest",
        offset_m: float = 25.0, depth_aware: bool = True,
    ) -> dict:
        """Plan a water-only route from the boat's current position.

        Synchronous and CPU/IO-heavy (Overpass fetch + shapely/networkx); the UI
        endpoint calls it in an executor. Returns the API contract dict. Does NOT
        start navigation.

        When ``depth_aware`` is set (default) and a ``min_depth_m`` safety
        threshold is configured, imported depth data (contours + soundings) is
        turned into a shallow no-go mask so the route proactively goes AROUND
        shoals instead of relying on the reactive shallow-stop. Falls back
        transparently to plain routing when there is no depth data.
        """
        from .nav import routing, water

        # Fresh plan: clear any stale cancel request so a normal plan runs.
        self._route_plan_cancelled = False
        pos = self.state.position
        if pos is not None and not pos.is_null():
            start_lat, start_lon = pos.lat, pos.lon
        else:
            start_lat, start_lon = self.config.sim.start_lat, self.config.sim.start_lon

        cache = water.WaterCache(self.config.data_dir)
        bbox = water.bbox_around(start_lat, start_lon, dest_lat, dest_lon)
        water_ll = cache.find_covering(bbox)
        if water_ll is None:
            try:
                elements = water.fetch_overpass(*bbox)
            except Exception as exc:  # network / endpoint failure
                logger.warning("water fetch failed: %s", exc)
                return {
                    "ok": False,
                    "waypoints": [],
                    "message": "No offline chart for this area; connect once to download it.",
                }
            water_ll = water.assemble_water(elements)
            if water_ll.is_empty:
                return {
                    "ok": False,
                    "waypoints": [],
                    "message": "No mapped water found around the route.",
                }
            try:
                cache.store(bbox, water_ll)
            except OSError as exc:  # pragma: no cover - disk failure
                logger.warning("water cache store failed: %s", exc)

        # Depth-aware routing: build a shallow no-go mask from imported depth
        # (contours + soundings) so routes avoid shoals by default. Cheap and
        # bounded (bbox-windowed, capped); yields None when no depth data exists,
        # in which case routing is byte-identical to before.
        avoid_shallow_ll = None
        min_depth_m = self.config.safety.min_depth_m
        if depth_aware and min_depth_m and min_depth_m > 0.0:
            try:
                # bbox is (south, west, north, east); depth windowing wants
                # (west, south, east, north).
                s, w, n, e = bbox
                avoid_shallow_ll = self.depth_map.shallow_polygons((w, s, e, n), min_depth_m)
            except Exception as exc:  # pragma: no cover - defensive; never block a plan
                logger.warning("shallow mask build failed: %s", exc)
                avoid_shallow_ll = None

        result = routing.plan_route(
            start_lat=start_lat,
            start_lon=start_lon,
            dest_lat=dest_lat,
            dest_lon=dest_lon,
            water_ll=water_ll,
            mode=mode,
            shoreline_offset_m=offset_m,
            cancelled=lambda: self._route_plan_cancelled,
            avoid_shallow_ll=avoid_shallow_ll,
        )
        return {
            "ok": result.ok,
            "waypoints": result.waypoints,
            "message": result.message,
        }

    def cancel_route_plan(self) -> None:
        """Request that an in-progress route plan abort ASAP (#54)."""
        self._route_plan_cancelled = True

    # ------------------------------------------------------------------ #
    # "Around island" loop route (#77)
    # ------------------------------------------------------------------ #
    def plan_island_loop(
        self, click_lat: float, click_lon: float, offset_m: float = 20.0
    ) -> dict:
        """Plan a closed loop route encircling the island under ``(lat, lon)``.

        Uses the same offline water chart/cache as :meth:`plan_route` (fetches
        once if not cached). The boat's current position (or the sim start)
        decides which water body is the basin. Does NOT start navigation -- it
        returns waypoints for the route editor. Synchronous + CPU/IO-heavy; the
        UI endpoint calls it in an executor. Returns
        ``{ok, waypoints, loop, message}``.
        """
        from .nav import routing, water

        pos = self.state.position
        if pos is not None and not pos.is_null():
            boat_lat, boat_lon = pos.lat, pos.lon
        else:
            boat_lat, boat_lon = self.config.sim.start_lat, self.config.sim.start_lon

        cache = water.WaterCache(self.config.data_dir)
        bbox = water.bbox_around(boat_lat, boat_lon, click_lat, click_lon)
        water_ll = cache.find_covering(bbox)
        if water_ll is None:
            try:
                elements = water.fetch_overpass(*bbox)
            except Exception as exc:  # network / endpoint failure
                logger.warning("water fetch failed: %s", exc)
                return {
                    "ok": False,
                    "waypoints": [],
                    "loop": True,
                    "message": "No offline chart for this area; connect once to download it.",
                }
            water_ll = water.assemble_water(elements)
            if water_ll.is_empty:
                return {
                    "ok": False,
                    "waypoints": [],
                    "loop": True,
                    "message": "No mapped water found around the island.",
                }
            try:
                cache.store(bbox, water_ll)
            except OSError as exc:  # pragma: no cover - disk failure
                logger.warning("water cache store failed: %s", exc)

        result = routing.plan_island_loop(
            click_lat,
            click_lon,
            water_ll,
            boat_lat=boat_lat,
            boat_lon=boat_lon,
            offset_m=offset_m,
        )
        return {
            "ok": result.ok,
            "waypoints": result.waypoints,
            "loop": result.loop,
            "message": result.message,
        }

    # ------------------------------------------------------------------ #
    # Area survey "map mode" route (#47)
    # ------------------------------------------------------------------ #
    def plan_survey(
        self, polygon_latlon: list, spacing_m: float, angle_deg: float | None = None
    ) -> dict:
        """Plan a boustrophedon coverage route over a closed area polygon.

        Pure CPU work (shapely); the UI endpoint calls it in an executor. Does
        NOT start navigation -- it returns waypoints for the route editor.
        """
        from .nav import survey, water as water_mod

        # Fetch the cached water polygon covering the drawn area so the survey
        # is clipped to water and connecting legs stay off land. No cached
        # water for the area -> plan against the polygon alone (survey.py still
        # repairs legs that exit the drawn polygon itself).
        water_geom = None
        try:
            lats = [float(p[0]) for p in polygon_latlon]
            lons = [float(p[1]) for p in polygon_latlon]
            if lats and lons:
                cache = water_mod.WaterCache(self.config.data_dir)
                bbox = water_mod.bbox_around(min(lats), min(lons), max(lats), max(lons))
                geom = cache.find_covering(bbox)
                if geom is not None and not geom.is_empty:
                    water_geom = geom
        except Exception as exc:  # noqa: BLE001 - clipping is best-effort
            logger.warning("survey water lookup failed (planning unclipped): %s", exc)

        try:
            result = survey.plan_survey(
                polygon_latlon, float(spacing_m), angle_deg, water=water_geom
            )
        except (ValueError, TypeError) as exc:
            logger.warning("survey plan failed: %s", exc)
            return {"ok": False, "waypoints": [], "message": f"Bad survey request: {exc}"}
        return {
            "ok": result.ok,
            "waypoints": result.waypoints,
            "message": result.message,
        }

    def plan_work_spots(self, polygon_latlon: list, spacing_m: float) -> dict:
        """Generate Work Area spots: an even serpentine grid over a drawn area,
        clipped to water (spots on land are dropped). Pure CPU (shapely) + the
        offline water cache; the UI endpoint calls it in an executor. Returns
        ``{ok, waypoints, message}`` -- the UI loads these as the Work Area spots."""
        from .nav import survey, water as water_mod
        from shapely.geometry import Point as _Pt

        try:
            result = survey.plan_work_spots(polygon_latlon, float(spacing_m))
        except (ValueError, TypeError) as exc:
            logger.warning("work-area spots failed: %s", exc)
            return {"ok": False, "waypoints": [], "message": f"Bad work-area request: {exc}"}
        if not result.ok:
            return {"ok": False, "waypoints": [], "message": result.message}

        # Clip spots to water (drop any over land) using the cached water polygon
        # for the area's bbox; if no water is cached, return the grid unclipped.
        wps = result.waypoints
        try:
            lats = [w["lat"] for w in wps]
            lons = [w["lon"] for w in wps]
            cache = water_mod.WaterCache(self.config.data_dir)
            bbox = water_mod.bbox_around(min(lats), min(lons), max(lats), max(lons))
            geom = cache.find_covering(bbox)
            if geom is not None and not geom.is_empty:
                proj = water_mod.Projection.for_point(
                    (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2
                )
                water_m = proj.to_metric(geom)
                if not water_m.is_valid:
                    water_m = water_m.buffer(0)
                kept = [
                    w for w in wps
                    if water_m.covers(_Pt(*proj.point_to_metric(w["lon"], w["lat"])))
                ]
                if kept:  # only apply the clip if something survives (else keep all)
                    wps = [dict(w, name=f"Spot {i + 1}") for i, w in enumerate(kept)]
        except Exception as exc:  # noqa: BLE001 - clipping is best-effort
            logger.warning("work-area water clip skipped: %s", exc)
        return {"ok": True, "waypoints": wps,
                "message": f"{len(wps)} work spots." if wps else result.message}

    def contour_route(self, lat: float, lon: float, window_m: float = 700.0) -> dict:
        """Build a route that follows the imported depth contour nearest
        (lat, lon), chaining same-depth pieces into a continuous track (a closed
        isobath comes back as a loop). Pure CPU (shapely); the UI endpoint calls it
        in an executor. Returns ``{ok, waypoints, depth_m, loop, message}`` -- the
        UI loads the waypoints as a route (patrol optional)."""
        from .nav import contour_route as cr

        dlat = window_m / 111_320.0
        dlon = window_m / (111_320.0 * max(0.1, math.cos(math.radians(lat))))
        bbox = (lon - dlon, lat - dlat, lon + dlon, lat + dlat)  # (w, s, e, n)
        contours = self.depth_map.contours_in(bbox=bbox)
        if not contours:
            return {"ok": False, "waypoints": [],
                    "message": "No depth contours loaded around there."}
        return cr.contour_route_near(lat, lon, contours)

    # ------------------------------------------------------------------ #
    # Offline chart prefetch + management (#52)
    # ------------------------------------------------------------------ #
    def prefetch_chart(self, bbox: list) -> dict:
        """Fetch + cache the water polygon for a bbox so the boat can route
        offline later. ``bbox`` is ``[south, west, north, east]``.

        Synchronous and IO-heavy (Overpass fetch); call it in an executor.
        Handles network failure gracefully.
        """
        from .nav import water

        try:
            south, west, north, east = (float(v) for v in bbox)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "cached": False,
                "vertices": 0,
                "message": "bbox must be [south, west, north, east].",
            }
        box = (south, west, north, east)
        cache = water.WaterCache(self.config.data_dir)
        existing = cache.find_covering(box)
        if existing is not None:
            return {
                "ok": True,
                "cached": True,
                "vertices": water._count_vertices(existing),
                "message": "Area already cached.",
            }
        try:
            elements = water.fetch_overpass(*box)
        except Exception as exc:  # network / endpoint failure
            logger.warning("chart prefetch fetch failed: %s", exc)
            return {
                "ok": False,
                "cached": False,
                "vertices": 0,
                "message": f"Could not download chart (offline?): {exc}",
            }
        water_ll = water.assemble_water(elements)
        if water_ll.is_empty:
            return {
                "ok": False,
                "cached": False,
                "vertices": 0,
                "message": "No mapped water found in that area.",
            }
        try:
            cache.store(box, water_ll)
        except OSError as exc:  # pragma: no cover - disk failure
            logger.warning("chart cache store failed: %s", exc)
            return {
                "ok": False,
                "cached": False,
                "vertices": water._count_vertices(water_ll),
                "message": f"Fetched chart but failed to cache it: {exc}",
            }
        return {
            "ok": True,
            "cached": True,
            "vertices": water._count_vertices(water_ll),
            "message": "Chart downloaded and cached for offline routing.",
        }

    def list_charts(self) -> dict:
        """List cached chart bboxes + on-disk sizes (for the UI to show/manage)."""
        import json as _json

        from .nav import water

        cache = water.WaterCache(self.config.data_dir)
        charts: list[dict] = []
        if cache.dir.exists():
            for meta_path in sorted(cache.dir.glob("*.json")):
                try:
                    meta = _json.loads(meta_path.read_text())
                    bbox = meta["bbox"]
                except (OSError, ValueError, KeyError):
                    continue
                wkb_path = meta_path.with_suffix(".wkb")
                size = wkb_path.stat().st_size if wkb_path.exists() else 0
                charts.append(
                    {
                        "bbox": bbox,
                        "vertices": meta.get("vertices", 0),
                        "size_bytes": size,
                    }
                )
        return {"charts": charts}

    def clear_charts(self) -> dict:
        """Delete every cached chart. Returns how many were removed."""
        from .nav import water

        cache = water.WaterCache(self.config.data_dir)
        removed = 0
        if cache.dir.exists():
            for path in list(cache.dir.glob("*.wkb")) + list(cache.dir.glob("*.json")):
                try:
                    path.unlink()
                    if path.suffix == ".wkb":
                        removed += 1
                except OSError as exc:  # pragma: no cover - disk failure
                    logger.warning("could not remove cached chart %s: %s", path, exc)
        return {"ok": True, "removed": removed, "message": f"Cleared {removed} cached chart(s)."}

    def _save_environment(self) -> None:
        """Persist the sim weather base so a restart resumes the same
        conditions (the Simulator panel's sliders otherwise looked set while
        the restarted sim ran calm)."""
        env = self._environment
        data = {k: float(getattr(env, k, 0.0)) for k in _ENV_PERSIST_KEYS}
        try:
            tmp = self._env_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
            os.replace(tmp, self._env_path)
        except OSError as exc:  # pragma: no cover - disk-full etc.
            logger.warning("could not persist %s: %s", self._env_path, exc)

    def _apply_weather_preset(self, preset_id: str) -> None:
        """Apply a named weather preset to the live sim environment."""
        from .sim.weather import WEATHER_PRESETS

        preset = WEATHER_PRESETS.get(preset_id)
        if preset is None or self.simulator is None:
            logger.warning("unknown weather preset: %r", preset_id)
            return
        env = self.simulator.environment
        env.current_speed = preset.current_speed
        env.current_dir = preset.current_dir
        env.wind_speed = preset.wind_speed
        env.wind_dir = preset.wind_dir
        env.gust_amplitude_mps = preset.gust_amplitude_mps
        env.wind_variability = preset.wind_variability
        env.current_variability = preset.current_variability
        self.simulator.set_weather_base()
        self._save_environment()
        logger.info("applied weather preset %r", preset_id)

    def apply_tuned_gains(self, job: str, params: dict, *, persist: bool = False) -> None:
        """Apply auto-tuned gains to the live controller (used by /api/tune).

        With ``persist=True`` the tuned gains are ALSO written into the active
        boat profile's saved gains (``boat_gains.json``), closing the "persist
        applied gains back to a config file" debt. It defaults to ``False`` so
        the existing ``POST /api/tune`` behaviour (live-apply only) is unchanged.
        """
        from .core.models import ControlModeName

        c = self.controller
        if job == "heading":
            c.helm.pid.kp = float(params["heading_kp"])
            c.helm.pid.kd = float(params["heading_kd"])
            c.helm.pid.reset()
        elif job == "cruise":
            c.cruise_pid.kp = float(params["kp"])
            c.cruise_pid.ki = float(params["ki"])
            c.cruise_pid.reset()
        elif job == "drift":
            pid = c.modes[ControlModeName.DRIFT].pid
            pid.kp = float(params["kp"])
            pid.ki = float(params["ki"])
            pid.reset()
        elif job == "anchor":
            cfg = c.modes[ControlModeName.ANCHOR_HOLD].config
            cfg.kp = float(params["kp"])
            cfg.kd = float(params["kd"])
            cfg.idle_deadband_m = float(params["idle_deadband_m"])
        logger.info("applied tuned %s gains live: %s", job, params)
        if persist:
            self._persist_tuned_gains(job, params)

    def _persist_tuned_gains(self, job: str, params: dict) -> None:
        """Merge an auto-tuned job's gains into the active boat profile's saved
        gains (only the section that job tuned) and persist them."""
        if getattr(self, "boats", None) is None:
            return
        from .analysis.tuning import gains_block_from_tuning

        frag = gains_block_from_tuning(job, params)
        if not frag:
            return
        pid = self.boats.active_id
        block = dict(self._boat_gains.get(pid, {}))
        block.update(frag)  # replace only the tuned section(s)
        self._boat_gains[pid] = block
        self._save_boat_gains_file()
        logger.info("persisted tuned %s gains into boat profile %s", job, pid)

    # ------------------------------------------------------------------ #
    # Depth-map gridding (server-side averaging for the depth overlay)
    # ------------------------------------------------------------------ #
    def depth_grid(self, cell_m: float = 15.0, bbox=None, field: str = "depth") -> dict:
        """Server-side gridded chart: bins soundings into ~``cell_m`` metre cells
        averaging the value per cell, so the UI can paint an averaged colour chart
        instead of 100k individual dots. ``cell_m`` is clamped to 2..200.

        ``bbox`` = (west, south, east, north) limits the grid to that viewport
        window (Tier-1 windowing) so a large chart only ships what's on screen.
        ``field`` selects the layer: ``"depth"`` (default) or ``"hardness"``
        (bottom-hardness, raw 0..127) -- same gridding, different source.

        Returns ``{ok, field, cell_m, min_depth, max_depth, count, cells}``; the
        chart changes slowly, so the UI polls this rather than the 5 Hz telemetry.
        """
        try:
            cell = float(cell_m)
        except (TypeError, ValueError):
            cell = 15.0
        cell = max(2.0, min(200.0, cell))
        source = self.depth_map.hardness if field == "hardness" else None
        grid = self.depth_map.as_grid(cell, bbox=bbox, source=source)
        grid["ok"] = True
        grid["field"] = field
        return grid

    def depth_at(self, lat: float, lon: float) -> dict:
        """Best-known depth at a point (nearest sounding within ~100 m, else the
        nearest imported contour vertex) for the map long-press menu.
        ``{ok, depth_m?, source?, dist_m?}``."""
        hit = self.depth_map.depth_at(lat, lon)
        if hit is None:
            return {"ok": False}
        return {"ok": True, **hit}

    def depth_contours(self, bbox=None, limit: int = 20000) -> dict:
        """Imported depth contours (isobath polylines) windowed to a
        (west, south, east, north) bbox. Returns ``{ok, count, contours}`` where
        each contour is ``{d: depth_m, pts: [[lat, lon], ...]}``."""
        cs = self.depth_map.contours_in(bbox=bbox, limit=limit)
        return {"ok": True, "count": len(cs), "contours": cs}

    def depth_composition(self, bbox=None, limit: int = 30000) -> dict:
        """Imported bottom-composition polygons, windowed to a
        (west, south, east, north) bbox. Returns ``{ok, count, polygons}`` where
        each is ``{pct: 0..100, ring: [[lat, lon], ...]}`` -- rendered FILLED
        (a vector polygon layer; not rasterised)."""
        ps = self.depth_map.composition_in(bbox=bbox, limit=limit)
        return {"ok": True, "count": len(ps), "polygons": ps}

    def water_polygon(self, bbox) -> dict:
        """OSM water polygon(s) for a (west, south, east, north) bbox, used to
        CLIP the depth overlays to water (don't draw composition over land). Uses
        the same offline WaterCache as routing; fetches from Overpass + caches if
        absent (so offline it needs the area pre-downloaded). Returns
        ``{ok, water}`` where water is GeoJSON-style MultiPolygon coords
        ``[[[ [lon,lat], ... ]=exterior, [ ... ]=hole, ... ], ...]`` (empty if none)."""
        from .nav import water
        w, s, e, n = bbox
        wbbox = (s, w, n, e)                       # water.py order: (S, W, N, E)
        cache = water.WaterCache(self.config.data_dir)
        try:
            geom = cache.find_covering(wbbox)
            if geom is None:
                geom = water.assemble_water(water.fetch_overpass(*wbbox))
                if geom is not None and not geom.is_empty:
                    cache.store(wbbox, geom)
        except Exception as exc:  # noqa: BLE001 - network/parse; clip is optional
            logger.warning("water fetch for clip failed: %s", exc)
            return {"ok": False, "water": []}
        if geom is None or geom.is_empty:
            return {"ok": True, "water": []}
        # The cached water geometry can be 9-20 MB / ~930k vertices, but this is
        # a purely VISUAL clip mask, so coarse is fine. Shrink it before sending:
        #   (a) clip to a slightly-padded request bbox (cover a bit past the view)
        #   (b) simplify to a few-metre tolerance
        #   (c) round coords to 5 decimals (~1 m)
        import shapely.geometry as sgeom

        pad = 0.10 * max(e - w, n - s)             # ~10% of the bbox span
        clip_box = sgeom.box(w - pad, s - pad, e + pad, n + pad)
        try:
            geom = geom.intersection(clip_box)
            geom = geom.simplify(1e-4, preserve_topology=True)  # ~11 m, coarse mask
        except Exception as exc:  # noqa: BLE001 - degenerate geom; clip is optional
            logger.warning("water clip/simplify failed: %s", exc)
            return {"ok": True, "water": []}
        if geom is None or geom.is_empty:
            return {"ok": True, "water": []}
        # Keep only polygonal parts (a clip can yield lines/points/collections).
        if geom.geom_type == "Polygon":
            polys = [geom]
        elif geom.geom_type == "MultiPolygon":
            polys = list(geom.geoms)
        elif geom.geom_type == "GeometryCollection":
            polys = [g for g in geom.geoms if g.geom_type == "Polygon"]
        else:
            polys = []
        out = []
        for p in polys:
            if p.is_empty:
                continue
            rings = [list(p.exterior.coords)] + [list(r.coords) for r in p.interiors]
            out.append([[[round(x, 5), round(y, 5)] for (x, y) in ring] for ring in rings])
        return {"ok": True, "water": out}

    def import_depth_map(self, filename: str, data: bytes, replace: bool = False) -> dict:
        """Import soundings from an uploaded open-format depth file (CSV/XYZ or
        GeoJSON). ``replace`` swaps the whole chart; otherwise the soundings are
        merged in. Persists to ``depthmap.json`` so the import survives restarts.

        Memory: a large GeoJSON/JSONL CHART upload is spilled to a temp file, the
        in-RAM HTTP body is freed, and the file is parsed with the BOUNDED
        streaming reader (columnar builders, one feature at a time) -- so the
        parse never adds a second full decoded-string copy + all-feature dict
        lists on top of the body. NOTE: the uploaded ``data`` bytes are inherently
        resident (FastAPI reads the whole HTTP body before this runs), so the UI
        upload's transient peak is bounded by the body size itself (~= the file);
        for a 512 MB device the on-device MIGRATION / offline .npz path (which
        never holds the body in RAM) is the fully-bounded route -- see
        ``DepthMap._migrate_json_chart``. Small CSV/XYZ soundings stay on the
        in-memory path (they're tiny)."""
        from .nav.depth import (ColumnarFeatures, parse_depth_features,
                                stream_parse_depth_features)

        name = (filename or "").lower()
        head = data[:64].lstrip()[:1] if data else b""   # peek a prefix, not the whole body
        is_geojson = name.endswith((".geojson", ".json", ".geojsonl", ".ndjson", ".jsonl")) \
            or head in (b"{", b"[")
        try:
            if is_geojson:
                import tempfile

                tmp = tempfile.NamedTemporaryFile(
                    mode="wb", suffix=".chartupload",
                    dir=self.config.data_dir, delete=False)
                tmp_name = tmp.name
                try:
                    tmp.write(data)
                    tmp.flush()
                    tmp.close()
                    del data                    # free the HTTP body ASAP
                    with open(tmp_name, "r", encoding="utf-8", errors="replace") as fh:
                        parsed = stream_parse_depth_features(fh)
                finally:
                    try:
                        os.remove(tmp_name)
                    except OSError:             # pragma: no cover - defensive
                        pass
            else:
                parsed = parse_depth_features(filename, data)
        except Exception as exc:  # noqa: BLE001 - any parse error -> clean message
            logger.warning("depth import parse failed: %s", exc)
            return {"ok": False, "error": f"could not parse the file: {exc}", "imported": 0}
        pts = parsed["soundings"]
        hard = parsed["hardness"]
        cont = parsed.get("contours", [])
        comp = parsed.get("composition", [])
        if not pts and not hard and not cont and not comp:
            return {"ok": False, "error": "no valid (lat, lon, depth) soundings found in the file",
                    "imported": 0}
        dm = self.depth_map
        if replace:
            dm.points = []
            dm.hardness = []
            # Reset the vector layers to EMPTY COLUMNAR stores (not plain lists):
            # ``extend`` then concatenates the parsed columnar arrays in place, so
            # a large replace-import stays bounded (a plain-list ``extend`` would
            # iterate the columnar result back into a full dict list -- the ~1.7 GB
            # shape this store exists to avoid).
            dm.contours = ColumnarFeatures.empty("d", "pts")
            dm.composition = ColumnarFeatures.empty("pct", "ring")
            dm._last = None
        dm.points.extend(pts)
        dm.hardness.extend(hard)
        dm.contours.extend(cont)
        dm.composition.extend(comp)
        if len(dm.points) > dm.max_points:
            dm.points = dm.points[-dm.max_points:]
        if len(dm.hardness) > dm.max_points:
            dm.hardness = dm.hardness[-dm.max_points:]
        dm.save(self._depth_map_path)           # soundings
        dm.save_chart(self._depth_chart_path)   # static chart (hardness/contours/composition)
        self._depth_saved_n = len(dm.points)
        logger.info("imported %d soundings + %d hardness + %d contours + %d composition from %s",
                    len(pts), len(hard), len(cont), len(comp), filename)
        return {"ok": True, "imported": len(pts), "hardness": len(hard),
                "contours": len(cont), "composition": len(comp), "total": len(dm.points)}

    def _health_snapshot(self) -> dict:
        """Cheap per-sensor freshness + controller-loop health for telemetry.

        Ages are seconds since each input last arrived (``None`` when it has
        never been received); ``controller_tick_age_s`` is the control loop's
        heartbeat age (``None`` before the loop has run). Also surfaces the
        wave-1 ``controller_fault`` and the governor's active staleness flags.
        No I/O -- pure reads off the shared state and the last governor status."""
        now = self._mono_fn()
        st = self.state

        def _age(stamp: float | None) -> float | None:
            return round(now - stamp, 2) if stamp is not None else None

        tick = st.controller_last_tick_monotonic
        status = self.controller.safety_status
        depth_age = _age(st.depth_received_mono)
        depth_stale_s = self.controller.safety.config.depth_stale_s
        health = {
            "fix_age_s": _age(st.fix_received_mono),
            "heading_age_s": _age(st.heading_received_mono),
            "depth_age_s": depth_age,
            "imu_age_s": _age(st.imu_received_mono),
            "controller_fault": st.controller_fault,
            "controller_tick_age_s": (round(now - tick, 2) if tick else None),
            # Active staleness / freshness flags (last governor tick).
            "heading_stale": status.heading_stale,
            "fix_lost": status.fix_lost,
            "depth_stale": depth_age is not None and depth_age > depth_stale_s,
        }
        # Per-device connection health, surfaced from serial devices that expose
        # ``healthy`` / ``last_data_monotonic`` (the reconnect work). Sim devices
        # lack the attributes, so the block is omitted entirely on a sim-only
        # runtime -- keeping the base health shape unchanged when no real device
        # reports health.
        devices = self._device_health(now)
        if devices:
            health["devices"] = devices
        return health

    def _device_health(self, now: float | None = None) -> dict:
        """``{gps: {healthy, data_age_s}, compass: ..., depth: ..., motor: ...}``
        for any device exposing ``healthy`` / ``last_data_monotonic``.

        Null-safe: a device without a ``healthy`` attribute (sim devices) is
        omitted; a present-but-never-received ``last_data_monotonic`` yields a
        ``data_age_s`` of ``None``."""
        if now is None:
            now = self._mono_fn()
        out: dict = {}
        for name, dev in (
            ("gps", self.gps),
            ("compass", self.compass),
            ("depth", self.depth_sounder),
            ("motor", self.controller.motor),
        ):
            healthy = getattr(dev, "healthy", None)
            if healthy is None:
                continue  # sim / attribute-less device -> no health to report
            last = getattr(dev, "last_data_monotonic", None)
            out[name] = {
                "healthy": bool(healthy),
                "data_age_s": round(now - last, 2) if last is not None else None,
            }
        # Hardware watchdog (#44): only when enabled. healthy = the heartbeat is
        # armed + running (a stopped watchdog would drop the motor-supply relay).
        wd = getattr(self, "watchdog", None)
        if wd is not None and getattr(wd, "enabled", False):
            out["watchdog"] = {"healthy": bool(getattr(wd, "_started", False)),
                               "data_age_s": None}
        return out

    def _device_connected_map(self, cfg: AppConfig) -> dict:
        """``{kind: bool}`` — a device is connected unless its source is "none".

        For a **split** motor plan the map additionally carries ``"thrust"`` and
        ``"steering"`` per-channel booleans: *connected* means the channel is
        both configured (source ≠ "none") **and** was actually built (channel
        object is not ``None``).  A build failure (e.g. serial channels arriving
        in Task 3) therefore shows up here so mode-gating keeps the pilot safe.

        When the plan is **combined** those keys are absent; the fail-open
        default in :func:`~vanchor.core.capabilities.missing_devices` treats
        them as connected, preserving exact back-compat with all legacy tests.
        """
        from .hardware.link_plan import plan_motor_links
        from .hardware.split_motor import SplitMotor
        hw = cfg.hardware
        conn = {k: hw.source(k) != "none" for k in ("gps", "compass", "depth", "motor")}
        bsrc = hw.battery_source or ("sim" if self.simulator is not None else "none")
        conn["battery"] = bsrc != "none"

        # Split plan: add per-channel connectivity (fail-open for combined so
        # every existing test that omits "thrust"/"steering" stays green).
        try:
            plan = plan_motor_links(hw)
        except ValueError:
            plan = None  # malformed config; leave channel keys absent (fail-open)

        if plan is not None and plan.kind == "split":
            motor = getattr(
                getattr(self, "controller", None), "motor", None)
            thrust_ch = motor.thrust if isinstance(motor, SplitMotor) else None
            steering_ch = motor.steering if isinstance(motor, SplitMotor) else None

            t_src = plan.thrust["source"] if plan.thrust else "none"
            s_src = plan.steering["source"] if plan.steering else "none"

            # Connected = configured (non-none) AND actually built (not None)
            conn["thrust"] = (t_src != "none") and (thrust_ch is not None)
            conn["steering"] = (s_src != "none") and (steering_ch is not None)
            # Motor composite: at least one channel active
            conn["motor"] = conn["thrust"] or conn["steering"]

        # Combined plan with a neutral (disabled) channel: include the disabled
        # channel as False so anchor/vectored modes are correctly gated with
        # "Steering/Thrust not connected".  The OMIT rule applies only to the
        # plain combined case (no neutral_channel); here we must be explicit.
        elif plan is not None and plan.kind == "combined" and plan.neutral_channel:
            conn[plan.neutral_channel] = False

        return conn

    def device_status(self) -> dict:
        """Per-device ``{source, connected, healthy}`` for the gating UI.

        ``connected`` = the configured source is not "none". ``healthy`` is the
        device's live health (``None`` when the device doesn't report it, e.g. a
        sim device)."""
        hw = self.config.hardware
        connected = self._device_connected_map(self.config)

        def _healthy(dev) -> bool | None:
            h = getattr(dev, "healthy", None)
            if h is not None:
                return bool(h)
            hf = getattr(dev, "health", None)  # battery monitors expose health()
            if callable(hf):
                try:
                    return bool(hf().get("healthy")) if isinstance(hf(), dict) else None
                except Exception:  # noqa: BLE001
                    return None
            return None

        out: dict = {}
        for kind, dev in (("gps", self.gps), ("compass", self.compass),
                          ("depth", self.depth_sounder), ("motor", self.controller.motor),
                          ("battery", getattr(self, "battery_monitor", None))):
            src = hw.battery_source if kind == "battery" else hw.source(kind)
            if kind == "battery" and not src:
                src = "sim" if self.simulator is not None else "none"
            out[kind] = {"source": src, "connected": connected.get(kind, True),
                         "healthy": _healthy(dev)}

        # Split plan: add per-channel status entries + update motor roll-up.
        from .hardware.link_plan import plan_motor_links
        from .hardware.split_motor import SplitMotor
        try:
            plan = plan_motor_links(hw)
        except ValueError:
            plan = None

        if plan is not None and plan.kind == "split":
            motor = self.controller.motor
            thrust_ch = motor.thrust if isinstance(motor, SplitMotor) else None
            steering_ch = motor.steering if isinstance(motor, SplitMotor) else None

            t_src = plan.thrust["source"] if plan.thrust else "none"
            s_src = plan.steering["source"] if plan.steering else "none"
            t_conn = (t_src != "none") and (thrust_ch is not None)
            s_conn = (s_src != "none") and (steering_ch is not None)

            def _ch_healthy(ch) -> bool | None:
                if ch is None:
                    return False  # build failed / serial Task 3 placeholder
                return _healthy(ch)

            out["thrust"] = {
                "source": t_src,
                "connected": t_conn,
                "healthy": _ch_healthy(thrust_ch),
            }
            out["steering"] = {
                "source": s_src,
                "connected": s_conn,
                "healthy": _ch_healthy(steering_ch),
            }
            # Update the composite motor healthy to the roll-up (both healthy).
            t_h = out["thrust"]["healthy"]
            s_h = out["steering"]["healthy"]
            known = [h for h in (t_h, s_h) if h is not None]
            out["motor"]["healthy"] = all(known) if known else None
            out["motor"]["source"] = "split"
            out["motor"]["connected"] = t_conn or s_conn

        elif plan is not None and plan.kind == "combined" and plan.neutral_channel:
            # Combined plan with a disabled channel: surface it in device_status
            # so telemetry()'s mode_availability derivation can gate correctly.
            # The active channel is not surfaced separately (back-compat with the
            # plain combined case); only the DISABLED side gets an entry.
            out[plan.neutral_channel] = {
                "source": "none",
                "connected": False,
                "healthy": None,
            }

        return out

    async def phone_ingest(self, kind: str, client_id, data: dict) -> str:
        """Feed one phone-sensor sample (see hardware.drivers.phone). Returns
        accepted/rejected/inactive -- 'inactive' until a phone source is selected
        in Devices, 'rejected' while ANOTHER client holds the feeder slot."""
        hub = getattr(self, "phone_hub", None)
        if hub is None:
            return "inactive"
        return await hub.ingest(kind, client_id, data)

    def client_log(self, entries: list, session: str = "?") -> int:
        """Ingest client-RUM entries (JS errors, WS lifecycle, sensor
        breadcrumbs) from a browser. Each entry is logged under the
        ``vanchor.client`` logger -- which the debug recorder's root log
        capture already includes -- AND written to an active recording as a
        structured ``client`` stream, so field problems on a phone are
        troubleshootable from the same recording as the boat data. Bounded:
        at most 50 entries per call, fields truncated."""
        client_logger = logging.getLogger("vanchor.client")
        sid = str(session)[:12]
        now = time.time()
        accepted = 0
        for e in entries[:50]:
            if not isinstance(e, dict):
                continue
            level = str(e.get("level", "info")).lower()
            event = str(e.get("event", ""))[:40]
            msg = str(e.get("msg", ""))[:500]
            lvl = (logging.ERROR if level == "error"
                   else logging.WARNING if level in ("warn", "warning")
                   else logging.INFO)
            client_logger.log(lvl, "[%s] %s: %s", sid, event, msg)
            if self.debug.active:
                self.debug.write("client", {"session": sid, "level": level,
                                            "event": event, "msg": msg,
                                            "t": e.get("t")}, now)
            accepted += 1
        return accepted

    def phone_disconnect(self, client_id) -> None:
        """A WS client vanished: free any phone-sensor feeder slots it held (the
        only automatic reassignment path -- helm changes never touch feeders)."""
        hub = getattr(self, "phone_hub", None)
        if hub is not None:
            hub.on_disconnect(client_id)

    def device_debug(self, kind: str) -> dict:
        """Human-readable raw-data snapshot for one device (Devices -> Debug).
        Returns ``{ok, kind, source, debug}``; ``ok:false`` if no such device.

        Also accepts the per-channel kinds ``"steering"`` and ``"thrust"`` when
        the motor plan is split; ``"motor"`` always returns the composite
        :class:`~vanchor.hardware.split_motor.SplitMotor` debug (which includes
        both channels) even in a split build, so the existing UI debug button
        keeps working.
        """
        hw = self.config.hardware

        # Handle per-channel split kinds.
        if kind in ("steering", "thrust"):
            from .hardware.split_motor import SplitMotor
            from .hardware.link_plan import plan_motor_links
            motor = self.controller.motor
            try:
                plan = plan_motor_links(hw)
            except ValueError:
                plan = None
            if plan is None or plan.kind != "split":
                return {"ok": False, "kind": kind,
                        "debug": f"Motor plan is not split; no '{kind}' channel."}
            ch = motor.thrust if kind == "thrust" else motor.steering
            ch_link = plan.thrust if kind == "thrust" else plan.steering
            src = ch_link["source"] if ch_link else "none"
            if ch is None:
                return {"ok": False, "kind": kind, "source": src,
                        "debug": f"No {kind} channel is active (not built)."}
            try:
                text = ch.debug()
            except Exception as exc:  # noqa: BLE001
                text = f"debug() raised: {type(exc).__name__}: {exc}"
            return {"ok": True, "kind": kind, "source": src, "debug": text}

        dev = {"gps": self.gps, "compass": self.compass,
               "depth": self.depth_sounder, "motor": self.controller.motor,
               "battery": getattr(self, "battery_monitor", None)}.get(kind)
        if dev is None:
            return {"ok": False, "kind": kind,
                    "debug": f"No {kind} device is active (source is 'none' or unbuilt)."}
        src = (self.config.hardware.battery_source if kind == "battery"
               else self.config.hardware.source(kind))
        try:
            text = dev.debug()
        except Exception as exc:  # noqa: BLE001 - debug must never break the UI
            text = f"debug() raised: {type(exc).__name__}: {exc}"
        return {"ok": True, "kind": kind, "source": src, "debug": text}

    def all_device_debug(self) -> dict:
        """``{kind: debug_string}`` for every device -- recorded into a debug
        session so raw device data is preserved (notably the UBX GPS, which
        bypasses the per-sentence ``nmea`` capture)."""
        from .hardware.link_plan import plan_motor_links
        kinds = ["gps", "compass", "depth", "motor", "battery"]
        try:
            plan = plan_motor_links(self.config.hardware)
            if plan.kind == "split":
                kinds += ["steering", "thrust"]
        except ValueError:
            pass
        return {kind: self.device_debug(kind).get("debug", "") for kind in kinds}

    # ------------------------------------------------------------------ #
    # Connector framework (consent-gated bus bridges)
    # ------------------------------------------------------------------ #

    def _make_connector_sink(self, name: str):
        """Return a command sink for connector ``name``.

        The sink wraps :meth:`handle_command` with
        :meth:`record_command` attribution (Constraint 4).  Exceptions
        (including any residual :exc:`PermissionError`) are caught,
        logged, and attributed as ``"error"`` in the audit ring — they
        NEVER propagate to ``handle_command`` (which would not know what
        to do with them).
        """
        def _sink(cmd: dict) -> None:
            ctype = cmd.get("type")
            try:
                self.handle_command(cmd)
                self.record_command(ctype, f"connector:{name}", "accepted")
            except Exception as exc:  # noqa: BLE001 - sink must never propagate
                self.record_command(ctype, f"connector:{name}", "error", str(exc))
        return _sink

    def connector_status(self) -> list[dict]:
        """Status of every *registered* connector (not just armed ones).

        Each entry: ``{name, label, description, grant_lines, control,
        armed, needs_reconsent, running, status, settings, settings_schema}``.

        ``settings`` contains current values from the grant store merged over
        schema defaults.  Secret fields are masked as ``"•••"`` when set or
        ``""`` when unset.  Internal keys (``data_dir``, ``user_edited``) are
        excluded.  ``settings_schema`` is the connector's declared field list.
        """
        from .connectors import registry as _creg
        from .connectors.registry import (
            armed as _armed,
            needs_reconsent as _needs_reconsent,
        )
        result: list[dict] = []
        for name in _creg.names():
            sp = _creg.spec(name)
            if sp is None:  # pragma: no cover - registry invariant
                continue
            grant_settings = self._connector_grants.get(name, {}).get("settings", {})
            # Inject data_dir so connectors that buffer to disk (e.g. metrics)
            # use the runtime's data dir instead of CWD.  Grant settings win on
            # any explicit key (e.g. a custom data_dir override).
            settings = {"data_dir": self.config.data_dir, **grant_settings}
            try:
                conn_proto = _creg.build(name, settings)
                mfst = conn_proto.manifest
            except Exception as exc:  # noqa: BLE001 - a bad connector can't break status
                logger.warning("connector %r failed to build for status: %s", name, exc)
                continue
            running_conn = self.connectors.get(name)
            try:
                st = running_conn.status() if running_conn is not None else {}
            except Exception:  # noqa: BLE001 - status must never raise
                st = {}
            # Build the masked settings dict from the schema + grant store.
            schema = getattr(conn_proto, "settings_schema", []) or []
            masked_settings = _mask_connector_settings(schema, grant_settings)
            result.append({
                "name": name,
                "label": mfst.label,
                "description": mfst.description,
                "grant_lines": list(mfst.grant_lines),
                "control": bool(mfst.control),
                "armed": _armed(name, mfst, self._connector_grants),
                "needs_reconsent": _needs_reconsent(name, mfst, self._connector_grants),
                "running": running_conn is not None,
                "status": st,
                "settings": masked_settings,
                "settings_schema": schema,
            })
        return result

    async def set_connector_armed(self, name: str, enabled: bool) -> dict:
        """Persist the grant, then live-start or stop the connector.

        Returns ``{ok, running}`` on success; ``{ok:False, error:...}`` when
        ``name`` is unknown or the connector fails to build."""
        from .connectors import registry as _creg
        from .connectors.base import manifest_hash as _mhash
        from .connectors.context import ConnectorContext
        from .connectors.registry import save_grants as _save_grants

        if not _creg.has(name):
            return {"ok": False, "error": f"unknown connector {name!r}"}

        grant_settings = self._connector_grants.get(name, {}).get("settings", {})
        # Inject data_dir so connectors that buffer to disk use the runtime's
        # data dir; grant settings override if they carry an explicit key.
        settings = {"data_dir": self.config.data_dir, **grant_settings}
        try:
            conn = _creg.build(name, settings)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"failed to build connector: {exc}"}

        # Persist the grant with the CURRENT manifest hash (consent).
        self._connector_grants[name] = {
            "enabled": bool(enabled),
            "manifest_hash": _mhash(conn.manifest),
            "settings": settings,
        }
        _save_grants(self.config.data_dir, self._connector_grants)

        if enabled:
            if name not in self.connectors:
                sink = self._make_connector_sink(name)
                ctx = ConnectorContext(
                    self.bus, conn.manifest, sink, mono_fn=self._mono_fn
                )
                try:
                    await conn.start(ctx)
                    self.connectors[name] = conn
                    logger.info("connector %r armed and started", name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "connector %r failed to start after arming: %s", name, exc
                    )
                    return {
                        "ok": True, "running": False,
                        "error": f"started failed: {exc}",
                    }
        else:
            existing = self.connectors.pop(name, None)
            if existing is not None:
                try:
                    await existing.stop()
                    logger.info("connector %r disarmed and stopped", name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "connector %r failed to stop cleanly: %s", name, exc
                    )

        return {"ok": True, "running": name in self.connectors}

    async def set_connector_settings(self, name: str, values: dict) -> dict:
        """Validate, persist, and live-apply new settings for connector ``name``.

        The ``values`` dict is validated against the connector's
        ``settings_schema``:

        * Unknown keys (not in the schema) → ``{ok: False, error: ...}`` (400).
        * A masked secret value ``"•••"`` means *keep the stored value
          unchanged* — it is **never** written literally.
        * Values are type-coerced according to the field's ``type``.
        * The merge is additive: existing stored keys not covered by the schema
          (or not present in ``values``) survive unchanged.
        * The internal ``user_edited: true`` flag is set so the nmea-tcp
          boot re-sync does not clobber explicitly chosen host/port values.

        If the connector is **running**, live-applies the change:
        stop → rebuild with new settings → start.  A failing restart is logged
        and the connector is left not-running (never crashes the runtime).

        If the new settings change the connector's **manifest** (e.g. flipping
        ``thruster_control`` on the nmea2000 connector), the connector is
        stopped and the response includes ``needs_reconsent: true`` — the UI
        should surface the re-consent flow.
        """
        from .connectors import registry as _creg
        from .connectors.base import manifest_hash as _mhash
        from .connectors.context import ConnectorContext
        from .connectors.registry import save_grants as _save_grants

        if not _creg.has(name):
            return {"ok": False, "error": f"unknown connector {name!r}"}

        current_grant = self._connector_grants.get(name, {})
        stored_settings = current_grant.get("settings", {})
        settings_for_proto = {"data_dir": self.config.data_dir, **stored_settings}
        try:
            proto = _creg.build(name, settings_for_proto)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"failed to build connector: {exc}"}

        schema: list = getattr(proto, "settings_schema", []) or []
        schema_keys: set = {f["key"] for f in schema if f.get("key")}

        # Reject unknown keys
        for key in values:
            if key not in schema_keys:
                return {"ok": False, "error": f"unknown setting key {key!r}"}

        # Type-coerce values; skip masked secrets (they mean "unchanged")
        coerced: dict = {}
        for field in schema:
            key = field.get("key")
            if not key or key not in values:
                continue
            raw = values[key]
            # Masked secret = "leave the stored value alone"
            if field.get("secret") and raw == "•••":
                continue
            ftype = field.get("type", "str")
            try:
                if ftype == "int":
                    coerced[key] = int(raw)
                elif ftype == "float":
                    coerced[key] = float(raw)
                elif ftype == "bool":
                    if isinstance(raw, bool):
                        coerced[key] = raw
                    else:
                        coerced[key] = str(raw).lower() in ("true", "1", "yes", "on")
                else:
                    coerced[key] = str(raw)
            except (ValueError, TypeError) as exc:
                return {"ok": False, "error": f"invalid value for {key!r}: {exc}"}

        # Merge into existing stored settings, preserving unknown/internal keys.
        # user_edited is updated; data_dir is not user-visible.
        new_stored = dict(stored_settings)
        new_stored.update(coerced)
        new_stored["user_edited"] = True

        # Build with new settings to detect a manifest change (e.g. thruster_control).
        new_settings_full = {"data_dir": self.config.data_dir, **new_stored}
        try:
            new_proto = _creg.build(name, new_settings_full)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"failed to build with new settings: {exc}"}

        new_manifest_hash = _mhash(new_proto.manifest)
        old_manifest_hash = current_grant.get("manifest_hash", "")
        manifest_changed = bool(old_manifest_hash) and (new_manifest_hash != old_manifest_hash)
        was_enabled = bool(current_grant.get("enabled", False))
        needs_reconsent_flag = manifest_changed and was_enabled

        # Persist — the manifest_hash in the grant stays as-is (only
        # set_connector_armed updates it; that's the consent step).
        self._connector_grants[name] = {
            **current_grant,
            "settings": new_stored,
        }
        _save_grants(self.config.data_dir, self._connector_grants)

        # Live-apply when the connector is running.
        running_conn = self.connectors.get(name)
        if running_conn is not None:
            # Always stop the current instance first.
            self.connectors.pop(name, None)
            try:
                await running_conn.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "connector %r stop failed during settings update: %s", name, exc
                )
            if manifest_changed:
                # Manifest changed → connector is disarmed (hash mismatch);
                # don't restart it.  The user must re-consent.
                logger.info(
                    "connector %r stopped after manifest-changing settings update "
                    "(needs_reconsent=True)", name
                )
            else:
                # Rebuild + restart with new settings.
                try:
                    new_conn = _creg.build(name, new_settings_full)
                    sink = self._make_connector_sink(name)
                    ctx = ConnectorContext(
                        self.bus, new_conn.manifest, sink, mono_fn=self._mono_fn
                    )
                    await new_conn.start(ctx)
                    self.connectors[name] = new_conn
                    logger.info("connector %r restarted with new settings", name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "connector %r failed to restart with new settings: %s; "
                        "left not-running",
                        name, exc,
                    )

        return {
            "ok": True,
            "needs_reconsent": needs_reconsent_flag,
            "running": name in self.connectors,
        }

    def connector_debug(self, name: str) -> dict:
        """Human-readable debug string for connector ``name``.

        Returns ``{ok, name, debug}``; ``ok:False`` if the connector is
        not known or not running (mirroring :meth:`device_debug`)."""
        from .connectors import registry as _creg

        if not _creg.has(name):
            return {
                "ok": False, "name": name,
                "debug": f"unknown connector {name!r}",
            }
        conn = self.connectors.get(name)
        if conn is None:
            return {
                "ok": False, "name": name,
                "debug": f"connector {name!r} is not running",
            }
        try:
            text = conn.debug()
        except Exception as exc:  # noqa: BLE001 - debug must never break the UI
            text = f"debug() raised: {type(exc).__name__}: {exc}"
        return {"ok": True, "name": name, "debug": text}

    def telemetry(self) -> dict:
        """Build a PURE telemetry snapshot -- no side effects.

        This is called by BOTH the WS broadcaster and ``GET /api/state``, so it
        must not mutate anything: the periodic safety evaluations (launch
        capture, RTL recommend, link failsafe), trip accumulation and depth
        persistence all live in the supervisor task (see ``_run_supervisor`` /
        ``_supervise_once``); depth-sounding accumulation is driven by the
        broadcaster via ``record_depth_sounding`` so polling ``/api/state`` can't
        double-record soundings or perturb failsafe timing (findings M2/H4/#7).
        """
        # During replay, play recorded frames back instead of live state. Live
        # safety evaluation keeps running regardless -- it lives in the
        # supervisor now, not here -- so swapping the displayed frame can't
        # disable it.
        if self.replay.active:
            frame = self.replay.current(time.time())
            if frame is not None:
                return frame

        payload = self.state.to_dict()
        payload["safety"] = self.controller.safety_status.to_dict()
        # Land guard settings ride the safety block (the status part comes from
        # the governor; enabled/margin/chart come from config + runtime).
        payload["safety"]["land_guard"].update({
            "enabled": self.controller.safety.config.land_guard_enabled,
            "margin_m": self.controller.safety.config.land_guard_margin_m,
            "have_chart": self.controller.safety.has_water_geometry,
        })
        # Server-side safety geometry (#23) so the browser becomes a CACHE, not
        # the source of truth: a freshly-opened client renders the SERVER's
        # zones/min-depth/failsafe. Raw no-go rings come from the persistence
        # store; min-depth + failsafe are read live off the governor (the
        # authority). safety.js adopts these as truth and only pushes local ->
        # server on an explicit edit or a one-time migration (no echo loop).
        _gov = self.controller.safety.config
        payload["safety_geometry"] = {
            "nogo_zones": self.safety_geometry.nogo_zones,
            "min_depth_m": _gov.min_depth_m,
            "fix_failsafe_enabled": _gov.fix_failsafe_enabled,
        }
        payload["health"] = self._health_snapshot()
        # Device availability + per-mode gating (Not-connected devices disable the
        # modes/functions that need them; the UI shows the reason).
        from .core.capabilities import mode_availability
        payload["devices"] = self.device_status()
        payload["mode_availability"] = mode_availability(
            {k: v["connected"] for k, v in payload["devices"].items()}
        )
        payload["battery"] = self.battery_snapshot()
        payload["link"] = {
            "client_connected": self._ui_clients > 0,
            "since_s": (
                round(self._mono_fn() - self._last_client_seen, 1)
                if self._last_client_seen is not None
                else None
            ),
            "failsafe_engaged": self._link_failsafe_engaged,
            # What the failsafe DID: "continue" | "hold" | "stop" | None. Lets
            # the UI report "continuing mission" instead of a blanket
            # "holding position" when continue-mission is on.
            "failsafe_action": self._link_failsafe_action,
        }
        ctrl = self.controller
        # Manual course-hold line (bearing + anchor), for the chart overlay.
        mc = ctrl.manual
        payload["manual_course"] = (
            {"bearing": mc.course_bearing,
             "lat": mc.course_origin.lat, "lon": mc.course_origin.lon}
            if mc.course_bearing is not None and mc.course_origin is not None
            else None
        )
        payload["auto_apb"] = {
            "enabled": self.config.safety.auto_follow_apb,
            # engaged = the CURRENT Follow-APB session was started by auto-APB
            "engaged": (self._auto_apb_latched
                        and self.state.mode == ControlModeName.FOLLOW_APB),
        }
        payload["cruise"] = {
            "enabled": ctrl.cruise_knots is not None,
            "target_knots": ctrl.cruise_knots or 0.0,
        }
        payload["track"] = {
            "recording": ctrl.track.recording,
            "count": len(ctrl.track.points),
            # Most recent breadcrumbs for the map (cap the payload size).
            "points": [[p.lat, p.lon] for p in ctrl.track.points[-300:]],
        }
        payload["trip"] = self.trip.snapshot(self._now_fn())
        # Expose (read-only) the accumulated depth map. Accumulation + periodic
        # persistence are NOT done here (telemetry() is a pure snapshot): the
        # broadcaster drives sounding accumulation via record_depth_sounding()
        # and the supervisor checkpoints to disk off the event loop.
        # depth_count is a cheap scalar; depth_points (~28 KB) is the bulk of the
        # frame. telemetry() returns the COMPLETE snapshot (so /api/state is
        # deterministic + full); the high-rate WS broadcaster decimates
        # depth_points to ~1 Hz (see ui/server.py:broadcaster). The frontend uses
        # depth_count for the readout and retains the last points when the WS
        # omits the array.
        payload["depth_count"] = len(self.depth_map.points)
        payload["depth_points"] = self.depth_map.as_list()

        # Closed-loop steering unit: target (pre-slew) vs feedback (actual head
        # angle). On hardware the feedback is the AS5600; in sim it's the
        # slew-limited applied steering.
        boat = self.config.boat
        max_ang = self.state.max_steer_angle_deg
        actual = self.state.motor_command.steering
        target_deg = self.controller.safety.desired_steering * max_ang
        angle_deg = actual * max_ang
        rng = max(1.0, boat.steer_range_deg)
        payload["steering"] = {
            "commanded": round(actual, 3),
            "target_deg": round(target_deg, 1),
            "angle_deg": round(angle_deg, 1),
            "rate_dps": boat.max_steer_rate_dps,
            "range_deg": boat.steer_range_deg,
            "wrap_pct": round(max(-100.0, min(100.0, angle_deg / rng * 100.0)), 0),
            "feedback_ok": True,
        }
        # On real hardware the steering Arduino reports its *measured* azimuth
        # back over serial (#83). SerialMotorController parses those ``A`` lines
        # into ``last_feedback``; when present we surface the real feedback
        # instead of the commanded estimate. The sim motor has no such
        # attribute, so the simulator path keeps the modelled values above.
        feedback = getattr(self.controller.motor, "last_feedback", None)
        if feedback is not None:
            payload["steering"]["angle_deg"] = round(feedback.angle_deg, 1)
            payload["steering"]["wrap_pct"] = round(feedback.wrap_pct, 0)
            payload["steering"]["feedback_ok"] = feedback.ok
        payload["boat"] = self.boat_profile()
        payload["gps_offset"] = {
            "dlat": self.navigator.gps_dlat,
            "dlon": self.navigator.gps_dlon,
            "active": self.navigator.gps_offset_active,
        }
        payload["throttle_override"] = {
            "active": self.controller.throttle_override is not None,
            "percent": (
                self.controller.throttle_override * 100.0
                if self.controller.throttle_override is not None
                else 0.0
            ),
        }
        # Guided pattern modes (#57/#58/#59) -- expose each mode's live state.
        contour_mode = ctrl.modes[ControlModeName.CONTOUR_FOLLOW]
        payload["contour"] = {
            "target_depth_m": round(self.state.contour_target_depth_m, 1),
            "depth_m": round(self.state.depth_m, 1),
            "error_m": round(contour_mode.error_m, 2),
        }
        orbit_mode = ctrl.modes[ControlModeName.ORBIT]
        payload["orbit"] = {
            "center_lat": (
                self.state.orbit_center.lat if self.state.orbit_center else None
            ),
            "center_lon": (
                self.state.orbit_center.lon if self.state.orbit_center else None
            ),
            "radius_m": round(self.state.orbit_radius_m, 1),
            "direction": self.state.orbit_direction,
            "range_m": round(orbit_mode.range_m, 2),
        }
        trolling_mode = ctrl.modes[ControlModeName.TROLLING]
        payload["trolling"] = {
            "base_heading": round(self.state.trolling_base_heading, 2),
            "amplitude_deg": round(self.state.trolling_amplitude_deg, 1),
            "period_s": round(self.state.trolling_period_s, 1),
            "phase": round(trolling_mode.phase, 3),
        }
        # Learned anchor mode (#34): the live residual-decay guardrail + polarity
        # bookkeeping, so a degraded hybrid falling back to its PID floor is
        # visible (hold_quality itself is in state.to_dict()).
        ml_mode = ctrl.modes.get(ControlModeName.ANCHOR_ML)
        if ml_mode is not None:
            payload["anchor_ml"] = {
                "residual_scale": round(ml_mode.residual_scale, 3),
                "residual_scale_effective": round(
                    ml_mode.residual_scale_effective, 3
                ),
                "guard_hold_ratio": round(ml_mode.guard_hold_ratio, 3),
                "steer_sign": ml_mode.steer_sign,
                "policy_steer_sign": ml_mode.policy_steer_sign,
            }
        payload["nav"] = {
            "paused": self.controller.suspended is not None,
            "suspended_mode": (
                self.controller.suspended["mode"].value
                if self.controller.suspended is not None
                else None
            ),
        }
        payload["calibration"] = self.calibration.snapshot()
        payload["sim_enabled"] = self.simulator is not None
        if self.simulator is not None:
            truth = self.simulator.truth()
            env = self.simulator.environment
            payload["truth"] = {
                "lat": truth.point.lat,
                "lon": truth.point.lon,
                "heading_deg": round(truth.heading_deg, 2),
                "speed_mps": round(truth.speed_mps, 3),
            }
            payload["environment"] = {
                "current_speed": env.current_speed,
                "current_dir": env.current_dir,
                "wind_speed": round(env.wind_speed, 2),
                "wind_dir": round(env.wind_dir, 1),
                "gust_amplitude_mps": env.gust_amplitude_mps,
                # Slow-wander amount so the UI can show how variable it is.
                "wind_variability": env.wind_variability,
                "current_variability": env.current_variability,
                # Instantaneous gusty wind the boat actually feels right now.
                "wind_gust_now": round(env.wind_speed + self.simulator.current_gust_mps, 2),
            }
        payload["debug"] = self.debug.status()
        payload["replay"] = {"active": self.replay.active, "name": self.replay.name} if self.replay.active else {"active": False}
        # NB: recording this frame into the debug session is done by the
        # broadcaster (off the event loop), not here -- telemetry() is pure so
        # that GET /api/state polling can't inject phantom frames into a session.
        return payload

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self.recorder.start()
        # Arm the external hardware watchdog (#44) before the supervisor begins
        # petting it. A no-op when disabled; a bad GPIO must not crash boot.
        try:
            self.watchdog.start()
        except Exception:
            logger.exception("hardware watchdog failed to start; continuing without it")
        # A serial device that won't open (e.g. an unplugged GPS or a renamed
        # port) must NOT take the whole app down on boot — log it and carry on so
        # the UI stays reachable to fix the device config (which reloads live).
        async def _try_start(name: str, dev) -> None:
            if dev is None:
                return
            try:
                await dev.start()
            except Exception:
                logger.exception("device %s failed to start; continuing without it", name)
        await _try_start("gps", self.gps)
        await _try_start("compass", self.compass)
        await _try_start("depth", self.depth_sounder)
        # Battery monitor (#42): the sim monitor is a read-view (no lifecycle); a
        # real shunt driver starts its poll loop here. A gauge that won't start is
        # logged, not fatal — same boot-resilience as the sensors.
        await _try_start("battery", getattr(self, "battery_monitor", None))
        # Open the motor transport too: a serial motor is never usable
        # otherwise -- its first flush() raises on the unopened port. Same
        # boot-resilience as the sensors: a motor that won't open is logged, not
        # fatal, so the UI stays reachable to fix the port.
        try:
            await _start_motor(self.controller.motor)
        except Exception:
            logger.exception("motor failed to start; continuing without it")
        if self.simulator is not None:
            self._sim_task = asyncio.ensure_future(self.simulator.run())
            self._tasks.append(self._sim_task)
        self._tasks.append(asyncio.ensure_future(self.controller.run()))
        # Periodic safety supervisor (~1 Hz): launch capture, RTL recommend, link
        # failsafe, trip accumulation + depth checkpointing. Runs regardless of
        # replay mode and client count (findings M2/H4/#7).
        self._tasks.append(asyncio.ensure_future(self._run_supervisor()))
        # Start armed connectors (after devices are up so the bus is ready).
        # A connector that fails to build or start is logged and skipped;
        # it NEVER crashes the whole app (mirror the compass-driver resilience).
        await self._start_armed_connectors()
        logger.info("runtime started (model=%s, hardware=%s)", self.config.sim.model, self.config.hardware.enabled)

    async def _start_armed_connectors(self) -> None:
        """Build and start every ARMED connector.

        Idempotent: only starts connectors not already in
        :attr:`connectors`. A connector that fails to build or start is
        logged and skipped — NEVER crashes startup."""
        from .connectors import registry as _creg
        from .connectors.base import manifest_hash as _mhash
        from .connectors.context import ConnectorContext
        from .connectors.registry import armed as _armed

        for name in _creg.names():
            if name in self.connectors:
                continue  # already running (e.g. after set_connector_armed)
            grant_settings = self._connector_grants.get(name, {}).get("settings", {})
            # Inject data_dir so connectors that buffer to disk use the
            # runtime's data dir; grant settings override if explicitly set.
            settings = {"data_dir": self.config.data_dir, **grant_settings}
            try:
                conn = _creg.build(name, settings)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "connector %r failed to build at startup: %s; skipping", name, exc
                )
                continue
            if not _armed(name, conn.manifest, self._connector_grants):
                continue
            sink = self._make_connector_sink(name)
            ctx = ConnectorContext(
                self.bus, conn.manifest, sink, mono_fn=self._mono_fn
            )
            try:
                await conn.start(ctx)
                self.connectors[name] = conn
                logger.info("connector %r started", name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "connector %r failed to start: %s; skipping", name, exc
                )

    async def stop(self) -> None:
        self.debug.stop()
        self.depth_map.save(self._depth_map_path)
        if self.simulator is not None:
            self.simulator.stop()
        self.controller.stop()
        # Best-effort motor shutdown: the serial controller sends CMD 0 and
        # closes its port here (STOP-on-shutdown). No-op for the sim motor.
        await _stop_motor(self.controller.motor)
        if self.gps is not None:
            await self.gps.stop()
        if self.compass is not None:
            await self.compass.stop()
        if self.depth_sounder is not None:
            await self.depth_sounder.stop()
        bm = getattr(self, "battery_monitor", None)
        if bm is not None:
            with contextlib.suppress(Exception):
                await bm.stop()
        # Stop all running connectors (best-effort so one bad connector can't
        # block the rest of shutdown).
        for cname, conn in list(self.connectors.items()):
            with contextlib.suppress(Exception):
                await conn.stop()
        self.connectors.clear()
        # De-assert the hardware watchdog line (#44): stopping the heartbeat is
        # itself the safe state (relay drops). Best-effort so shutdown never hangs.
        with contextlib.suppress(Exception):
            self.watchdog.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.recorder.stop()
        logger.info("runtime stopped")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Vanchor-NG server")
    parser.add_argument("--config", default=None, help="YAML/JSON config file")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--model", default=None, choices=["simple", "fossen"])
    parser.add_argument("--hardware", action="store_true", help="use real serial devices")
    parser.add_argument("--nmea-tcp", action="store_true", help="accept NMEA over TCP")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args(argv)

    config = load(args.config)
    # A saved device config (devices.json under data_dir) overrides the loaded
    # base hardware/nmea_tcp config before the runtime builds any device, so an
    # API-edited setup survives restarts. CLI flags below still win.
    apply_device_overrides(config)
    if args.host:
        config.server.host = args.host
    if args.port:
        config.server.port = args.port
    if args.model:
        config.sim.model = args.model
    if args.hardware:
        config.hardware.enabled = True
    if args.nmea_tcp:
        config.nmea_tcp.enabled = True
    if args.log_level:
        config.log_level = args.log_level

    import uvicorn

    from .ui.server import create_app

    runtime = Runtime(config)
    app = create_app(runtime)

    # Optional HTTPS listener on a second port: secure-context browser APIs
    # (Screen Wake Lock, full PWA installs) need it. Best-effort -- a busy port
    # or missing cert/openssl logs a warning and plain HTTP is unaffected.
    tls_pair = None
    if config.server.https_port:
        from .tls import ensure_tls_cert, port_free
        if not port_free(config.server.host, config.server.https_port):
            logger.warning("HTTPS port %d is in use; HTTPS disabled",
                           config.server.https_port)
        else:
            tls_pair = ensure_tls_cert(config.data_dir,
                                       config.server.ssl_certfile,
                                       config.server.ssl_keyfile)

    # Advertise over mDNS so a phone/PWA finds vanchor.local without an IP.
    advert = None
    if config.server.mdns:
        from . import __version__
        from .discovery import advertise
        props = {"version": __version__}
        if tls_pair:
            props["https_port"] = str(config.server.https_port)
        advert = advertise(config.server.port, config.server.host, properties=props)

    log_level = (args.log_level or "info").lower()
    servers = [uvicorn.Server(uvicorn.Config(
        app, host=config.server.host, port=config.server.port, log_level=log_level))]
    if tls_pair:
        cert, key = tls_pair
        servers.append(uvicorn.Server(uvicorn.Config(
            app, host=config.server.host, port=config.server.https_port,
            log_level=log_level, ssl_certfile=cert, ssl_keyfile=key)))
        logger.info("HTTPS listening on port %d (cert: %s)",
                    config.server.https_port, cert)

    async def _serve_all() -> None:
        # One event loop for every listener (the Runtime's tasks/bus live on it).
        # We own the signal handling: uvicorn's per-server handlers would clobber
        # each other, leaving all but the last server unstoppable on Ctrl-C.
        import signal as _signal
        for srv in servers:
            srv.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        loop = asyncio.get_running_loop()

        def _stop() -> None:
            for srv in servers:
                srv.should_exit = True
        for sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except NotImplementedError:  # pragma: no cover - non-unix
                pass
        await asyncio.gather(*(srv.serve() for srv in servers))

    try:
        asyncio.run(_serve_all())
    finally:
        if advert is not None:
            advert.close()


if __name__ == "__main__":
    main()
