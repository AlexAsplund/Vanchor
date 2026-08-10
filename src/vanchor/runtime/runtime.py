"""The Runtime: owns every component and the background tasks that drive them.

Relocated out of the monolithic ``vanchor.app`` (issue #80). ``vanchor.app`` now
re-exports :class:`Runtime` for back-compat. The method bodies live in the sibling
``runtime/*`` collaborator modules; this class is the facade that wires them together
and runs the core control loop / lifecycle.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import time
from pathlib import Path

from ..controller.calibration import CalibrationRunner
from ..controller.controller import Controller, GainSchedule, Helm
from ..controller.modes import AnchorConfig, DriftConfig, FollowApbConfig, WaypointConfig
from ..controller.safety import BatteryLadder, SafetyConfig
from ..core import events, observability
from dataclasses import asdict

from ..core.config import (
    AppConfig,
    SafetyFloor,
    load,
)
from ..core.events import EventBus
from ..core.models import BoatState, ControlModeName, Environment, GeoPoint, Waypoint
from ..core.pid import PID
from ..core.state import NavigationState
from ..nav.depth import DepthMap
from ..nav.guard import SensorGuardConfig
from ..nav.navigator import Navigator
from ..nav.trip import TripLog
from ..hardware import registry
from ..hardware.drivers import load_drivers
from ..hardware.watchdog import HardwareWatchdog
from ..sim.bathymetry import Bathymetry
from ..sim.devices import SimCompass, SimDepthSounder, SimGps
from ..sim.simulator import Simulator

from .constants import (
    _ENV_PERSIST_KEYS,
    _MANUAL_UNDERWAY_THRUST_EPS,
    _UNDERWAY_MODES,
)
from .channels import (
    _NeutralChannelMotor,
    _TeeMotor,
    _SimChannelState,
    _SimThrustChannel,
    _SimSteeringChannel,
    _start_motor,
    _stop_motor,
)
from .builders import (
    _build_boat_params,
    _thrust_yaw_ff_norm,
    _make_fusion,
    _make_gps_filter,
    _build_battery_config,
    _mask_connector_settings,
    _overlay_menu_values,
)
from .boat_setup import BoatSetup
from .demo import demo_route_waypoints, apply_demo_mode
from .depth import DepthService
from .devices import DeviceManager
from .commands import CommandDispatcher
from .hardware_glue import HardwareGlue
from .hwscan import HardwareScan
from .nav_glue import NavGlue
from .safety_runtime import SafetyRuntime
from .sessions import SessionService
from .telemetry import TelemetryBuilder

logger = logging.getLogger("vanchor.app")

# Populate the pluggable device-driver registry (self-registering modules under
# hardware/drivers/). A new driver adds itself here just by existing.
load_drivers()

# Populate the pluggable connector registry (self-registering modules under
# connectors/). A new connector adds itself here just by existing.
from ..connectors import load_connectors  # noqa: E402

load_connectors()


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

        # --- Demo mode: force sim posture whenever demo.enabled is True ---- #
        # apply_demo_mode() is called by main() only for the --demo CLI flag.
        # When demo.enabled arrives via yaml (demo: enabled: true) or env
        # (VANCHOR_DEMO=1), the flag is set but the hardware/nmea_tcp/data_dir
        # posture is NOT forced.  Guard: if apply_demo_mode() was already called
        # upstream, the data_dir is an ephemeral tempdir (starts with the OS
        # temp dir prefix); skip a second call to avoid creating another tmpdir.
        if self.config.demo.enabled:
            import tempfile as _tf
            _already_applied = (
                not self.config.hardware.enabled
                and not self.config.nmea_tcp.enabled
                and self.config.data_dir.startswith(_tf.gettempdir())
            )
            if not _already_applied:
                # Preserve the existing readonly flag (may have been set via
                # yaml/env independently of the --demo-readonly CLI arg).
                apply_demo_mode(self.config, readonly=self.config.demo.readonly)

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
        from ..core.debug_recorder import DebugRecorder, ReplayPlayer

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
        # Hardware discovery/probe cluster (issue #79): source helpers, port
        # ownership, hw_scan/hw_probe, driver context/menus, split-channel
        # builder -- all live in HardwareScan; Runtime delegates.  Constructed
        # BEFORE _devices so the _driver_context / _build_split_channel shims
        # are ready when DeviceManager._construct_devices calls them at build
        # time, and BEFORE _safety so the battery-driver build path that calls
        # rt._driver_context() also resolves.
        self._hwscan = HardwareScan(self)
        # Device management cluster (issue #70): device config, construction,
        # status, health, debug -- all live in DeviceManager; Runtime delegates.
        self._devices = DeviceManager(self)
        # Failsafe + power + supervisor cluster (issue #76): battery source
        # discovery/monitor build, sim SoC setter, the link-loss deadman, push
        # alerts, RTL recommend + auto-RTL, the battery ladder, and the 1 Hz
        # supervisor loop all live in SafetyRuntime; Runtime delegates. Built
        # BEFORE _construct_devices so DeviceManager's battery-monitor build can
        # reach _build_battery_monitor / _battery_sources through the shims.
        self._safety = SafetyRuntime(self)
        # Connector + serial hardware-wiring cluster (issue #73): serial port
        # enumeration, hardware probing, serial device builders, and the
        # connector framework all live in HardwareGlue; Runtime delegates.
        # Constructed BEFORE _construct_devices so the serial builder shims
        # (_build_serial_gps, _build_serial_compass, _build_serial_motor) are
        # ready when DeviceManager._construct_devices calls them at build time.
        self._hw = HardwareGlue(self)
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
        # Depth cluster (issue #71): depth soundings, chart management, depth
        # grid/query, contour routing, import -- all live in DepthService.
        self._depth = DepthService(self)
        # Telemetry frame assembly (issue #74): battery_snapshot,
        # _supervisor_snapshot, _health_snapshot, telemetry -- all live in
        # TelemetryBuilder; Runtime delegates.
        self._telemetry = TelemetryBuilder(self)
        # Routing + anchor-alarm + fusion-cal cluster (issue #75): the 11
        # nav-glue methods live in NavGlue; Runtime delegates via shims or
        # direct repoints.
        self._nav = NavGlue(self)
        # Black-box / backup / replay cluster (issue #77): _build_blackbox,
        # _install_blackbox_hook, blackbox_dumps, blackbox_path_for,
        # create_backup, restore_backup, start_replay, stop_replay all live in
        # SessionService; Runtime delegates.
        self._sessions = SessionService(self)
        # Command-audit / dispatch cluster (issue #77): record_command,
        # command_audit, handle_command all live in CommandDispatcher; Runtime
        # delegates (handle_command keeps a shim because it is the command
        # entry point for every test and for the WS/REST server).
        self._commands = CommandDispatcher(self)

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
        from ..nav.calibration import load_calibration
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
        from ..connectors.registry import (
            armed as _conn_armed,
            load_grants as _load_grants,
            needs_reconsent as _conn_needs_reconsent,
            save_grants as _save_grants,
            spec as _conn_spec,
        )
        from ..connectors.base import manifest_hash as _manifest_hash

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
        # Guard against starting the runtime more than once. The CLI serves the
        # SAME app over two uvicorn servers (HTTP + HTTPS), and each server runs
        # the FastAPI lifespan -> start()/stop() are each called once PER server.
        # Without this, every subsystem ran twice: two controller loops driving
        # the motor, two simulator loops, two serial readers on one port (the
        # asyncio "readuntil() while another coroutine is already waiting" crash).
        # Ref-counted so the first start() actually starts and the last stop()
        # (when every server has shut down) actually tears down.
        self._start_count = 0
        self.calibration = CalibrationRunner(self)

        # --- Named boat profiles (#75, #89): persisted, selectable spec bundles.
        # On first run (no boats.json) seed a small set of ready-to-pick presets
        # with the bow trolling motor active; never clobber a user's saved
        # profiles. Then apply whichever profile is marked active so a saved
        # selection survives a restart.
        from ..core.boat_profiles import BoatProfileStore

        self.boats = BoatProfileStore(cfg.data_dir)
        # Boat-profile / specs / gains cluster (issue #72): all 18 methods live
        # in BoatSetup; Runtime delegates.  Construct BEFORE applying the active
        # profile so the first _apply_boat_specs / _apply_active_boat_gains calls
        # below go through the cluster.  BoatSetup.__init__ also loads
        # _boat_gains_path / _boat_gains from disk.
        self._boat = BoatSetup(self)
        active = self.boats.active()
        if active is not None:
            self._boat._apply_boat_specs(active["specs"])

        # --- Per-boat saved gain profiles (#31) -------------------------- #
        # Controller gains are now owned by BoatSetup (loaded in its __init__).
        # Apply the active profile's saved gains on top of its specs.
        self._boat._apply_active_boat_gains()

        # --- Server-persisted safety geometry (#23) ---------------------- #
        # No-go zones / min-depth / fix-failsafe live on the SERVER now, not just
        # the browser's localStorage. The governor is the live authority; this
        # store is the persistence layer. Load + APPLY at startup so a Pi restart
        # with NO client connected keeps the operator's zones/min-depth/failsafe.
        from ..core.prefs import PrefsStore, SafetyGeometryStore

        self.safety_geometry = SafetyGeometryStore(cfg.data_dir)
        self._apply_safety_geometry()
        # Generic UI-preferences KV store (browser-as-cache mechanism).
        self.prefs = PrefsStore(cfg.data_dir)

        # --- Passive anchor alarm (adoption #10) -------------------------- #
        # Motor-OFF watch circle. Pure observer of state.position, evaluated by
        # the 1 Hz supervisor, persisted like safety.json so a Pi restart with no
        # client connected keeps watching. It holds no reference to the
        # controller/motor and can never move the boat; "recover" goes through
        # the normal anchor_hold command path.
        from ..core.anchor_alarm import AnchorAlarmStore, AnchorAlarmWatcher

        self.anchor_alarm = AnchorAlarmWatcher(
            AnchorAlarmStore(cfg.data_dir),
            stale_fix_s=cfg.safety.anchor_alarm_stale_fix_s,
        )
        self.anchor_alarm.on_breach.append(
            lambda snap: logger.warning(
                "ANCHOR ALARM: %.0f m from alarm anchor (radius %.0f m)",
                snap.get("distance_m") or 0.0, snap.get("radius_m") or 0.0,
            )
        )

        # --- Web Push notifications (adoption #7) ------------------------ #
        # Observe-only: watchers in the 1 Hz supervisor mirror alerts.js's
        # edge-triggered conditions; delivery happens on a worker thread.
        from ..push import PushService
        self.push = PushService(cfg.data_dir, cfg.push, now_fn=self._mono_fn)
        self._push_prev: dict[str, bool] = {}   # edge memory for evaluate_push_alerts
        self._push_prev_cap: float = 1.0        # battery-ladder step memory

        # Also hook into anchor_alarm.on_breach for edge-triggered push on the
        # passive watch-circle alarm (mirrors the evaluate_push_alerts watcher but
        # fires immediately on breach rather than waiting for the next supervisor tick).
        self.anchor_alarm.on_breach.append(
            lambda snap: self.push.notify(
                "anchor_alarm",
                "Anchor watch alarm",
                "Boat outside the watch circle",
            )
        )
        # Mirror the breach into the server-side alert log (Task 1 D8) with the
        # ALARM POINT's coordinates so the history entry can "Show on map".
        self.anchor_alarm.on_breach.append(
            lambda snap: self.alert_log.record(
                "alarm",
                "Anchor watch alarm — boat outside the watch circle"
                + (f" ({snap.get('distance_m'):.0f} m out)"
                   if snap.get("distance_m") is not None else ""),
                kind="anchor_alarm",
                lat=snap.get("lat"), lon=snap.get("lon"),
            )
        )

        # --- Server-side alert log (Task 1 D8) ------------------------------ #
        # Persists the alert history so a new browser session can hydrate from
        # /api/alerts and merge with its localStorage copy.
        from pathlib import Path as _Path
        from ..core.alertlog import AlertLog
        self.alert_log: AlertLog = AlertLog(
            _Path(cfg.data_dir),
            max_entries=100,
        )

        # --- Supervisor link (host-side update/backup/disk daemon) --------- #
        if cfg.supervisor.enabled:
            from ..supervisor_client import SupervisorClient
            _token_file = (cfg.supervisor.token_file
                           or str(Path(cfg.data_dir) / "supervisor" / "token"))
            self.supervisor_link: "SupervisorClient | None" = SupervisorClient(
                cfg.supervisor.url, _token_file
            )
        else:
            self.supervisor_link = None
        self._supervisor_status: dict | None = None
        self._supervisor_status_at: float = 0.0

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
        self._sessions._build_blackbox(cfg)

        # --- External hardware watchdog heartbeat (#44) ------------------ #
        # A GPIO line the ~1 Hz supervisor must keep toggling or an external relay
        # cuts the motor supply -- covering a Pi hard-hang the firmware watchdog
        # cannot. OFF by default and a no-op until started, so building it here is
        # free. Uses the MONOTONIC clock so an RTC step can't skew the cadence.
        from ..core.config import WatchdogConfig

        self.watchdog = HardwareWatchdog.from_config(
            getattr(cfg, "watchdog", None) or WatchdogConfig(),
            now_fn=self._mono_fn,
        )

    def _build_blackbox(self, cfg: AppConfig) -> None:
        """Shim → SessionService (issue #77)."""
        return self._sessions._build_blackbox(cfg)

    def _install_blackbox_hook(self) -> None:
        """Shim → SessionService (issue #77)."""
        return self._sessions._install_blackbox_hook()

    # ------------------------------------------------------------------ #
    # Black-box flight recorder (#20) -- read API for the UI
    # ------------------------------------------------------------------ #
    def blackbox_dumps(self) -> dict:
        """Shim → SessionService (issue #77)."""
        return self._sessions.blackbox_dumps()

    def blackbox_path_for(self, file_name: str) -> str | None:
        """Shim → SessionService (issue #77)."""
        return self._sessions.blackbox_path_for(file_name)

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
    # Boat profile / specs / gains -- shims to BoatSetup (issue #72)
    # ------------------------------------------------------------------ #
    def boat_profile(self) -> dict:
        """Shim → BoatSetup (issue #72)."""
        return self._boat.boat_profile()

    def _apply_boat_specs(self, specs: dict) -> None:
        """Shim → BoatSetup (issue #72)."""
        return self._boat._apply_boat_specs(specs)

    def update_boat(self, fields: dict) -> dict:
        """Shim → BoatSetup (issue #72)."""
        return self._boat.update_boat(fields)

    def boat_profiles_list(self) -> dict:
        """Shim → BoatSetup (issue #72)."""
        return self._boat.boat_profiles_list()

    def boat_profiles_create(self, name: str, specs: dict | None = None) -> dict:
        """Shim → BoatSetup (issue #72)."""
        return self._boat.boat_profiles_create(name, specs)

    def boat_profiles_update(
        self, profile_id: str, name: str | None = None, specs: dict | None = None
    ) -> dict | None:
        """Shim → BoatSetup (issue #72)."""
        return self._boat.boat_profiles_update(profile_id, name, specs)

    def boat_profiles_activate(self, profile_id: str) -> dict | None:
        """Shim → BoatSetup (issue #72)."""
        return self._boat.boat_profiles_activate(profile_id)

    def boat_profiles_delete(self, profile_id: str) -> bool:
        """Shim → BoatSetup (issue #72)."""
        return self._boat.boat_profiles_delete(profile_id)

    def save_boat_gains(self, profile_id: str | None = None) -> dict:
        """Shim → BoatSetup (issue #72)."""
        return self._boat.save_boat_gains(profile_id)

    def boat_gains(self, profile_id: str | None = None) -> dict:
        """Shim → BoatSetup (issue #72)."""
        return self._boat.boat_gains(profile_id)

    def apply_tuned_gains(self, job: str, params: dict, *, persist: bool = False) -> None:
        """Shim → BoatSetup (issue #72)."""
        return self._boat.apply_tuned_gains(job, params, persist=persist)

    # ------------------------------------------------------------------ #
    # Versioned backup / restore of all persistent state
    # ------------------------------------------------------------------ #
    def create_backup(self, client: dict | None = None, *, created_at: str | None = None) -> bytes:
        """Shim → SessionService (issue #77)."""
        return self._sessions.create_backup(client=client, created_at=created_at)

    def restore_backup(self, zip_bytes: bytes) -> dict:
        """Shim → SessionService (issue #77)."""
        return self._sessions.restore_backup(zip_bytes)

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
        """Shim → SafetyRuntime (issue #76)."""
        return self._safety._battery_sources()

    def _compass_sources(self) -> tuple:
        """Shim → HardwareScan (issue #79)."""
        return self._hwscan._compass_sources()

    def _gps_sources(self) -> tuple:
        """Shim → HardwareScan (issue #79)."""
        return self._hwscan._gps_sources()

    def list_serial_ports(self) -> list[dict]:
        """Shim → HardwareGlue (issue #73)."""
        return self._hw.list_serial_ports()

    # -- Hardware setup wizard: scan / probe endpoints (adoption pack #2) ---- #

    def _ports_in_use(self) -> dict[str, str]:
        """Shim → HardwareScan (issue #79)."""
        return self._hwscan._ports_in_use()

    def _i2c_addrs_in_use(self) -> set[tuple[int, int]]:
        """Shim → HardwareScan (issue #79)."""
        return self._hwscan._i2c_addrs_in_use()

    def hw_scan(self) -> dict:
        """Shim → HardwareScan (issue #79)."""
        return self._hwscan.hw_scan()

    async def hw_probe(self, payload: dict) -> dict:
        """Shim → HardwareScan (issue #79)."""
        return await self._hwscan.hw_probe(payload)

    @property
    def _hw_probe_lock(self):
        """Shim → HardwareScan (issue #79); kept for test back-compat."""
        return self._hwscan._hw_probe_lock

    # -- fusion calibration (still-capture system-ID; see nav.calibration) --- #
    def fusion_calibration(self) -> dict:
        """Shim → NavGlue (issue #75)."""
        return self._nav.fusion_calibration()

    def set_interference_compensation(self, enabled: bool) -> dict:
        """EXPERIMENTAL: toggle the real-time motor-interference heading remedy
        (needs an interference calibration to have any effect)."""
        from ..nav.calibration import FusionCalibration, save_calibration
        cal = self._fusion_cal or FusionCalibration()
        cal.interference_comp_enabled = bool(enabled)
        save_calibration(self.config.data_dir, cal)
        self._fusion_cal = cal
        self.navigator.apply_calibration(cal)
        return {"ok": True, "enabled": bool(enabled),
                "has_model": cal.motor_interference_slope is not None}

    def start_fusion_capture(self, mode: str = "still") -> dict:
        from ..nav.calibration import CAPTURE_MODES
        if self.navigator.fusion is None:
            return {"ok": False, "error": "fusion is disabled"}
        if mode not in CAPTURE_MODES:
            return {"ok": False, "error": f"unknown mode {mode!r}"}
        self._capture_mode = mode
        self.navigator.start_capture()
        return {"ok": True, "capturing": True, "mode": mode}

    def stop_fusion_capture(self) -> dict:
        from ..nav.calibration import tune
        buf = self.navigator.stop_capture()
        if buf is None:
            return {"ok": False, "error": "no capture was running"}
        from ..nav.calibration import interference_recommendations
        mode = getattr(self, "_capture_mode", "still")
        cal, warnings = tune(buf, mode)
        out = {"ok": True, "mode": mode, "calibration": cal.to_dict(), "warnings": warnings}
        if mode == "interference":
            out["recommendations"] = interference_recommendations(cal.motor_interference_score)
        return out

    def save_fusion_calibration(self, data: dict) -> dict:
        """Shim → NavGlue (issue #75)."""
        return self._nav.save_fusion_calibration(data)

    def reset_fusion_calibration(self) -> dict:
        """Shim → NavGlue (issue #75)."""
        return self._nav.reset_fusion_calibration()

    def device_config(self) -> dict:
        """Delegated to DeviceManager (issue #70)."""
        return self._devices.device_config()

    def sim_motor_config(self) -> dict:
        """Current simulated-motor actuation-shaping config (#36) as a plain dict.

        A small standalone reader kept separate from :meth:`device_config` so the
        (frozen) device-config response shape is unchanged; the values are still
        editable through :meth:`set_device_config`'s ``sim_motor`` block."""
        return asdict(self.config.sim_motor)

    def set_device_config(self, payload: dict) -> dict:
        """Delegated to DeviceManager (issue #70)."""
        return self._devices.set_device_config(payload)

    def _build_serial_gps(self, cfg: AppConfig):
        """Shim → HardwareGlue (issue #73)."""
        return self._hw._build_serial_gps(cfg)

    def _device_menus(self) -> list:
        """Delegated to DeviceManager (issue #70)."""
        return self._devices._device_menus()

    def _driver_menus(self) -> dict:
        """Shim → HardwareScan (issue #79)."""
        return self._hwscan._driver_menus()

    def _device_by_kind(self, kind: str):
        """Delegated to DeviceManager (issue #70)."""
        return self._devices._device_by_kind(kind)

    def apply_device_setting(self, kind: str, key: str, value) -> dict:
        """Delegated to DeviceManager (issue #70)."""
        return self._devices.apply_device_setting(kind, key, value)

    def run_device_action(self, kind: str, name: str, params: dict | None = None) -> dict:
        """Delegated to DeviceManager (issue #70)."""
        return self._devices.run_device_action(kind, name, params)

    def _build_serial_compass(self, cfg: AppConfig):
        """Shim → HardwareGlue (issue #73)."""
        return self._hw._build_serial_compass(cfg)

    def _build_serial_motor(self, cfg: AppConfig):
        """Shim → HardwareGlue (issue #73)."""
        return self._hw._build_serial_motor(cfg)

    def _build_split_channel(
        self,
        name: str,
        link: dict | None,
        sim_motor,
        sim_state: "_SimChannelState | None",
        cfg: AppConfig,
    ):
        """Shim → HardwareScan (issue #79)."""
        return self._hwscan._build_split_channel(name, link, sim_motor, sim_state, cfg)

    def _construct_devices(self, cfg: AppConfig) -> dict:
        """Delegated to DeviceManager (issue #70)."""
        return self._devices._construct_devices(cfg)

    def _driver_context(self, kind: str, source: str, config):
        """Shim → HardwareScan (issue #79)."""
        return self._hwscan._driver_context(kind, source, config)

    def _build_battery_monitor(self, cfg: AppConfig, simulator):
        """Shim → SafetyRuntime (issue #76)."""
        return self._safety._build_battery_monitor(cfg, simulator)

    async def reload_devices(self) -> dict:
        """Delegated to DeviceManager (issue #70)."""
        return await self._devices.reload_devices()

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
        """Shim → SessionService (issue #77)."""
        return self._sessions.start_replay(file_name)

    def stop_replay(self) -> None:
        """Shim → SessionService (issue #77)."""
        return self._sessions.stop_replay()

    # ------------------------------------------------------------------ #
    # Command audit log (#26)
    # ------------------------------------------------------------------ #
    def record_command(
        self, ctype: object, source: str, outcome: str, detail: str | None = None
    ) -> None:
        """Shim → CommandDispatcher (issue #77)."""
        return self._commands.record_command(ctype, source, outcome, detail)

    def command_audit(self, n: int = 50) -> dict:
        """Shim → CommandDispatcher (issue #77)."""
        return self._commands.command_audit(n)

    def handle_command(self, command: dict) -> None:
        """Shim → CommandDispatcher (issue #77). THE command entry point for the
        WS/REST server and the test suite -- kept as a shim with the exact
        signature so every caller and every ``rt.handle_command`` monkeypatch
        keeps working."""
        return self._commands.handle_command(command)

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
        """Shim → SafetyRuntime (issue #76)."""
        return self._safety._set_battery(soc_pct)

    # ------------------------------------------------------------------ #
    # Passive anchor alarm recover (adoption #10)
    # ------------------------------------------------------------------ #
    def _anchor_alarm_recover(self) -> dict:
        """Repoint → NavGlue (issue #75)."""
        return self._nav._anchor_alarm_recover()

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
        """Shim → TelemetryBuilder (issue #74)."""
        return self._telemetry.battery_snapshot()

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
        """Shim → SafetyRuntime (issue #76)."""
        return self._safety._underway()

    def evaluate_link_failsafe(self, now: float | None = None) -> bool:
        """Shim → SafetyRuntime (issue #76)."""
        return self._safety.evaluate_link_failsafe(now)

    def evaluate_auto_apb(self, now: float | None = None) -> bool:
        """Repoint → NavGlue (issue #75)."""
        return self._nav.evaluate_auto_apb(now)

    def evaluate_anchor_alarm(self) -> dict:
        """Repoint → NavGlue (issue #75)."""
        return self._nav.evaluate_anchor_alarm()

    def evaluate_push_alerts(self) -> None:
        """Shim → SafetyRuntime (issue #76)."""
        return self._safety.evaluate_push_alerts()

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
            from ..nav import water as _water
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
        """Shim → SafetyRuntime (issue #76)."""
        return self._safety.evaluate_rtl_recommend()

    def _schedule_auto_rtl(self) -> None:
        """Shim → SafetyRuntime (issue #76)."""
        return self._safety._schedule_auto_rtl()

    async def _run_auto_rtl(self, loop) -> None:
        """Shim → SafetyRuntime (issue #76)."""
        return await self._safety._run_auto_rtl(loop)

    # ------------------------------------------------------------------ #
    # Low-battery thrust-derating ladder (#49)
    # ------------------------------------------------------------------ #
    def evaluate_battery_ladder(self) -> float:
        """Shim → SafetyRuntime (issue #76)."""
        return self._safety.evaluate_battery_ladder()

    def _battery_rtl_handoff(self, soc_pct: float) -> None:
        """Shim → SafetyRuntime (issue #76)."""
        return self._safety._battery_rtl_handoff(soc_pct)

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
            ("evaluate_anchor_alarm", self.evaluate_anchor_alarm),
            ("evaluate_auto_apb", self.evaluate_auto_apb),
            ("refresh_land_guard_water", self.refresh_land_guard_water),
            ("trip_update", lambda: self.trip.update(
                self.state.position, self.state.sog_knots, self._now_fn())),
            # Web Push alert dispatcher (adoption #7): edge-triggered, after
            # battery/link evaluators so their flags are fresh this tick.
            ("evaluate_push_alerts", self.evaluate_push_alerts),
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

    async def _run_demo_scenario(self) -> None:
        """Demo mode: once the sim GPS has produced a fix, engage the seeded
        scenario through the NORMAL command path (handle_command), so every
        governor/failsafe applies exactly as for a human command. One-shot."""
        cfg = self.config.demo
        # Wait (max ~20 s) for the navigator to have a position.
        for _ in range(100):
            if self.state.position is not None:
                break
            await asyncio.sleep(0.2)
        else:
            logger.warning("demo scenario: no fix after 20 s; skipping seed")
            return
        if self.state.mode != ControlModeName.MANUAL:
            return  # someone already engaged something (e.g. a fast client)
        if abs(self.state.motor_command.thrust) > 0.05:
            return  # operator is actively driving; skip demo seeding
        try:
            if cfg.weather_preset:
                self.handle_command({"type": "weather_preset", "id": cfg.weather_preset})
            if cfg.scenario == "anchor":
                self.handle_command({"type": "anchor_hold", "radius_m": 8})
            else:  # "route" (default; unknown values fall back here)
                p = self.state.position
                self.handle_command({
                    "type": "goto",
                    "waypoints": demo_route_waypoints(p.lat, p.lon),
                    "throttle": 0.6,
                    "loop": True,
                })
            logger.info("demo scenario %r engaged", cfg.scenario)
        except Exception:  # noqa: BLE001 - a demo seed must never kill the runtime
            logger.exception("demo scenario failed to engage")

    async def _run_supervisor_client(self) -> None:
        """Shim → SafetyRuntime (issue #76)."""
        return await self._safety._run_supervisor_client()

    def _supervisor_snapshot(self) -> dict:
        """Shim → TelemetryBuilder (issue #74)."""
        return self._telemetry._supervisor_snapshot()

    async def _run_supervisor(self, period_s: float = 1.0) -> None:
        """Shim → SafetyRuntime (issue #76)."""
        return await self._safety._run_supervisor(period_s)

    def record_depth_sounding(self) -> None:
        """Shim → DepthService (issue #71)."""
        return self._depth.record_depth_sounding()

    def _update_depth_divergence(self, position: "GeoPoint | None" = None) -> None:
        """Shim → DepthService (issue #71)."""
        return self._depth._update_depth_divergence(position)

    async def _maybe_persist_depth(self) -> None:
        """Shim → DepthService (issue #71)."""
        return await self._depth._maybe_persist_depth()

    def _load_route(self, command: dict) -> None:
        """Repoint → NavGlue (issue #75)."""
        return self._nav._load_route(command)

    # ------------------------------------------------------------------ #
    # Smart "Take me here" water routing (task #43)
    # ------------------------------------------------------------------ #
    def plan_route(
        self, dest_lat: float, dest_lon: float, mode: str = "fastest",
        offset_m: float = 25.0, depth_aware: bool = True,
    ) -> dict:
        """Shim → NavGlue (issue #75)."""
        return self._nav.plan_route(dest_lat, dest_lon, mode, offset_m, depth_aware)

    def cancel_route_plan(self) -> None:
        """Shim → NavGlue (issue #75)."""
        return self._nav.cancel_route_plan()

    # ------------------------------------------------------------------ #
    # "Around island" loop route (#77)
    # ------------------------------------------------------------------ #
    def plan_island_loop(
        self, click_lat: float, click_lon: float, offset_m: float = 20.0
    ) -> dict:
        """Shim → NavGlue (issue #75)."""
        return self._nav.plan_island_loop(click_lat, click_lon, offset_m)

    # ------------------------------------------------------------------ #
    # Area survey "map mode" route (#47)
    # ------------------------------------------------------------------ #
    def plan_survey(
        self, polygon_latlon: list, spacing_m: float, angle_deg: float | None = None
    ) -> dict:
        """Shim → NavGlue (issue #75)."""
        return self._nav.plan_survey(polygon_latlon, spacing_m, angle_deg)

    def plan_work_spots(self, polygon_latlon: list, spacing_m: float) -> dict:
        """Generate Work Area spots: an even serpentine grid over a drawn area,
        clipped to water (spots on land are dropped). Pure CPU (shapely) + the
        offline water cache; the UI endpoint calls it in an executor. Returns
        ``{ok, waypoints, message}`` -- the UI loads these as the Work Area spots."""
        from ..nav import survey, water as water_mod
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
        """Shim → DepthService (issue #71)."""
        return self._depth.contour_route(lat, lon, window_m)

    # ------------------------------------------------------------------ #
    # Offline chart prefetch + management (#52)
    # ------------------------------------------------------------------ #
    def prefetch_chart(self, bbox: list) -> dict:
        """Shim → DepthService (issue #71)."""
        return self._depth.prefetch_chart(bbox)

    def list_charts(self) -> dict:
        """Shim → DepthService (issue #71)."""
        return self._depth.list_charts()

    def clear_charts(self) -> dict:
        """Shim → DepthService (issue #71)."""
        return self._depth.clear_charts()

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
        from ..sim.weather import WEATHER_PRESETS

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

    # ------------------------------------------------------------------ #
    # Depth-map gridding (server-side averaging for the depth overlay)
    # ------------------------------------------------------------------ #
    def depth_grid(self, cell_m: float = 15.0, bbox=None, field: str = "depth") -> dict:
        """Shim → DepthService (issue #71)."""
        return self._depth.depth_grid(cell_m, bbox, field)

    def depth_at(self, lat: float, lon: float) -> dict:
        """Shim → DepthService (issue #71)."""
        return self._depth.depth_at(lat, lon)

    def depth_contours(self, bbox=None, limit: int = 20000) -> dict:
        """Shim → DepthService (issue #71)."""
        return self._depth.depth_contours(bbox, limit)

    def depth_composition(self, bbox=None, limit: int = 30000) -> dict:
        """Shim → DepthService (issue #71)."""
        return self._depth.depth_composition(bbox, limit)

    def composition_tile(self, z: int, x: int, y: int) -> bytes:
        """Shim → DepthService: a composition raster-tile PNG (#117)."""
        return self._depth.composition_tile(z, x, y)

    def contours_tile(self, z: int, x: int, y: int) -> bytes:
        """Shim → DepthService: a depth-contour raster-tile PNG (#118)."""
        return self._depth.contours_tile(z, x, y)

    def tiles_info(self) -> dict:
        """Shim → DepthService: static-chart tile-layer metadata (#116)."""
        return self._depth.tiles_info()

    def set_tiles_mode(self, mode: str) -> dict:
        """Shim → DepthService: set tile invalidation mode auto/static (#119)."""
        return self._depth.set_tiles_mode(mode)

    def clear_tiles(self) -> dict:
        """Shim → DepthService: wipe the server tile cache (#119)."""
        return self._depth.clear_tiles()

    def pregenerate_tiles(self, zmax: int = 16) -> dict:
        """Shim → DepthService: batch-render the chart's tile pyramid (#121)."""
        return self._depth.pregenerate_tiles(zmax)

    def water_polygon(self, bbox) -> dict:
        """OSM water polygon(s) for a (west, south, east, north) bbox, used to
        CLIP the depth overlays to water (don't draw composition over land). Uses
        the same offline WaterCache as routing; fetches from Overpass + caches if
        absent (so offline it needs the area pre-downloaded). Returns
        ``{ok, water}`` where water is GeoJSON-style MultiPolygon coords
        ``[[[ [lon,lat], ... ]=exterior, [ ... ]=hole, ... ], ...]`` (empty if none)."""
        from ..nav import water
        w, s, e, n = bbox
        wbbox = (s, w, n, e)                       # water.py order: (S, W, N, E)
        cache = water.WaterCache(self.config.data_dir)
        try:
            geom = cache.find_covering(wbbox)
            if geom is None:
                from .fetch_relay import relay_http_post
                _post = relay_http_post(getattr(self, "fetch_relay", None))
                geom = water.assemble_water(water.fetch_overpass(*wbbox, http_post=_post))
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
        """Shim → DepthService (issue #71)."""
        return self._depth.import_depth_map(filename, data, replace)

    def _health_snapshot(self) -> dict:
        """Shim → TelemetryBuilder (issue #74)."""
        return self._telemetry._health_snapshot()

    def _device_health(self, now: float | None = None) -> dict:
        """Delegated to DeviceManager (issue #70)."""
        return self._devices._device_health(now)

    def _device_connected_map(self, cfg: AppConfig) -> dict:
        """Delegated to DeviceManager (issue #70)."""
        return self._devices._device_connected_map(cfg)

    def device_status(self) -> dict:
        """Delegated to DeviceManager (issue #70)."""
        return self._devices.device_status()

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
        """Delegated to DeviceManager (issue #70)."""
        return self._devices.device_debug(kind)

    def all_device_debug(self) -> dict:
        """Delegated to DeviceManager (issue #70)."""
        return self._devices.all_device_debug()

    # ------------------------------------------------------------------ #
    # Connector framework (consent-gated bus bridges)
    # ------------------------------------------------------------------ #

    def _make_connector_sink(self, name: str):
        """Shim → HardwareGlue (issue #73)."""
        return self._hw._make_connector_sink(name)

    def connector_status(self) -> list[dict]:
        """Shim → HardwareGlue (issue #73)."""
        return self._hw.connector_status()

    async def set_connector_armed(self, name: str, enabled: bool) -> dict:
        """Shim → HardwareGlue (issue #73)."""
        return await self._hw.set_connector_armed(name, enabled)

    async def set_connector_settings(self, name: str, values: dict) -> dict:
        """Shim → HardwareGlue (issue #73)."""
        return await self._hw.set_connector_settings(name, values)

    def connector_debug(self, name: str) -> dict:
        """Shim → HardwareGlue (issue #73)."""
        return self._hw.connector_debug(name)

    def telemetry(self) -> dict:
        """Shim → TelemetryBuilder (issue #74)."""
        return self._telemetry.telemetry()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        # Idempotent across multiple server lifespans (HTTP + HTTPS share one
        # Runtime): only the first start() boots the subsystems; later ones are
        # no-ops so nothing (controller, simulator, serial readers) runs twice.
        self._start_count += 1
        if self._start_count > 1:
            logger.debug("runtime start() ignored (already started, count=%d)",
                         self._start_count)
            return
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
        self._tasks.append(asyncio.ensure_future(self._run_supervisor_client()))
        if self.config.demo.enabled:
            self._tasks.append(asyncio.ensure_future(self._run_demo_scenario()))
        # Start armed connectors (after devices are up so the bus is ready).
        # A connector that fails to build or start is logged and skipped;
        # it NEVER crashes the whole app (mirror the compass-driver resilience).
        await self._start_armed_connectors()
        logger.info("runtime started (model=%s, hardware=%s)", self.config.sim.model, self.config.hardware.enabled)

    async def _start_armed_connectors(self) -> None:
        """Shim → HardwareGlue (issue #73)."""
        await self._hw._start_armed_connectors()

    async def stop(self) -> None:
        # Mirror start()'s ref-count: tear down only when the LAST server that
        # started us shuts down. A stop() with servers still running is a no-op.
        if self._start_count > 0:
            self._start_count -= 1
        if self._start_count > 0:
            logger.debug("runtime stop() deferred (%d server(s) still up)",
                         self._start_count)
            return
        # Stop the Web Push worker thread before task cleanup (best-effort).
        with contextlib.suppress(Exception):
            self.push.stop()
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

