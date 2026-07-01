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
import contextlib
import logging
import math
import os
import time

from .controller.calibration import CalibrationRunner
from .controller.controller import Controller, Helm
from .controller.modes import AnchorConfig, DriftConfig, FollowApbConfig, WaypointConfig
from .controller.safety import SafetyConfig
from .core import events, observability
from dataclasses import asdict

from .core.config import (
    AppConfig,
    HardwareConfig,
    NmeaTcpConfig,
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
from .sim.bathymetry import Bathymetry
from .sim.devices import SimCompass, SimDepthSounder, SimGps
from .sim.simulator import Simulator

logger = logging.getLogger("vanchor.app")

# Populate the pluggable device-driver registry (self-registering modules under
# hardware/drivers/). A new driver adds itself here just by existing.
load_drivers()

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


def _build_battery_config(cfg: AppConfig):
    """Map the app `battery:` config onto the sim battery model (#60)."""
    from .sim.battery import BatteryConfig as SimBatteryConfig

    b = cfg.battery
    return SimBatteryConfig(
        capacity_ah=b.capacity_ah,
        nominal_v=b.nominal_v,
        reserve_pct=b.reserve_pct,
    )


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

        # --- devices: simulated and/or real serial hardware (per-device) -- #
        # Built via _construct_devices so the SAME logic powers a live reload
        # (reload_devices) when the device config changes — no process restart.
        self.nmea_tcp = None
        self._environment = environment      # reused when devices are rebuilt live
        self._sim_task: "asyncio.Task | None" = None
        dev = self._construct_devices(cfg)
        self.simulator = dev["simulator"]
        self.gps = dev["gps"]
        self.compass = dev["compass"]
        self.depth_sounder = dev["depth_sounder"]
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
            declination_deg=cfg.sensors.magnetic_declination_deg,
        )
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
            ),
            anchor_config=AnchorConfig(
                kp=cfg.control.anchor_kp,
                kd=cfg.control.anchor_kd,
                idle_deadband_m=cfg.control.anchor_idle_deadband_m,
                boat_max_speed_mps=cfg.boat.max_speed_mps,
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
        )

        if cfg.nmea_tcp.enabled:
            from .nav.nmea_net import NmeaTcpServer

            self.nmea_tcp = NmeaTcpServer(
                self.bus, host=cfg.nmea_tcp.host, port=cfg.nmea_tcp.port
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

        # --- Lost-connection failsafe (#64) ------------------------------ #
        # Number of connected UI clients and the last time one was seen alive.
        self._ui_clients = 0
        self._last_client_seen: float | None = None
        # True once the failsafe has auto-engaged hold-position (so we don't
        # repeatedly re-engage it; cleared on reconnect).
        self._link_failsafe_engaged = False
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
        # Lateral-offset thrust-yaw feed-forward follows the geometry/trim live so
        # changing the offset (or the calibrated trim) updates compensation now.
        self.controller.helm.thrust_yaw_ff = _thrust_yaw_ff_norm(self.config)
        # Anchor mode caps thrust by the boat's top speed; keep it in step.
        anchor = self.controller.modes.get(ControlModeName.ANCHOR_HOLD)
        if anchor is not None and hasattr(anchor, "config"):
            anchor.config.boat_max_speed_mps = b.max_speed_mps
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
        return self.boats.get(profile_id)

    def boat_profiles_activate(self, profile_id: str) -> dict | None:
        """Make a profile active and apply its specs to the live sim. Returns
        the applied boat profile dict, or None if the id is unknown."""
        if not self.boats.set_active(profile_id):
            return None
        active = self.boats.active()
        if active is not None:
            self._apply_boat_specs(active["specs"])
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
        return True

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
    _SENSOR_SOURCES = ("sim", "serial", "nmea")
    _MOTOR_SOURCES = ("sim", "serial", "both")

    def _compass_sources(self) -> tuple:
        """Built-in compass sources + any registered driver sources (e.g.
        ``hwt901b``). Registered drivers are discovered from the plugin registry,
        so a new compass driver adds itself here without editing this file."""
        from .hardware import registry
        return self._SENSOR_SOURCES + tuple(registry.sources("compass"))

    def device_config(self) -> dict:
        """Current device/hardware config + the selectable options.

        Shape matches what :meth:`set_device_config` persists, plus ``options``
        (for the UI's selects) and ``restart_required`` (always ``False`` on a
        plain read; a POST returns ``True`` because devices are rebuilt only on
        restart, not hot-swapped)."""
        return {
            "hardware": asdict(self.config.hardware),
            "nmea_tcp": asdict(self.config.nmea_tcp),
            "options": {
                "sensor": list(self._SENSOR_SOURCES),
                "compass": list(self._compass_sources()),
                "motor": list(self._MOTOR_SOURCES),
            },
            "menus": self._device_menus(),
            "driver_menus": self._driver_menus(),
            "restart_required": False,
        }

    def set_device_config(self, payload: dict) -> dict:
        """Validate, persist, and apply a device-config edit.

        ``payload`` is ``{"hardware": {...}, "nmea_tcp": {...}}`` (either key
        optional). Validates source values + field types, writes
        ``devices.json``, and updates the in-memory ``config.hardware`` /
        ``config.nmea_tcp`` so a subsequent read reflects it. Devices are NOT
        hot-swapped; the change applies on the next restart, so the returned
        ``restart_required`` is ``True``. Raises :class:`ValueError` on a bad
        payload (the endpoint maps it to a 400)."""
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        hw_in = payload.get("hardware") or {}
        nmea_in = payload.get("nmea_tcp") or {}
        if not isinstance(hw_in, dict) or not isinstance(nmea_in, dict):
            raise ValueError("'hardware' and 'nmea_tcp' must be objects")

        # Build validated copies off the *current* config (so an edit can be
        # partial). Sources: sensors sim|serial|nmea, motor sim|serial|both.
        hw = HardwareConfig(**asdict(self.config.hardware))
        for dev in ("gps", "compass", "depth"):
            key = f"{dev}_source"
            allowed = self._compass_sources() if dev == "compass" else self._SENSOR_SOURCES
            if hw_in.get(key) is not None and hw_in[key] not in allowed:
                raise ValueError(
                    f"{key} must be one of {allowed} (got {hw_in[key]!r})"
                )
        if hw_in.get("motor_source") is not None and hw_in["motor_source"] not in self._MOTOR_SOURCES:
            raise ValueError(
                f"motor_source must be one of {self._MOTOR_SOURCES} (got {hw_in['motor_source']!r})"
            )
        # Ports are strings; baudrate is an int. Coerce/validate via the merge.
        for key in ("gps_port", "compass_port", "motor_port"):
            if key in hw_in and hw_in[key] is not None and not isinstance(hw_in[key], str):
                raise ValueError(f"{key} must be a string")
        for key, src in (("baudrate", hw_in), ("port", nmea_in)):
            if key in src and src[key] is not None:
                try:
                    int(src[key])
                except (TypeError, ValueError):
                    raise ValueError(f"{key} must be an integer") from None

        nmea = NmeaTcpConfig(**asdict(self.config.nmea_tcp))
        _merge_into(hw, hw_in)
        _merge_into(nmea, nmea_in)

        save_device_overrides(self.config.data_dir, hw, nmea)
        # Reflect the edit in the live config so a subsequent GET shows it
        # (the actual devices are rebuilt only on restart).
        self.config.hardware = hw
        self.config.nmea_tcp = nmea
        logger.info("device config updated (restart required to apply): %s", payload)
        # Persisted + reflected in-memory; devices are rebuilt on the next start.
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
        return SerialGps(PySerialTransport(hw.gps_port, baudrate=baud), self.bus)

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
                                  self.config.nmea_tcp)
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
        return SerialCompass(PySerialTransport(hw.compass_port, baudrate=hw.compass_baud), self.bus)

    def _build_serial_motor(self, cfg: AppConfig):
        from .hardware.serial_devices import SerialMotorController
        from .hardware.serial_link import PySerialTransport
        hw = cfg.hardware
        return SerialMotorController(PySerialTransport(hw.motor_port, baudrate=hw.motor_baud))

    def _construct_devices(self, cfg: AppConfig) -> dict:
        """Build the device set (simulator + sensors + motor) for ``cfg.hardware``.
        Returns a dict and does NOT mutate ``self`` — so a live reload can build
        the new set first and swap it in only on success (see reload_devices)."""
        src = {n: cfg.hardware.source(n) for n in ("gps", "compass", "depth", "motor")}
        # The sim boat exists whenever any device is simulated (sensors read its
        # truth; the sim motor drives it).
        simulator = None
        if any(s in ("sim", "both") for s in src.values()):
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
            )
        sim_motor = simulator.motor if simulator is not None else None
        if src["motor"] == "serial":
            motor = self._build_serial_motor(cfg)
        elif src["motor"] == "both":
            motor = _TeeMotor([sim_motor, self._build_serial_motor(cfg)])
        else:
            motor = sim_motor
        # "nmea" (or anything not sim/serial) builds NO internal sensor: the
        # navigator is fed by external NMEA over the bridge/inject instead.
        gps = compass = depth = None
        if src["gps"] == "serial":
            gps = self._build_serial_gps(cfg)
        elif src["gps"] == "sim":
            gps = SimGps(simulator.truth, self.bus, update_hz=cfg.sensors.gps_hz,
                         position_noise_m=cfg.sensors.gps_noise_m)
        if src["compass"] == "serial":
            compass = self._build_serial_compass(cfg)
        elif src["compass"] == "sim":
            compass = SimCompass(simulator.truth, self.bus, update_hz=cfg.sensors.compass_hz,
                                 heading_noise_deg=cfg.sensors.compass_noise_deg)
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
                self.bus, update_hz=cfg.sensors.depth_hz)
        return {"simulator": simulator, "gps": gps, "compass": compass,
                "depth_sounder": depth, "motor": motor}

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
            for d in (new["gps"], new["compass"], new["depth_sounder"]):
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
        for d in (self.gps, self.compass, self.depth_sounder):
            if d is not None:
                with contextlib.suppress(Exception):
                    await d.stop()
        if self.simulator is not None:
            self.simulator.stop()
        if self._sim_task is not None:
            self._sim_task.cancel()
        self.gps, self.compass, self.depth_sounder = new["gps"], new["compass"], new["depth_sounder"]
        self.simulator, self._sim_task = new["simulator"], new_sim_task
        # Swap the motor in, then stop the OLD one (closes its port + kills the
        # feedback task -> no port/task leak). Best-effort so a stubborn old
        # motor can't strand the reload.
        old_motor = self.controller.motor
        self.controller.motor = new["motor"]
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
        elif ctype == "weather_preset" and self.simulator is not None:
            self._apply_weather_preset(str(command.get("id", "")))
        elif ctype == "teleport":
            self._teleport(command)
        elif ctype == "inject_nmea":
            asyncio.ensure_future(self.bus.publish(events.NMEA_IN, str(command["sentence"])))
        elif ctype == "set_gps_offset":
            self.navigator.set_gps_offset(
                float(command["true_lat"]), float(command["true_lon"])
            )
        elif ctype == "clear_gps_offset":
            self.navigator.clear_gps_offset()
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
        else:
            self.controller.handle_command(command)

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
        """Battery telemetry. From the sim battery, or zeros if none (hardware
        battery monitor over the HAL will populate this later)."""
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
            # Driving by hand with the link gone -> cut the motor (STOP).
            logger.warning("link lost %.0fs while driving manually; STOP (zero thrust)", timeout)
            self.controller.handle_command({"type": "stop"})
        else:
            # Guided mode -> hold position (anchor-hold here).
            logger.warning("link lost %.0fs while underway; engaging hold-position", timeout)
            self.controller.handle_command({"type": "anchor_hold"})
        self._link_failsafe_engaged = True
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
            ("evaluate_rtl_recommend", self.evaluate_rtl_recommend),
            ("evaluate_link_failsafe", self.evaluate_link_failsafe),
            ("trip_update", lambda: self.trip.update(
                self.state.position, self.state.sog_knots, self._now_fn())),
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
        self.depth_map.record(sounding_pos, self.state.depth_m)

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
        self, dest_lat: float, dest_lon: float, mode: str = "fastest", offset_m: float = 25.0
    ) -> dict:
        """Plan a water-only route from the boat's current position.

        Synchronous and CPU/IO-heavy (Overpass fetch + shapely/networkx); the UI
        endpoint calls it in an executor. Returns the API contract dict. Does NOT
        start navigation.
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

        result = routing.plan_route(
            start_lat=start_lat,
            start_lon=start_lon,
            dest_lat=dest_lat,
            dest_lon=dest_lon,
            water_ll=water_ll,
            mode=mode,
            shoreline_offset_m=offset_m,
            cancelled=lambda: self._route_plan_cancelled,
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
        logger.info("applied weather preset %r", preset_id)

    def apply_tuned_gains(self, job: str, params: dict) -> None:
        """Apply auto-tuned gains to the live controller (used by /api/tune)."""
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
        merged in. Persists to ``depthmap.json`` so the import survives restarts."""
        from .nav.depth import parse_depth_features

        try:
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
            dm.contours = []
            dm.composition = []
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
        return out

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
        payload["health"] = self._health_snapshot()
        payload["battery"] = self.battery_snapshot()
        payload["link"] = {
            "client_connected": self._ui_clients > 0,
            "since_s": (
                round(self._mono_fn() - self._last_client_seen, 1)
                if self._last_client_seen is not None
                else None
            ),
            "failsafe_engaged": self._link_failsafe_engaged,
        }
        ctrl = self.controller
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
        if self.nmea_tcp is not None:
            # A taken NMEA-TCP port must not crash the whole app on boot.
            try:
                await self.nmea_tcp.start()
                logger.info("NMEA TCP server listening on port %s", self.nmea_tcp.bound_port)
            except Exception:
                logger.exception("NMEA TCP server failed to start; continuing without it")
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
        logger.info("runtime started (model=%s, hardware=%s)", self.config.sim.model, self.config.hardware.enabled)

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
        if self.nmea_tcp is not None:
            await self.nmea_tcp.stop()
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
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        log_level=(args.log_level or "info").lower(),
    )


if __name__ == "__main__":
    main()
