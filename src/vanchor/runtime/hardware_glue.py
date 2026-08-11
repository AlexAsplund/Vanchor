"""Connector + serial hardware-wiring cluster extracted from Runtime (issue #73).

The 11 methods that handle serial-port enumeration, hardware probing, serial
device construction (GPS/compass/motor builders), and the connector framework
(consent-gated bus bridges) live here.  ``HardwareGlue`` holds a back-reference
to ``Runtime`` via ``self._rt`` for shared state that remains on Runtime
(config, state, bus, connectors, controller, gps, compass, _connector_grants,
_mono_fn, handle_command, record_command, etc.).

Private state owned by this cluster:
  (none) -- ``_connector_grants`` is initialised in ``Runtime.__init__`` and
  accessed directly by tests, so it stays on Runtime via ``self._rt._connector_grants``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("vanchor.app")


class HardwareGlue:
    """Serial hardware builders + connector framework -- split out of Runtime."""

    # GPS baud capacity constants (used for the link-saturation warning).
    # Assume RMC + GGA per fix; each sentence is ≤ 82 bytes; 10 bits per byte
    # (UART: 8 data + 1 start + 1 stop, no parity at these rates).
    _GPS_BYTES_PER_SENTENCE: int = 82
    _GPS_SENTENCES_PER_FIX: int = 2
    _BAUD_WARN_FRACTION: float = 0.70   # warn when estimated load exceeds 70 %

    def __init__(self, rt) -> None:
        self._rt = rt   # back-reference to Runtime for shared state

    # ------------------------------------------------------------------ #
    # Serial-port enumeration
    # ------------------------------------------------------------------ #

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
                # Label with the ALIAS name first, target in parens: showing only
                # the resolved target made /dev/serial0 render as a second
                # "ttyAMA0" entry -- users looking for "serial0" couldn't find it.
                name = os.path.basename(link)
                label = name if name == target else f"{name} ({target})"
                candidates.append((link, f"{label}{tag} (stable)"))
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

    # ------------------------------------------------------------------ #
    # Hardware setup wizard: serial probe
    # ------------------------------------------------------------------ #

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
        _owned = rt._ports_in_use()
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

    # ------------------------------------------------------------------ #
    # Serial device builders
    # ------------------------------------------------------------------ #

    def _build_serial_gps(self, cfg):
        from ..hardware.serial_devices import SerialGps
        from ..hardware.serial_link import PySerialTransport
        rt = self._rt
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
            parity=hw.gps_parity, stopbits=hw.gps_stopbits), rt.bus)

    def _build_serial_compass(self, cfg):
        from ..hardware.serial_devices import SerialCompass
        from ..hardware.serial_link import PySerialTransport
        rt = self._rt
        hw = cfg.hardware
        return SerialCompass(PySerialTransport(
            hw.compass_port, baudrate=hw.compass_baud, bytesize=hw.compass_bytesize,
            parity=hw.compass_parity, stopbits=hw.compass_stopbits), rt.bus)

    def _build_serial_motor(self, cfg):
        from ..hardware.serial_devices import SerialMotorController
        from ..hardware.i2c_link import make_motor_transport
        hw = cfg.hardware
        return SerialMotorController(make_motor_transport(
            hw.motor_port, baudrate=hw.motor_baud, bytesize=hw.motor_bytesize,
            parity=hw.motor_parity, stopbits=hw.motor_stopbits))

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
        rt = self._rt

        def _sink(cmd: dict) -> None:
            ctype = cmd.get("type")
            try:
                rt.handle_command(cmd)
                rt.record_command(ctype, f"connector:{name}", "accepted")
            except Exception as exc:  # noqa: BLE001 - sink must never propagate
                rt.record_command(ctype, f"connector:{name}", "error", str(exc))
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
        from ..connectors import registry as _creg
        from ..connectors.registry import (
            armed as _armed,
            needs_reconsent as _needs_reconsent,
        )
        from ..runtime.builders import _mask_connector_settings
        rt = self._rt
        result: list[dict] = []
        for name in _creg.names():
            sp = _creg.spec(name)
            if sp is None:  # pragma: no cover - registry invariant
                continue
            grant_settings = rt._connector_grants.get(name, {}).get("settings", {})
            # Inject data_dir so connectors that buffer to disk (e.g. metrics)
            # use the runtime's data dir instead of CWD.  Grant settings win on
            # any explicit key (e.g. a custom data_dir override).
            settings = {"data_dir": rt.config.data_dir, **grant_settings}
            try:
                conn_proto = _creg.build(name, settings)
                mfst = conn_proto.manifest
            except Exception as exc:  # noqa: BLE001 - a bad connector can't break status
                logger.warning("connector %r failed to build for status: %s", name, exc)
                continue
            running_conn = rt.connectors.get(name)
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
                "armed": _armed(name, mfst, rt._connector_grants),
                "needs_reconsent": _needs_reconsent(name, mfst, rt._connector_grants),
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
        from ..connectors import registry as _creg
        from ..connectors.base import manifest_hash as _mhash
        from ..connectors.context import ConnectorContext
        from ..connectors.registry import save_grants as _save_grants

        rt = self._rt
        if not _creg.has(name):
            return {"ok": False, "error": f"unknown connector {name!r}"}

        grant_settings = rt._connector_grants.get(name, {}).get("settings", {})
        # Inject data_dir so connectors that buffer to disk use the runtime's
        # data dir; grant settings override if they carry an explicit key.
        settings = {"data_dir": rt.config.data_dir, **grant_settings}
        try:
            conn = _creg.build(name, settings)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"failed to build connector: {exc}"}

        # Persist the grant with the CURRENT manifest hash (consent).
        rt._connector_grants[name] = {
            "enabled": bool(enabled),
            "manifest_hash": _mhash(conn.manifest),
            "settings": settings,
        }
        _save_grants(rt.config.data_dir, rt._connector_grants)

        if enabled:
            if name not in rt.connectors:
                sink = self._make_connector_sink(name)
                ctx = ConnectorContext(
                    rt.bus, conn.manifest, sink, mono_fn=rt._mono_fn
                )
                try:
                    await conn.start(ctx)
                    rt.connectors[name] = conn
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
            existing = rt.connectors.pop(name, None)
            if existing is not None:
                try:
                    await existing.stop()
                    logger.info("connector %r disarmed and stopped", name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "connector %r failed to stop cleanly: %s", name, exc
                    )

        return {"ok": True, "running": name in rt.connectors}

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
        from ..connectors import registry as _creg
        from ..connectors.base import manifest_hash as _mhash
        from ..connectors.context import ConnectorContext
        from ..connectors.registry import save_grants as _save_grants

        rt = self._rt
        if not _creg.has(name):
            return {"ok": False, "error": f"unknown connector {name!r}"}

        current_grant = rt._connector_grants.get(name, {})
        stored_settings = current_grant.get("settings", {})
        settings_for_proto = {"data_dir": rt.config.data_dir, **stored_settings}
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
        new_settings_full = {"data_dir": rt.config.data_dir, **new_stored}
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
        rt._connector_grants[name] = {
            **current_grant,
            "settings": new_stored,
        }
        _save_grants(rt.config.data_dir, rt._connector_grants)

        # Live-apply when the connector is running.
        running_conn = rt.connectors.get(name)
        if running_conn is not None:
            # Always stop the current instance first.
            rt.connectors.pop(name, None)
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
                        rt.bus, new_conn.manifest, sink, mono_fn=rt._mono_fn
                    )
                    await new_conn.start(ctx)
                    rt.connectors[name] = new_conn
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
            "running": name in rt.connectors,
        }

    def connector_debug(self, name: str) -> dict:
        """Human-readable debug string for connector ``name``.

        Returns ``{ok, name, debug}``; ``ok:False`` if the connector is
        not known or not running (mirroring :meth:`device_debug`)."""
        from ..connectors import registry as _creg

        rt = self._rt
        if not _creg.has(name):
            return {
                "ok": False, "name": name,
                "debug": f"unknown connector {name!r}",
            }
        conn = rt.connectors.get(name)
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

    async def _start_armed_connectors(self) -> None:
        """Build and start every ARMED connector.

        Idempotent: only starts connectors not already in
        :attr:`connectors`. A connector that fails to build or start is
        logged and skipped — NEVER crashes startup."""
        from ..connectors import registry as _creg
        from ..connectors.base import manifest_hash as _mhash
        from ..connectors.context import ConnectorContext
        from ..connectors.registry import armed as _armed

        rt = self._rt
        for name in _creg.names():
            if name in rt.connectors:
                continue  # already running (e.g. after set_connector_armed)
            grant_settings = rt._connector_grants.get(name, {}).get("settings", {})
            # Inject data_dir so connectors that buffer to disk use the
            # runtime's data dir; grant settings override if explicitly set.
            settings = {"data_dir": rt.config.data_dir, **grant_settings}
            try:
                conn = _creg.build(name, settings)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "connector %r failed to build at startup: %s; skipping", name, exc
                )
                continue
            if not _armed(name, conn.manifest, rt._connector_grants):
                continue
            sink = self._make_connector_sink(name)
            ctx = ConnectorContext(
                rt.bus, conn.manifest, sink, mono_fn=rt._mono_fn
            )
            try:
                await conn.start(ctx)
                rt.connectors[name] = conn
                logger.info("connector %r started", name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "connector %r failed to start: %s; skipping", name, exc
                )
