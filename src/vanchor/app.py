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
import tempfile
import time
from pathlib import Path

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

# Module-level helpers extracted to runtime/ sub-package (issue #69).
from .runtime.channels import (  # noqa: E402
    _NeutralChannelMotor,
    _TeeMotor,
    _SimChannelState,
    _SimThrustChannel,
    _SimSteeringChannel,
    _start_motor,
    _stop_motor,
)
from .runtime.builders import (  # noqa: E402
    _build_boat_params,
    _thrust_yaw_ff_norm,
    _make_fusion,
    _make_gps_filter,
    _build_battery_config,
    _mask_connector_settings,
    _overlay_menu_values,
)
from .runtime.boat_setup import BoatSetup  # noqa: E402
from .runtime.demo import demo_route_waypoints, apply_demo_mode  # noqa: E402
from .runtime.depth import DepthService  # noqa: E402
from .runtime.devices import DeviceManager  # noqa: E402
from .runtime.hardware_glue import HardwareGlue  # noqa: E402

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
        # Device management cluster (issue #70): device config, construction,
        # status, health, debug -- all live in DeviceManager; Runtime delegates.
        self._devices = DeviceManager(self)
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
        from .core.prefs import PrefsStore, SafetyGeometryStore

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
        from .core.anchor_alarm import AnchorAlarmStore, AnchorAlarmWatcher

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
        from .push import PushService
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
        from .core.alertlog import AlertLog
        self.alert_log: AlertLog = AlertLog(
            _Path(cfg.data_dir),
            max_entries=100,
        )

        # --- Supervisor link (host-side update/backup/disk daemon) --------- #
        if cfg.supervisor.enabled:
            from .supervisor_client import SupervisorClient
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
        # Lock for the hardware probe wizard: only one port may be probed at a
        # time (opens a real serial port / I2C bus briefly). If another probe
        # is already running the endpoint returns 409 immediately.
        self._hw_probe_lock = asyncio.Lock()

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
                self._boat._apply_boat_specs(active["specs"])
            # Per-boat gains (#31) live in a sidecar; reload + re-apply too.
            self._boat._boat_gains = self._boat._load_boat_gains()
            self._boat._apply_active_boat_gains()
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
        """Shim → HardwareGlue (issue #73)."""
        return self._hw.list_serial_ports()

    # -- Hardware setup wizard: scan / probe endpoints (adoption pack #2) ---- #

    def _ports_in_use(self) -> dict[str, str]:
        """realpath(serial port) -> owning device kind, for every ACTIVE device
        whose driver holds a serial port open.

        Conservative: if in doubt, mark in use. I2C port strings (``i2c:...``)
        are handled by :meth:`_i2c_addrs_in_use`; serial strings only here.
        """
        import os

        hw = self.config.hardware
        result: dict[str, str] = {}

        def _add(port: str, kind: str) -> None:
            if not port or port.startswith("i2c:"):
                return
            try:
                result[os.path.realpath(port)] = kind
            except OSError:
                result[port] = kind

        # GPS
        if self.gps is not None and hw.source("gps") in ("serial", "ublox"):
            _add(hw.gps_port, "gps")

        # Compass
        if self.compass is not None and hw.source("compass") in ("serial", "hwt901b"):
            _add(hw.compass_port, "compass")

        # Motor — derive from the link plan (conservative: mark if source touches serial)
        try:
            from .hardware.link_plan import plan_motor_links
            plan = plan_motor_links(hw)
            if plan.kind == "combined":
                if plan.source in ("serial", "both") and plan.link:
                    _add(plan.link.get("port", ""), "motor")
            elif plan.kind == "split":
                for _ch_dict in (plan.steering, plan.thrust):
                    if _ch_dict and _ch_dict.get("source") in ("serial", "both"):
                        _add(_ch_dict.get("port", ""), "motor")
        except Exception:  # noqa: BLE001
            # If plan_motor_links raises, be conservative
            _p = getattr(hw, "motor_port", "") or ""
            if _p and not _p.startswith("i2c:"):
                _add(_p, "motor")

        return result

    def _i2c_addrs_in_use(self) -> set[tuple[int, int]]:
        """(bus, addr) pairs currently owned by a running driver on an I2C port."""
        hw = self.config.hardware
        result: set[tuple[int, int]] = set()
        ports_to_check: list[str] = []

        try:
            from .hardware.link_plan import plan_motor_links
            plan = plan_motor_links(hw)
            if plan.kind == "combined":
                if plan.link:
                    _p = plan.link.get("port", "")
                    if _p and _p.startswith("i2c:"):
                        ports_to_check.append(_p)
            elif plan.kind == "split":
                for _ch_dict in (plan.steering, plan.thrust):
                    if _ch_dict:
                        _p = _ch_dict.get("port", "") or ""
                        if _p.startswith("i2c:"):
                            ports_to_check.append(_p)
        except Exception:  # noqa: BLE001
            _p = getattr(hw, "motor_port", "") or ""
            if _p.startswith("i2c:"):
                ports_to_check = [_p]

        for _port in ports_to_check:
            _parts = _port.split(":")
            try:
                _bus_n = int(_parts[1])
                _addr_n = int(_parts[2], 0) if len(_parts) > 2 else 0x42
                result.add((_bus_n, _addr_n))
            except (IndexError, ValueError):
                pass

        return result

    def hw_scan(self) -> dict:
        """Enumerate candidate hardware endpoints WITHOUT opening any of them.

        Returns serial ports (list_serial_ports with ownership annotation),
        I2C buses, the statically known I2C device list, and capability flags.
        In demo mode returns a graceful empty posture (no hardware scanning).
        """
        import glob
        import importlib.util
        import os
        from .hardware.probe import hint_from_metadata

        caps = {
            "serial": importlib.util.find_spec("serial_asyncio") is not None,
            "i2c": importlib.util.find_spec("smbus2") is not None,
        }

        # Known I2C devices (static)
        _ina = getattr(self.config, "battery", None)
        _ina_addr = getattr(_ina, "i2c_addr", 0x40) if _ina else 0x40
        known_i2c = [
            {"kind": "helm-pico", "addr": "0x42",
             "label": "Vanchor helm PCB (motor tunnel)"},
            {"kind": "ina226", "addr": f"0x{_ina_addr:02x}",
             "label": "INA226 battery shunt"},
        ]

        # Demo mode: return sim posture without scanning real hardware
        if getattr(self.config, "demo", None) and self.config.demo.enabled:
            return {"ports": [], "i2c_buses": [], "known_i2c": known_i2c,
                    "capabilities": caps}

        in_use = self._ports_in_use()
        i2c_owned = self._i2c_addrs_in_use()

        # Serial ports: list_serial_ports() + ownership + metadata hint
        ports = []
        for _entry in self.list_serial_ports():
            _path = _entry["path"]
            _desc = _entry.get("description", "")
            try:
                _rp = os.path.realpath(_path)
            except OSError:
                _rp = _path
            ports.append({
                **_entry,
                "in_use": in_use.get(_rp),
                "hint": hint_from_metadata(_path, _desc),
            })

        # I2C buses
        i2c_buses = []
        for _dev in sorted(glob.glob("/dev/i2c-[0-9]*")):
            try:
                _bus_n = int(_dev.split("-")[-1])
            except ValueError:
                continue
            _owned = any(_b == _bus_n for (_b, _) in i2c_owned)
            i2c_buses.append({"bus": _bus_n, "path": _dev, "in_use": _owned})

        return {"ports": ports, "i2c_buses": i2c_buses, "known_i2c": known_i2c,
                "capabilities": caps}

    async def hw_probe(self, payload: dict) -> dict:
        """Briefly open ONE candidate endpoint, identify it, return a live sample.

        Serialised by self._hw_probe_lock. Refuses endpoints owned by a running
        driver (409) and concurrent probe requests (409).

        SAFETY: Serial probing is passive (zero writes) unless active_ubx_ident
        is requested AND the passive stage classified the port as a GNSS
        candidate. The motor probe writes MOTOR_INFO_CMD (INFO+CRC, read-only
        identify) as the sole sanctioned motor write, then listens for the
        firmware's INFO response and unsolicited A/E broadcast. See
        hardware/probe.py docstring.
        """
        from .hardware import probe as probe_mod

        if self._hw_probe_lock.locked():
            return {"ok": False, "error": "another probe is already running"}

        async with self._hw_probe_lock:
            target = payload.get("target")
            if target not in ("serial", "i2c"):
                raise ValueError(f"unknown target {target!r}; must be 'serial' or 'i2c'")

            if target == "serial":
                return await self._hw_probe_serial(payload, probe_mod)
            return await self._hw_probe_i2c(payload, probe_mod)

    async def _hw_probe_serial(self, payload: dict, probe_mod) -> dict:
        """Shim → HardwareGlue (issue #73)."""
        return await self._hw._hw_probe_serial(payload, probe_mod)

    async def _hw_probe_i2c(self, payload: dict, probe_mod) -> dict:
        """I2C probe — called from hw_probe under the lock."""
        _bus_raw = payload.get("bus")
        _addr_raw = payload.get("addr")
        _kind = str(payload.get("kind", "auto"))

        if _bus_raw is None:
            raise ValueError("'bus' is required for i2c target")
        try:
            _bus = int(_bus_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid bus {_bus_raw!r}") from exc
        if _bus < 0:
            raise ValueError(f"bus must be >= 0, got {_bus}")

        if _addr_raw is None:
            raise ValueError("'addr' is required for i2c target")
        try:
            _addr = int(str(_addr_raw), 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid addr {_addr_raw!r}") from exc
        if not (0x03 <= _addr <= 0x77):
            raise ValueError(f"i2c addr 0x{_addr:02X} out of range 0x03..0x77")

        if _kind not in ("auto", "helm-pico", "ina226"):
            raise ValueError(f"unknown i2c kind {_kind!r}")

        # Ownership check
        _owned = self._i2c_addrs_in_use()
        if (_bus, _addr) in _owned:
            return {
                "ok": False, "conflict": True,
                "error": (
                    f"i2c:{_bus}:0x{_addr:02X} is in use by the running motor driver"
                    " — pick another or change Devices config first"
                ),
            }

        _raw = await asyncio.to_thread(probe_mod.probe_i2c, _bus, _addr, _kind)

        # Build response inline
        _detected = _raw.get("detected", "unknown")
        _resp: dict = {
            "ok": True, "target": "i2c",
            "bus": _bus, "addr": f"0x{_addr:02x}",
            "detected": _detected, "sample": _raw.get("sample", {}),
        }
        if _detected == "helm-pico":
            _resp["suggest"] = {
                "kind": "motor", "source": "serial",
                "fields": {"motor_source": "serial",
                           "motor_port": f"i2c:{_bus}:0x{_addr:02x}"},
            }
        elif _detected == "ina226":
            _fields: dict = {"battery_source": "ina226"}
            _sug: dict = {"kind": "battery", "source": "ina226", "fields": _fields}
            if _bus != 1 or _addr != 0x40:
                _sug["note"] = (
                    "set battery.i2c_bus/i2c_addr in vanchor.yaml if not 1/0x40"
                )
            _resp["suggest"] = _sug
        else:
            _resp["suggest"] = None
        return _resp

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
                from .hardware.i2c_link import make_motor_transport
                transport = make_motor_transport(
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
        """Delegated to DeviceManager (issue #70)."""
        return self._devices._construct_devices(cfg)

    def _driver_context(self, kind: str, source: str, config):
        """Build the NARROW, versioned capability object (#43) a pluggable driver
        is constructed with — publish a reading, report health, read its own
        config, a logger/clock, and coarse boat motion. Deliberately carries NO
        reference to the runtime, the motor, or the safety governor, so a driver
        (or a community pack) can never reach STOP/the deadman/the failsafes
        through it (see docs/extension-packs.md — the safety floor is never a pack
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
        elif ctype == "anchor_alarm_set":
            # Drop the ALARM anchor (passive watch circle; NO motor involvement).
            # Explicit lat/lon wins (future map-tap placement); default = the
            # boat's current position. Refused without a position.
            lat, lon = command.get("lat"), command.get("lon")
            if lat is not None and lon is not None:
                point = GeoPoint(float(lat), float(lon))
            else:
                pos = self.state.position
                if pos is None or pos.is_null():
                    logger.warning("anchor_alarm_set ignored: no position fix")
                    return
                point = pos
            radius = float(command.get(
                "radius_m", self.config.safety.anchor_alarm_default_radius_m))
            snap = self.anchor_alarm.set(point, radius, now=self._now_fn())
            logger.info("anchor alarm ARMED: %.5f, %.5f r=%.0fm",
                        point.lat, point.lon, snap["radius_m"])
        elif ctype == "anchor_alarm_clear":
            self.anchor_alarm.clear()
            logger.info("anchor alarm cleared")
        elif ctype == "anchor_alarm_recover":
            self._anchor_alarm_recover()
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
    # Passive anchor alarm recover (adoption #10)
    # ------------------------------------------------------------------ #
    def _anchor_alarm_recover(self) -> dict:
        """One-tap recover (adoption #10): engage the NORMAL anchor_hold at
        the alarm anchor point via the standard command path — device gating,
        governor, every failsafe apply unchanged. Disarms the passive alarm
        only if the hold actually engaged (the governor's drag alarm then
        covers the hold); if the controller refused (e.g. motor not
        connected), the passive watch stays armed."""
        st = self.anchor_alarm.store
        if not st.armed or st.lat is None or st.lon is None:
            logger.warning("anchor_alarm_recover ignored: alarm not armed")
            return {"ok": False, "message": "alarm not armed"}
        self.controller.handle_command({
            "type": "anchor_hold",
            "anchor": {"lat": st.lat, "lon": st.lon},
        })
        engaged = self.state.mode in (
            ControlModeName.ANCHOR_HOLD,
            ControlModeName.ANCHOR_ML,
            ControlModeName.ANCHOR_LEIF,
        )
        if engaged:
            self.anchor_alarm.clear()
            logger.info("anchor alarm recover: anchor_hold engaged at alarm point")
            return {"ok": True}
        logger.warning("anchor alarm recover: anchor_hold refused; alarm stays armed")
        return {"ok": False, "message": "anchor_hold refused (device gating?)"}

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

    def evaluate_anchor_alarm(self) -> dict:
        """1 Hz passive anchor-alarm step (adoption #10). Reads position +
        fix age off the shared state; NEVER issues motor commands or mode
        changes — breach only latches telemetry + fires the breach hooks."""
        st = self.state
        age = (
            (self._mono_fn() - st.fix_received_mono)
            if st.fix_received_mono is not None else None
        )
        return self.anchor_alarm.evaluate(st.position, age)

    def evaluate_push_alerts(self) -> None:
        """Edge-triggered Web Push dispatch (adoption #7). Mirrors the client-side
        banner conditions in static/alerts.js, but server-side so an alarm
        reaches the phone with NO client connected. Observe-only: reads state,
        never commands. Each send is enqueued to the push worker thread.

        Every edge is also recorded into the server-side alert log (Task 1
        D8/A14) so the alert-history panel survives a page reload."""
        prev = self._push_prev

        # Boat position for alert-log entries ("Show on map"); None when no fix.
        _pos = self.state.position
        _lat = _pos.lat if (_pos is not None and not _pos.is_null()) else None
        _lon = _pos.lon if (_pos is not None and not _pos.is_null()) else None

        # ---- anchor drag ----
        drag = bool(self.controller.safety_status.drag_alarm)
        if drag and not prev.get("drag"):
            dist = self.state.distance_to_anchor_m
            body = (f"Boat {dist:.0f} m from anchor" if self.state.anchor is not None
                    else "Boat has dragged from anchor")
            self.push.notify("anchor_drag", "Anchor drag alarm", body)
            self.alert_log.record("alarm", f"Anchor drag alarm — {body}",
                                  kind="drag", lat=_lat, lon=_lon)
        prev["drag"] = drag

        # ---- passive anchor alarm breach (via on_breach hook) ----
        # The on_breach hook (appended in __init__) fires the push immediately
        # on False->True; also track firing here for consistent edge state.
        aalarm = bool(self.anchor_alarm.firing)
        prev["anchor_alarm"] = aalarm  # track but don't double-fire; hook does it

        # ---- battery RTL recommend ----
        battery_rtl = bool(self.state.rtl_recommended)
        if battery_rtl and not prev.get("battery_rtl"):
            self.push.notify("battery", "Battery low", "Return to launch recommended")
            self.alert_log.record("warn", "Battery low — return to launch recommended",
                                  kind="battery", lat=_lat, lon=_lon)
        prev["battery_rtl"] = battery_rtl

        # ---- battery SoC ladder (UI convention: warn <25%, crit <10%) ----
        # Independent of the RTL estimate (Task 1 A5); edge-triggered so each
        # threshold crossing records/pushes exactly once, not once per tick.
        soc_val = self.battery_snapshot().get("soc_pct")
        if soc_val is not None:
            soc_f = float(soc_val)
            batt_crit = soc_f < 10.0
            batt_low = soc_f < 25.0
            if batt_crit and not prev.get("batt_crit"):
                self.push.notify("battery", "Battery critical",
                                 f"Battery at {soc_f:.0f}%")
                self.alert_log.record("alarm", f"Battery critical — {soc_f:.0f}%",
                                      kind="battery", lat=_lat, lon=_lon)
            elif batt_low and not batt_crit and not prev.get("batt_low"):
                self.push.notify("battery", "Battery low",
                                 f"Battery at {soc_f:.0f}%")
                self.alert_log.record("warn", f"Battery low — {soc_f:.0f}%",
                                      kind="battery", lat=_lat, lon=_lon)
            prev["batt_crit"] = batt_crit
            prev["batt_low"] = batt_low

        # ---- link loss failsafe ----
        link = bool(self._link_failsafe_engaged)
        if link and not prev.get("link"):
            action = self._link_failsafe_action
            if action == "stop":
                body = "Motor stopped (link-loss failsafe)"
            elif action == "continue":
                body = "Continuing mission unsupervised"
            else:
                body = "Holding position (failsafe)"
            self.push.notify("link", "Connection lost", body)
            self.alert_log.record("alarm", f"Connection lost — {body}",
                                  kind="link", lat=_lat, lon=_lon)
        prev["link"] = link

        # ---- shallow stop ----
        shallow = bool(self.controller.safety_status.shallow_stop)
        if shallow and not prev.get("shallow"):
            min_d = self.controller.safety.config.min_depth_m
            self.push.notify("depth", "Shallow water",
                             f"Auto-stopped: depth below {min_d:.1f} m")
            self.alert_log.record("alarm",
                                  f"Shallow — auto-stopped (depth below {min_d:.1f} m)",
                                  kind="shallow", lat=_lat, lon=_lon)
        prev["shallow"] = shallow

        # ---- depth divergence ----
        diverge = bool(self.state.depth_divergence_alert)
        if diverge and not prev.get("diverge"):
            self.push.notify("depth", "Depth warning",
                             "Sounder disagrees with chart — possible uncharted shoal")
            self.alert_log.record("warn",
                                  "Depth warning — sounder disagrees with chart",
                                  kind="depth", lat=_lat, lon=_lon)
        prev["diverge"] = diverge

        # ---- GPS fix lost ----
        fix_lost = bool(self.controller.safety_status.fix_lost)
        if fix_lost and not prev.get("fix_lost"):
            self.push.notify("link", "GPS fix lost", "Thrust cut until fix returns")
            self.alert_log.record("alarm", "GPS fix lost — thrust cut until fix returns",
                                  kind="gps", lat=_lat, lon=_lon)
        prev["fix_lost"] = fix_lost

        # ---- battery ladder step (cap decrease) ----
        cap = self.controller.safety.thrust_cap
        if cap < self._push_prev_cap - 1e-9:
            soc = self.battery_snapshot().get("soc_pct")
            self.push.notify(
                "battery", "Battery low",
                f"Thrust limited to {cap:.0%}"
                + (f" at {soc:.0f}% charge" if soc is not None else ""),
            )
        self._push_prev_cap = cap

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
            # No usable range estimate (boat not making way). A zero estimate
            # with a critically low pack must still recommend -- "unknown" is
            # not "infinite". (Task 1 A5: rtl_recommended stayed False at
            # range_m: 0 even with the pack nearly flat.)
            soc = self.battery_snapshot().get("soc_pct")
            if soc is not None and float(soc) <= 10.0:
                self.state.rtl_recommended = True
                return True
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
        """Poll the host-side supervisor's /v1/status and cache it in _supervisor_status.

        Exception-proof: logged errors back off and retry; task only exits on
        CancelledError at shutdown. Never mutates any safety-critical state."""
        if self.supervisor_link is None:
            return
        cfg = self.config.supervisor
        while True:
            try:
                status = await asyncio.to_thread(self.supervisor_link.status)
                self._supervisor_status = status
                self._supervisor_status_at = self._mono_fn()
                if status is None:
                    await asyncio.sleep(cfg.unavailable_backoff_s)
                elif status.get("job") is not None:
                    # A job is running — poll fast so the UI stays responsive.
                    await asyncio.sleep(1.0)
                else:
                    await asyncio.sleep(cfg.poll_interval_s)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("supervisor client poll error -- will retry")
                await asyncio.sleep(cfg.poll_interval_s)

    def _supervisor_snapshot(self) -> dict:
        """Supervisor link state for the UI (update/backup/disk cards + banners).

        Always returns a dict (never None) so the UI can safely read keys.
        The first container's tag/previous_tag are promoted to the top level
        so the UI can render the installed version without iterating."""
        from . import __version__
        if self.supervisor_link is None:
            return {"available": False, "app_version": __version__}
        st = self._supervisor_status
        if not st:
            return {"available": False, "app_version": __version__}
        containers = st.get("containers", [])
        first = containers[0] if containers else {}
        return {
            "available": True,
            "app_version": __version__,
            "supervisor_version": st.get("supervisor_version"),
            "api_version": st.get("api_version"),
            "tag": first.get("tag"),
            "previous_tag": first.get("previous_tag"),
            "containers": containers,
            "disk": st.get("disk"),
            "job": st.get("job"),
            "last_job": st.get("last_job"),
            "warnings": st.get("warnings", []),
            "backups": st.get("backups", {}),
        }

    async def _run_supervisor(self, period_s: float = 1.0) -> None:
        """~1 Hz task driving the periodic safety evaluations + depth persistence.

        Exception-proof: the whole body is guarded so a raise (from a step or a
        save) only logs and continues -- the task NEVER exits on its own; it ends
        only on cancellation at shutdown."""
        while True:
            try:
                await asyncio.sleep(period_s)
                self._supervise_once()
                await self._depth._maybe_persist_depth()
            except asyncio.CancelledError:
                raise  # shutdown -> let the cancellation propagate
            except Exception:  # noqa: BLE001 - supervisor must never die
                logger.exception("supervisor loop error -- will continue")

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
        """Shim → DepthService (issue #71)."""
        return self._depth.import_depth_map(filename, data, replace)

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
        # Passive anchor alarm (adoption #10): armed circle + live breach state.
        payload["anchor_alarm"] = self.anchor_alarm.snapshot()
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
        payload["demo_mode"] = self.config.demo.enabled
        payload["demo_readonly"] = self.config.demo.enabled and self.config.demo.readonly
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
        payload["supervisor"] = self._supervisor_snapshot()
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Vanchor-NG server")
    parser.add_argument("--config", default=None, help="YAML/JSON config file")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--model", default=None, choices=["simple", "fossen"])
    parser.add_argument("--hardware", action="store_true", help="use real serial devices")
    parser.add_argument("--nmea-tcp", action="store_true", help="accept NMEA over TCP")
    parser.add_argument("--demo", action="store_true",
                        help="demo mode: forced sim on the charted lake, seeded "
                             "moving scenario, ephemeral data dir, DEMO badge")
    parser.add_argument("--demo-readonly", action="store_true",
                        help="demo mode + every client pinned to observer (hosted demo)")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args(argv)

    config = load(args.config)
    demo = args.demo or args.demo_readonly
    if demo:
        apply_demo_mode(config, readonly=args.demo_readonly)
    else:
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
        if demo:
            logger.warning("--hardware ignored in demo mode (forced sim)")
        else:
            config.hardware.enabled = True
    if args.nmea_tcp:
        if demo:
            logger.warning("--nmea-tcp ignored in demo mode (forced sim)")
        else:
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
