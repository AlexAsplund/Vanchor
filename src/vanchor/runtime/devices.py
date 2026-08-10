"""Device management cluster extracted from Runtime (issue #70).

All 13 methods that handle device config, construction, status, health, and
debug live here. ``DeviceManager`` holds a back-reference to ``Runtime`` via
``self._rt`` for shared state that remains on Runtime (config, state, bus,
gps/compass/depth_sounder/controller/simulator/prefs, etc.).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("vanchor.app")

# Built-in (non-driver) sources -> how they connect: "serial" needs a port+baud,
# "i2c" needs a bus/address, "none" needs no connection field. Registered drivers
# carry their own transport (registry.transports()).
_BUILTIN_TRANSPORT = {"sim": "none", "serial": "serial", "nmea": "none",
                      "none": "none", "both": "serial"}
# option-key -> registry device kind (only where they differ; depth uses "sensor").
_OPTION_REG_KIND = {"sensor": "depth"}


def _same_serial_port(a: str | None, b: str | None) -> bool:
    """True if two configured serial ports point at the same device -- exact
    match, or the same target after resolving symlinks (e.g. /dev/ttyACM0 vs
    /dev/serial/by-id/...)."""
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        import os
        return os.path.realpath(a) == os.path.realpath(b)
    except Exception:  # noqa: BLE001 - realpath on a nonexistent path shouldn't crash
        return False


def _source_transports(options: dict, reg_transports: dict) -> dict:
    """``{option_key: {source: transport}}`` merging the built-in sources with the
    registry's per-driver transport, so the UI can show serial / I2C / no
    connection fields to match the selected source (not always serial)."""
    out: dict = {}
    for opt_key, sources in options.items():
        reg = reg_transports.get(_OPTION_REG_KIND.get(opt_key, opt_key), {})
        out[opt_key] = {s: reg.get(s, _BUILTIN_TRANSPORT.get(s, "serial"))
                        for s in sources}
    return out


class DeviceManager:
    """Device config, construction, status, health, debug -- split out of Runtime."""

    def __init__(self, rt) -> None:
        self._rt = rt   # back-reference to Runtime for shared state

    # ------------------------------------------------------------------ #
    # Device / hardware config (persisted, editable over the API)
    # ------------------------------------------------------------------ #
    def device_config(self) -> dict:
        """Current device/hardware config + the selectable options.

        Shape matches what :meth:`set_device_config` persists, plus ``options``
        (for the UI's selects) and ``restart_required`` (always ``False`` on a
        plain read; a POST returns ``True`` because devices are rebuilt only on
        restart, not hot-swapped)."""
        from dataclasses import asdict

        from ..hardware import registry
        rt = self._rt
        options = {
            "sensor": list(rt._SENSOR_SOURCES),
            "gps": list(rt._gps_sources()),
            "compass": list(rt._compass_sources()),
            "motor": list(rt._MOTOR_SOURCES),
            "battery": list(rt._battery_sources()),
            # Per-channel split sources ("both" is a combined concept, not a
            # per-channel one; split channels use sim | serial | none).
            "steering": list(rt._CHANNEL_SOURCES),
            "thrust": list(rt._CHANNEL_SOURCES),
        }
        return {
            "hardware": asdict(rt.config.hardware),
            "nmea_tcp": asdict(rt.config.nmea_tcp),
            "sim_motor": asdict(rt.config.sim_motor),  # actuation shaping (#36)
            "options": options,
            # How each source connects, so the UI shows the RIGHT connection
            # fields (serial port + baud / I2C target / none) per selected source.
            "source_transports": _source_transports(options, registry.transports()),
            "menus": self._device_menus(),
            "driver_menus": rt._driver_menus(),
            "restart_required": False,
        }

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
        from dataclasses import asdict
        from ..core.config import (
            HardwareConfig,
            NmeaTcpConfig,
            SimMotorConfig,
            _merge_into,
            save_device_overrides,
        )
        rt = self._rt
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        hw_in = payload.get("hardware") or {}
        nmea_in = payload.get("nmea_tcp") or {}
        motor_in = payload.get("sim_motor") or {}
        if not isinstance(hw_in, dict) or not isinstance(nmea_in, dict) or not isinstance(motor_in, dict):
            raise ValueError("'hardware', 'nmea_tcp' and 'sim_motor' must be objects")

        # Build validated copies off the *current* config (so an edit can be
        # partial). Sources: sensors sim|serial|nmea, motor sim|serial|both.
        hw = HardwareConfig(**asdict(rt.config.hardware))
        for dev in ("gps", "compass", "depth"):
            key = f"{dev}_source"
            if dev == "compass":
                allowed = rt._compass_sources()
            elif dev == "gps":
                allowed = rt._gps_sources()
            else:
                allowed = rt._SENSOR_SOURCES
            if hw_in.get(key) is not None and hw_in[key] not in allowed:
                raise ValueError(
                    f"{key} must be one of {allowed} (got {hw_in[key]!r})"
                )
        if hw_in.get("motor_source") is not None and hw_in["motor_source"] not in rt._MOTOR_SOURCES:
            raise ValueError(
                f"motor_source must be one of {rt._MOTOR_SOURCES} (got {hw_in['motor_source']!r})"
            )
        # Per-channel split sources (steering / thrust).
        for ch in ("steering", "thrust"):
            key = f"{ch}_source"
            if hw_in.get(key) is not None and hw_in[key] not in rt._CHANNEL_SOURCES:
                raise ValueError(
                    f"{key} must be one of {rt._CHANNEL_SOURCES} (got {hw_in[key]!r})"
                )
        batt_allowed = rt._battery_sources()
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

        nmea = NmeaTcpConfig(**asdict(rt.config.nmea_tcp))
        motor = SimMotorConfig(**asdict(rt.config.sim_motor))
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
        from ..hardware.link_plan import plan_motor_links
        try:
            plan_motor_links(hw)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        save_device_overrides(rt.config.data_dir, hw, nmea, motor)
        # Reflect the edit in the live config so a subsequent GET shows it.
        rt.config.hardware = hw
        rt.config.nmea_tcp = nmea
        rt.config.sim_motor = motor
        # Apply the shaping to the LIVE sim motor immediately (no restart): the
        # simulated motor exposes ``configure``; a real/tee motor doesn't, so this
        # is a safe no-op off-sim.
        sim_motor = getattr(rt.simulator, "motor", None)
        if sim_motor is not None and hasattr(sim_motor, "configure"):
            sim_motor.configure(
                reverse_delay_s=motor.reverse_delay_s,
                thrust_slew_per_s=motor.thrust_slew_per_s,
                thrust_lag_tau_s=motor.thrust_lag_tau_s,
            )
        logger.info("device config updated: %s", payload)
        # Hardware/NMEA devices rebuild on the next start; sim_motor applied live.
        return {"ok": True, "restart_required": True}

    def _device_menus(self) -> list:
        """Collect device-specific menus (settings/actions) from the active
        devices that expose ``device_menu()`` -- surfaced to the UI so a driver
        can offer its own controls (e.g. the HWT901B compass)."""
        rt = self._rt
        out: list = []
        for dev in (rt.gps, rt.compass, rt.depth_sounder):
            fn = getattr(dev, "device_menu", None)
            if callable(fn):
                try:
                    out.append(fn())
                except Exception as exc:  # noqa: BLE001 - a bad menu can't break config
                    logger.warning("device_menu failed: %s", exc)
        return out

    def _device_by_kind(self, kind: str):
        rt = self._rt
        return {"gps": rt.gps, "compass": rt.compass,
                "depth": rt.depth_sounder}.get(kind)

    def apply_device_setting(self, kind: str, key: str, value) -> dict:
        """Persist a device-menu setting for ``kind`` and apply it live if the
        device is running. Persisted settings are read when the device is
        (re)built, so a choice sticks even when the device isn't active yet."""
        from ..hardware import registry
        from ..core.config import save_device_overrides
        rt = self._rt
        fn = getattr(self._device_by_kind(kind), "apply_setting", None)
        known = any(
            key in {s.get("key") for s in menu.get("settings", [])}
            for menu in registry.menus(kind).values()
        )
        if not known and not callable(fn):
            return {"ok": False, "message": f"no settings for device {kind!r}"}
        ds = rt.config.hardware.device_settings
        ds.setdefault(kind, {})[key] = value
        try:
            save_device_overrides(rt.config.data_dir, rt.config.hardware,
                                  rt.config.nmea_tcp, rt.config.sim_motor)
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

    def _construct_devices(self, cfg) -> dict:
        """Build the device set (simulator + sensors + motor) for ``cfg.hardware``.
        Returns a dict and does NOT mutate ``self`` -- so a live reload can build
        the new set first and swap it in only on success (see reload_devices)."""
        from ..hardware.link_plan import plan_motor_links
        from ..hardware.interfaces import NullMotor
        from ..sim.devices import SimCompass, SimDepthSounder, SimGps
        from ..sim.simulator import Simulator
        from ..sim.bathymetry import Bathymetry
        from ..core.models import BoatState, GeoPoint
        from ..hardware import registry
        from ..runtime.builders import _build_boat_params, _build_battery_config
        from ..runtime.channels import (
            _NeutralChannelMotor,
            _TeeMotor,
            _SimChannelState,
            _SimThrustChannel,
            _SimSteeringChannel,
        )
        rt = self._rt
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
            prev = getattr(rt, "simulator", None)
            start_state = (
                prev.truth() if prev is not None
                else BoatState(point=GeoPoint(cfg.sim.start_lat, cfg.sim.start_lon), heading_deg=0.0)
            )
            simulator = Simulator(
                start=start_state,
                params=_build_boat_params(cfg),
                environment=rt._environment,
                physics_hz=cfg.sim.physics_hz,
                time_scale=cfg.sim.time_scale,
                model=cfg.sim.model,
                battery_config=_build_battery_config(cfg),
                # Actuation shaping (#36): default-zero => transparent passthrough.
                motor_reverse_delay_s=cfg.sim_motor.reverse_delay_s,
                motor_thrust_slew_per_s=cfg.sim_motor.thrust_slew_per_s,
                motor_thrust_lag_tau_s=cfg.sim_motor.thrust_lag_tau_s,
            )
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
                motor = rt._build_serial_motor(cfg)
            elif plan.source == "both":
                motor = _TeeMotor([sim_motor, rt._build_serial_motor(cfg)])
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
            from ..hardware.split_motor import SplitMotor
            sim_state = _SimChannelState() if sim_motor is not None else None
            thrust_ch = rt._build_split_channel(
                "thrust", plan.thrust, sim_motor, sim_state, cfg)
            steering_ch = rt._build_split_channel(
                "steering", plan.steering, sim_motor, sim_state, cfg)
            motor = SplitMotor(thrust=thrust_ch, steering=steering_ch)
        # "nmea" (or anything not sim/serial) builds NO internal sensor: the
        # navigator is fed by external NMEA over the bridge/inject instead.
        gps = compass = depth = None
        if src["gps"] == "serial":
            gps = rt._build_serial_gps(cfg)
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
            gps = SimGps(simulator.truth, rt.bus, update_hz=cfg.sensors.gps_hz * _ts,
                         position_noise_m=cfg.sensors.gps_noise_m,
                         emit_velocity=cfg.sensors.gps_velocity, **_jitter)
        elif registry.has("gps", src["gps"]):
            # A pluggable GPS driver (e.g. the UBX "ublox" M9N). Build eagerly but
            # resiliently -- a failure must not crash startup (mirrors compass).
            try:
                gps = registry.build_device("gps", src["gps"], rt, cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "gps source %r could not be built (%s); running without GPS. "
                    "Change it in Settings -> Devices.", src["gps"], exc)
                gps = None
        if src["compass"] == "serial":
            compass = rt._build_serial_compass(cfg)
        elif src["compass"] == "sim":
            # Deterministic sea-state model (#38) drives the sim IMU; Hs<=0 (the
            # default) leaves the flat-water IMU bit-for-bit unchanged.
            from ..sim.sea_state import SeaState
            compass = SimCompass(simulator.truth, rt.bus, update_hz=cfg.sensors.compass_hz * max(0.01, cfg.sim.time_scale),
                                 heading_noise_deg=cfg.sensors.compass_noise_deg,
                                 sea_state=SeaState.from_config(cfg.sea_state))
        elif registry.has("compass", src["compass"]):
            # A pluggable driver builds eagerly (may open a port / import an
            # optional lib), so a failure here must NOT crash startup -- skip it,
            # log why, and leave the UI reachable to fix the config (mirrors the
            # serial "unopenable device" resilience). The warning shows in
            # Settings -> View logs.
            try:
                compass = registry.build_device("compass", src["compass"], rt, cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "compass source %r could not be built (%s); running without a "
                    "compass. Change it in Settings -> Devices.", src["compass"], exc)
                compass = None
        # Share ONE reader when the compass is a serial NMEA source on the SAME
        # port as the serial GPS (a combo source emitting RMC/GGA + HDG). Two
        # readers on one port double-read the stream and publish every line to
        # events.NMEA_IN twice; both sensors are dumb pipes to that bus, so one
        # reader feeds the navigator (GPS *and* compass sentences) for both slots.
        if (src["gps"] == "serial" and src["compass"] == "serial"
                and gps is not None and compass is not None
                and _same_serial_port(cfg.hardware.gps_port, cfg.hardware.compass_port)):
            compass = gps
            logger.info("compass shares the GPS serial reader (same port %s)",
                        cfg.hardware.gps_port)
        if src["depth"] == "sim":
            depth = SimDepthSounder(
                simulator.truth,
                Bathymetry(origin=GeoPoint(cfg.sim.start_lat, cfg.sim.start_lon)),
                rt.bus, update_hz=cfg.sensors.depth_hz * max(0.01, cfg.sim.time_scale))
        battery_monitor = rt._build_battery_monitor(cfg, simulator)
        return {"simulator": simulator, "gps": gps, "compass": compass,
                "depth_sounder": depth, "motor": motor,
                "battery_monitor": battery_monitor}

    async def reload_devices(self) -> dict:
        """Rebuild the device set LIVE (no process restart) so a device-config
        change applies immediately. Builds + starts the NEW set first, and only
        stops the old + swaps in if that succeeds -- so a bad serial port leaves
        the current devices running and the autopilot uninterrupted. Returns
        ``{applied: bool, error?: str}``."""
        import asyncio
        import contextlib
        from ..runtime.channels import _start_motor, _stop_motor
        rt = self._rt
        try:
            new = self._construct_devices(rt.config)
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
        # New set is live -- now retire the old one and swap references.
        for d in (rt.gps, rt.compass, rt.depth_sounder,
                  getattr(rt, "battery_monitor", None)):
            if d is not None:
                with contextlib.suppress(Exception):
                    await d.stop()
        if rt.simulator is not None:
            rt.simulator.stop()
        if rt._sim_task is not None:
            rt._sim_task.cancel()
        rt.gps, rt.compass, rt.depth_sounder = new["gps"], new["compass"], new["depth_sounder"]
        rt.battery_monitor = new["battery_monitor"]
        rt.simulator, rt._sim_task = new["simulator"], new_sim_task
        # Swap the motor in, then stop the OLD one (closes its port + kills the
        # feedback task -> no port/task leak). Best-effort so a stubborn old
        # motor can't strand the reload.
        old_motor = rt.controller.motor
        rt.controller.motor = new["motor"]
        rt.controller.device_connected = self._device_connected_map(rt.config)
        await _stop_motor(old_motor)
        # Re-prime the navigator with a fresh fix/heading so the fix-lost failsafe
        # doesn't latch (and stop the motor) over the brief gap during the swap.
        for dev in (rt.gps, rt.compass):
            if dev is not None:
                with contextlib.suppress(Exception):
                    rt.navigator.handle_sentence(dev.sample())
        logger.info("device config applied live")
        return {"applied": True}

    def _device_health(self, now: float | None = None) -> dict:
        """``{gps: {healthy, data_age_s}, compass: ..., depth: ..., motor: ...}``
        for any device exposing ``healthy`` / ``last_data_monotonic``.

        Null-safe: a device without a ``healthy`` attribute (sim devices) is
        omitted; a present-but-never-received ``last_data_monotonic`` yields a
        ``data_age_s`` of ``None``."""
        rt = self._rt
        if now is None:
            now = rt._mono_fn()
        out: dict = {}
        for name, dev in (
            ("gps", rt.gps),
            ("compass", rt.compass),
            ("depth", rt.depth_sounder),
            ("motor", rt.controller.motor),
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
        wd = getattr(rt, "watchdog", None)
        if wd is not None and getattr(wd, "enabled", False):
            out["watchdog"] = {"healthy": bool(getattr(wd, "_started", False)),
                               "data_age_s": None}
        return out

    def _device_connected_map(self, cfg) -> dict:
        """``{kind: bool}`` -- a device is connected unless its source is "none".

        For a **split** motor plan the map additionally carries ``"thrust"`` and
        ``"steering"`` per-channel booleans: *connected* means the channel is
        both configured (source != "none") **and** was actually built (channel
        object is not ``None``).  A build failure (e.g. serial channels arriving
        in Task 3) therefore shows up here so mode-gating keeps the pilot safe.

        When the plan is **combined** those keys are absent; the fail-open
        default in :func:`~vanchor.core.capabilities.missing_devices` treats
        them as connected, preserving exact back-compat with all legacy tests.
        """
        from ..hardware.link_plan import plan_motor_links
        from ..hardware.split_motor import SplitMotor
        rt = self._rt
        hw = cfg.hardware
        conn = {k: hw.source(k) != "none" for k in ("gps", "compass", "depth", "motor")}
        bsrc = hw.battery_source or ("sim" if rt.simulator is not None else "none")
        conn["battery"] = bsrc != "none"

        # Split plan: add per-channel connectivity (fail-open for combined so
        # every existing test that omits "thrust"/"steering" stays green).
        try:
            plan = plan_motor_links(hw)
        except ValueError:
            plan = None  # malformed config; leave channel keys absent (fail-open)

        if plan is not None and plan.kind == "split":
            motor = getattr(
                getattr(rt, "controller", None), "motor", None)
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
        from ..hardware.link_plan import plan_motor_links
        from ..hardware.split_motor import SplitMotor
        rt = self._rt
        hw = rt.config.hardware
        connected = self._device_connected_map(rt.config)

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
        for kind, dev in (("gps", rt.gps), ("compass", rt.compass),
                          ("depth", rt.depth_sounder), ("motor", rt.controller.motor),
                          ("battery", getattr(rt, "battery_monitor", None))):
            src = hw.battery_source if kind == "battery" else hw.source(kind)
            if kind == "battery" and not src:
                src = "sim" if rt.simulator is not None else "none"
            out[kind] = {"source": src, "connected": connected.get(kind, True),
                         "healthy": _healthy(dev)}

        # Split plan: add per-channel status entries + update motor roll-up.
        try:
            plan = plan_motor_links(hw)
        except ValueError:
            plan = None

        if plan is not None and plan.kind == "split":
            motor = rt.controller.motor
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

    def device_debug(self, kind: str) -> dict:
        """Human-readable raw-data snapshot for one device (Devices -> Debug).
        Returns ``{ok, kind, source, debug}``; ``ok:false`` if no such device.

        Also accepts the per-channel kinds ``"steering"`` and ``"thrust"`` when
        the motor plan is split; ``"motor"`` always returns the composite
        :class:`~vanchor.hardware.split_motor.SplitMotor` debug (which includes
        both channels) even in a split build, so the existing UI debug button
        keeps working.
        """
        rt = self._rt
        hw = rt.config.hardware

        # Handle per-channel split kinds.
        if kind in ("steering", "thrust"):
            from ..hardware.split_motor import SplitMotor
            from ..hardware.link_plan import plan_motor_links
            motor = rt.controller.motor
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

        dev = {"gps": rt.gps, "compass": rt.compass,
               "depth": rt.depth_sounder, "motor": rt.controller.motor,
               "battery": getattr(rt, "battery_monitor", None)}.get(kind)
        if dev is None:
            return {"ok": False, "kind": kind,
                    "debug": f"No {kind} device is active (source is 'none' or unbuilt)."}
        src = (rt.config.hardware.battery_source if kind == "battery"
               else rt.config.hardware.source(kind))
        try:
            text = dev.debug()
        except Exception as exc:  # noqa: BLE001 - debug must never break the UI
            text = f"debug() raised: {type(exc).__name__}: {exc}"
        return {"ok": True, "kind": kind, "source": src, "debug": text}

    def all_device_debug(self) -> dict:
        """``{kind: debug_string}`` for every device -- recorded into a debug
        session so raw device data is preserved (notably the UBX GPS, which
        bypasses the per-sentence ``nmea`` capture)."""
        from ..hardware.link_plan import plan_motor_links
        rt = self._rt
        kinds = ["gps", "compass", "depth", "motor", "battery"]
        try:
            plan = plan_motor_links(rt.config.hardware)
            if plan.kind == "split":
                kinds += ["steering", "thrust"]
        except ValueError:
            pass
        return {kind: self.device_debug(kind).get("debug", "") for kind in kinds}
