"""Hardware discovery/probe cluster extracted from Runtime (issue #79).

The 10 methods that handle hardware enumeration and probing live here.
``HardwareScan`` holds a back-reference to ``Runtime`` via ``self._rt`` for
shared state that remains on Runtime (config, state, bus, gps, compass,
devices/collaborators, etc.).

Private state owned by this cluster:
  ``_hw_probe_lock`` -- only used by ``hw_probe`` / ``_hw_probe_i2c``; moved
  here.  A property shim on Runtime forwards ``rt._hw_probe_lock`` so the test
  suite that reads the lock directly still works.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("vanchor.app")


class HardwareScan:
    """Hardware discovery + probe cluster -- split out of Runtime."""

    def __init__(self, rt) -> None:
        self._rt = rt  # back-reference to Runtime for shared state
        # Lock for the hardware probe wizard: only one port may be probed at a
        # time (opens a real serial port / I2C bus briefly). If another probe
        # is already running the endpoint returns 409 immediately.
        self._hw_probe_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Source discovery helpers
    # ------------------------------------------------------------------ #

    def _compass_sources(self) -> tuple:
        """Built-in compass sources + any registered driver sources (e.g.
        ``hwt901b``). Registered drivers are discovered from the plugin registry,
        so a new compass driver adds itself here without editing this file."""
        from ..hardware import registry
        rt = self._rt
        return rt._SENSOR_SOURCES + tuple(registry.sources("compass"))

    def _gps_sources(self) -> tuple:
        """Built-in GPS sources + any registered GPS driver sources (e.g.
        ``ublox`` = the UBX M9N driver)."""
        from ..hardware import registry
        rt = self._rt
        return rt._SENSOR_SOURCES + tuple(registry.sources("gps"))

    # ------------------------------------------------------------------ #
    # Port / I2C ownership helpers
    # ------------------------------------------------------------------ #

    def _ports_in_use(self) -> dict[str, str]:
        """realpath(serial port) -> owning device kind, for every ACTIVE device
        whose driver holds a serial port open.

        Conservative: if in doubt, mark in use. I2C port strings (``i2c:...``)
        are handled by :meth:`_i2c_addrs_in_use`; serial strings only here.
        """
        import os

        rt = self._rt
        hw = rt.config.hardware
        result: dict[str, str] = {}

        def _add(port: str, kind: str) -> None:
            if not port or port.startswith("i2c:"):
                return
            try:
                result[os.path.realpath(port)] = kind
            except OSError:
                result[port] = kind

        # GPS
        if rt.gps is not None and hw.source("gps") in ("serial", "ublox"):
            _add(hw.gps_port, "gps")

        # Compass
        if rt.compass is not None and hw.source("compass") in ("serial", "hwt901b"):
            _add(hw.compass_port, "compass")

        # Motor — derive from the link plan (conservative: mark if source touches serial)
        try:
            from ..hardware.link_plan import plan_motor_links
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
        rt = self._rt
        hw = rt.config.hardware
        result: set[tuple[int, int]] = set()
        ports_to_check: list[str] = []

        try:
            from ..hardware.link_plan import plan_motor_links
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

    # ------------------------------------------------------------------ #
    # Hardware setup wizard: scan
    # ------------------------------------------------------------------ #

    def hw_scan(self) -> dict:
        """Enumerate candidate hardware endpoints WITHOUT opening any of them.

        Returns serial ports (list_serial_ports with ownership annotation),
        I2C buses, the statically known I2C device list, and capability flags.
        In demo mode returns a graceful empty posture (no hardware scanning).
        """
        import glob
        import importlib.util
        import os
        from ..hardware.probe import hint_from_metadata

        rt = self._rt
        caps = {
            "serial": importlib.util.find_spec("serial_asyncio") is not None,
            "i2c": importlib.util.find_spec("smbus2") is not None,
        }

        # Known I2C devices (static)
        _ina = getattr(rt.config, "battery", None)
        _ina_addr = getattr(_ina, "i2c_addr", 0x40) if _ina else 0x40
        known_i2c = [
            {"kind": "helm-pico", "addr": "0x42",
             "label": "Vanchor helm PCB (motor tunnel)"},
            {"kind": "ina226", "addr": f"0x{_ina_addr:02x}",
             "label": "INA226 battery shunt"},
        ]
        # Magnetometer compass chips (BN-880/BE-880 & standalone). Addresses come
        # from the driver's chip table so the wizard offers exactly what it can
        # autodetect.
        from ..nav import magnetometer as _mag
        for _spec in _mag.CHIP_TABLE:
            for _a in _spec.addresses:
                known_i2c.append({"kind": "magnetometer", "addr": f"0x{_a:02x}",
                                  "label": f"{_spec.label} compass"})

        # Demo mode: return sim posture without scanning real hardware
        if getattr(rt.config, "demo", None) and rt.config.demo.enabled:
            return {"ports": [], "i2c_buses": [], "known_i2c": known_i2c,
                    "capabilities": caps}

        in_use = self._ports_in_use()
        i2c_owned = self._i2c_addrs_in_use()

        # Serial ports: list_serial_ports() + ownership + metadata hint
        ports = []
        for _entry in rt.list_serial_ports():
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

    # ------------------------------------------------------------------ #
    # Hardware setup wizard: probe
    # ------------------------------------------------------------------ #

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
        from ..hardware import probe as probe_mod

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
        """Serial probe — called from hw_probe under the lock."""
        import os

        rt = self._rt
        port = payload.get("port")
        if not port or not isinstance(port, str):
            raise ValueError("'port' must be a non-empty string")

        # Validate and clamp parameters
        bauds_raw = payload.get("bauds", probe_mod.BAUD_LADDER["any"])
        if not isinstance(bauds_raw, list):
            raise ValueError("'bauds' must be a list")
        bauds: list[int] = []
        for _b in bauds_raw[:6]:
            try:
                _bi = int(_b)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid baud rate {_b!r}") from exc
            if not (300 <= _bi <= 1_000_000):
                raise ValueError(f"baud rate {_bi} out of range 300..1000000")
            bauds.append(_bi)
        if not bauds:
            bauds = probe_mod.BAUD_LADDER["any"]

        duration_s = max(0.5, min(5.0, float(payload.get("duration_s", 2.0))))
        active_ubx = bool(payload.get("active_ubx_ident", False))
        bytesize = int(payload.get("bytesize", 8))
        parity = str(payload.get("parity", "N"))
        stopbits = float(payload.get("stopbits", 1.0))

        # Ownership check
        try:
            _rp = os.path.realpath(port)
        except OSError:
            _rp = port
        _owned = self._ports_in_use()
        if _rp in _owned:
            return {
                "ok": False, "conflict": True,
                "error": (
                    f"port is in use by the running {_owned[_rp]} driver"
                    " — pick another or change Devices config first"
                ),
            }

        from ..hardware.serial_link import PySerialTransport
        from ..hardware.probe import suggest_for

        best: dict | None = None
        best_rank = -1  # 2=high, 1=medium, 0=none

        def _rank(conf: str) -> int:
            return {"high": 2, "medium": 1, "none": 0}.get(conf, 0)

        for _baud in bauds:
            transport = PySerialTransport(
                port, baudrate=_baud,
                bytesize=bytesize, parity=parity, stopbits=stopbits,
            )
            try:
                await transport.open()
            except (RuntimeError, Exception) as exc:  # noqa: BLE001
                _msg = str(exc)
                if "Permission" in _msg or "permission" in _msg:
                    _msg += " — permission denied — is the user in the dialout group?"
                return {"ok": False, "error": _msg}

            _ident: dict | None = None
            try:
                result = await probe_mod.probe_serial(transport, duration_s)
                if (active_ubx
                        and result.detected in ("ublox", "nmea-gps")
                        and result.confidence != "none"):
                    _ident = await probe_mod.ubx_mon_ver(transport)
                if (result.detected == "vanchor-motor"
                        and result.confidence != "none"):
                    try:
                        _ident = await probe_mod.motor_info_probe(transport)
                    except Exception:  # noqa: BLE001
                        pass  # fall back — A/E fingerprint already in result
            finally:
                try:
                    await transport.close()
                except Exception:  # noqa: BLE001
                    pass

            _r = _rank(result.confidence)
            if best is None or _r > best_rank:
                best_rank = _r
                _suggest = suggest_for(result.detected, port, _baud)
                _resp: dict = {
                    "ok": True, "target": "serial",
                    "port": port, "baud": _baud,
                    "detected": result.detected, "confidence": result.confidence,
                    "sample": result.sample, "raw_preview": result.raw_preview,
                    "counts": result.counts, "suggest": _suggest,
                }
                if _ident is not None:
                    _resp["ident"] = _ident
                best = _resp

            if result.confidence == "high":
                break

        return best or {"ok": False, "error": "no data received on any baud rate"}

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

        if _kind not in ("auto", "helm-pico", "ina226", "magnetometer"):
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
        elif _detected == "magnetometer":
            _chip = _raw.get("sample", {}).get("chip", "magnetometer")
            _resp["suggest"] = {
                "kind": "compass", "source": "magnetometer",
                "fields": {"compass_source": "magnetometer",
                           "compass_port": f"i2c:{_bus}:0x{_addr:02x}"},
                "note": f"{_chip} compass on I2C. Calibrate it in "
                        "Settings -> Devices after setup.",
            }
        else:
            _resp["suggest"] = None
        return _resp

    # ------------------------------------------------------------------ #
    # Driver context + menus
    # ------------------------------------------------------------------ #

    def _driver_context(self, kind: str, source: str, config):
        """Build the NARROW, versioned capability object (#43) a pluggable driver
        is constructed with — publish a reading, report health, read its own
        config, a logger/clock, and coarse boat motion. Deliberately carries NO
        reference to the runtime, the motor, or the safety governor, so a driver
        (or a community pack) can never reach STOP/the deadman/the failsafes
        through it (see docs/extensibility.md — the safety floor is never a pack
        concern)."""
        from ..hardware import registry
        rt = self._rt

        def motion():
            st = getattr(rt, "state", None)
            if st is None or st.fix is None:
                return None
            return (st.fix.cog_deg, st.sog_knots * 0.514444)  # knots -> m/s

        return registry.DriverContext(
            kind=kind, source=source, config=config,
            _bus=rt.bus, _now=rt._now_fn, _motion=motion,
        )

    def _driver_menus(self) -> dict:
        """Per-source device-menu SCHEMAS from registered drivers, with any saved
        settings overlaid -- so the UI can render a device's menu the moment its
        source is selected, before any instance exists. Keyed by source name."""
        from ..hardware import registry
        from ..runtime.builders import _overlay_menu_values
        rt = self._rt
        out: dict = {}
        saved_all = rt.config.hardware.device_settings or {}
        for kind in ("compass",):  # device kinds with pluggable driver menus
            for src, schema in registry.menus(kind).items():
                out[src] = _overlay_menu_values(schema, saved_all.get(kind, {}))
        return out

    # ------------------------------------------------------------------ #
    # Split-channel builder
    # ------------------------------------------------------------------ #

    def _build_split_channel(
        self,
        name: str,
        link: dict | None,
        sim_motor,
        sim_state,
        cfg,
    ):
        """Build one split motor channel; returns ``None`` on failure (Constraint 4).

        ``name`` is "thrust" or "steering"; ``link`` is the resolved channel
        link dict from :func:`~vanchor.hardware.link_plan.plan_motor_links`
        (``None`` or ``source=="none"`` -> not connected).  A build exception is
        caught, logged, and surfaced as ``None`` so the other channel can still
        start up.
        """
        from ..runtime.channels import (
            _SimThrustChannel,
            _SimSteeringChannel,
        )
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
                from ..hardware.serial_channels import (
                    SerialSteeringChannel,
                    SerialThrustChannel,
                )
                from ..hardware.i2c_link import make_motor_transport
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
