"""Runtime + API integration tests for the connector framework (Task 2).

Covers:
- registered-but-unarmed connector appears in connector_status with armed=False
- set_connector_armed(True) persists grant, live-starts; (False) stops
- API endpoints: GET /api/connectors, POST /api/connectors/{name}/arm,
  GET /api/connectors/{name}/debug
- end-to-end enforcement: PermissionError for non-control commands, STOP always
- legacy migration: cfg.nmea_tcp.enabled + empty connectors.json -> auto-arm
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from vanchor.connectors.base import Connector, ConnectorManifest, manifest_hash
from vanchor.connectors.context import ConnectorContext
from vanchor.connectors import registry as _creg
from vanchor.connectors.registry import (
    armed,
    load_grants,
    save_grants,
    register_connector,
)
from vanchor.core.config import AppConfig
from vanchor.core.events import EventBus
from vanchor.app import Runtime
from vanchor.ui.server import create_app


# --------------------------------------------------------------------------- #
# Shared dummy connector
# --------------------------------------------------------------------------- #


class _TrackingConnector(Connector):
    """A test connector that records ctx so tests can probe enforcement."""

    def __init__(self, *, name: str = "dummy-test", control: bool = False) -> None:
        self._name = name
        self._control = control
        self.ctx: ConnectorContext | None = None
        self.started = False
        self.stopped = False

    @property
    def manifest(self) -> ConnectorManifest:  # type: ignore[override]
        return ConnectorManifest(
            name=self._name,
            label="Dummy Test",
            description="A test connector used by the test suite.",
            consumes=("telemetry",),
            produces=(),
            control=self._control,
            grant_lines=("Read telemetry (test only)",),
        )

    async def start(self, ctx: ConnectorContext) -> None:
        self.ctx = ctx
        self.started = True

    async def stop(self) -> None:
        self.stopped = True
        self.ctx = None

    def debug(self) -> str:
        return f"DummyConnector(control={self._control}): started={self.started}"


# Track instances for per-test introspection.
_LAST_BUILT: dict[str, _TrackingConnector] = {}


def _register_dummy(name: str, *, control: bool = False) -> None:
    """Register a dummy connector under ``name``."""
    _LAST_BUILT.pop(name, None)

    def _build(settings: dict) -> Connector:
        c = _TrackingConnector(name=name, control=control)
        _LAST_BUILT[name] = c
        return c

    register_connector(name, _build, label=f"Dummy ({name})")


def _unregister(name: str) -> None:
    """Remove a connector from the global registry (test cleanup)."""
    _creg._REGISTRY.pop(name, None)  # type: ignore[attr-defined]
    _LAST_BUILT.pop(name, None)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _runtime(tmp_path: Path, *, nmea_tcp_enabled: bool = False) -> Runtime:
    """Minimal Runtime backed by the sim (no real hardware)."""
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.nmea_tcp.enabled = nmea_tcp_enabled
    if nmea_tcp_enabled:
        cfg.nmea_tcp.port = 0  # ephemeral port so tests don't clash
    return Runtime(cfg)


def _arm_grant(name: str, data_dir: Path, settings: dict | None = None) -> None:
    """Write a valid grant for ``name`` into ``<data_dir>/connectors.json``."""
    # Build a fresh instance to get the current manifest hash.
    sp = _creg.spec(name)
    assert sp is not None, f"connector {name!r} not registered"
    conn = sp.build(settings or {})
    grants = load_grants(data_dir)
    grants[name] = {
        "enabled": True,
        "manifest_hash": manifest_hash(conn.manifest),
        "settings": settings or {},
    }
    save_grants(data_dir, grants)


# --------------------------------------------------------------------------- #
# 1. Registered-but-unarmed connector appears in status with armed=False
# --------------------------------------------------------------------------- #


def test_unarmed_connector_appears_in_status(tmp_path: Path) -> None:
    _register_dummy("status-test")
    try:
        rt = _runtime(tmp_path)
        status = rt.connector_status()
        names = [e["name"] for e in status]
        assert "status-test" in names
        entry = next(e for e in status if e["name"] == "status-test")
        assert entry["armed"] is False
        assert entry["running"] is False
        assert entry["control"] is False
        assert "grant_lines" in entry
        assert isinstance(entry["grant_lines"], list)
    finally:
        _unregister("status-test")


# --------------------------------------------------------------------------- #
# 2. set_connector_armed: persists + live-starts / stops
# --------------------------------------------------------------------------- #


async def test_arm_true_persists_and_starts(tmp_path: Path) -> None:
    _register_dummy("arm-test")
    try:
        rt = _runtime(tmp_path)
        result = await rt.set_connector_armed("arm-test", True)
        assert result["ok"] is True
        assert result["running"] is True

        # Grant is persisted to connectors.json.
        grants = load_grants(tmp_path)
        assert "arm-test" in grants
        assert grants["arm-test"]["enabled"] is True

        # Connector is in the running set.
        assert "arm-test" in rt.connectors

        # Status reflects running=True, armed=True.
        status = rt.connector_status()
        entry = next(e for e in status if e["name"] == "arm-test")
        assert entry["running"] is True
        assert entry["armed"] is True
    finally:
        _unregister("arm-test")


async def test_arm_false_stops_running_connector(tmp_path: Path) -> None:
    _register_dummy("arm-stop-test")
    try:
        rt = _runtime(tmp_path)

        # Start it first.
        await rt.set_connector_armed("arm-stop-test", True)
        assert "arm-stop-test" in rt.connectors

        # Capture the running connector instance BEFORE disarming.
        running_conn = rt.connectors["arm-stop-test"]

        # Disarm it.
        result = await rt.set_connector_armed("arm-stop-test", False)
        assert result["ok"] is True
        assert result["running"] is False

        # Grant disabled.
        grants = load_grants(tmp_path)
        assert grants["arm-stop-test"]["enabled"] is False

        # Connector stopped and removed from running set.
        assert "arm-stop-test" not in rt.connectors
        # Verify stop() was called on the running instance.
        assert isinstance(running_conn, _TrackingConnector)
        assert running_conn.stopped is True
    finally:
        _unregister("arm-stop-test")


async def test_arm_unknown_returns_error(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    result = await rt.set_connector_armed("no-such-connector", True)
    assert result["ok"] is False
    assert "error" in result


# --------------------------------------------------------------------------- #
# 3. API endpoints
# --------------------------------------------------------------------------- #


def test_api_get_connectors(tmp_path: Path) -> None:
    _register_dummy("api-list-test")
    try:
        app = create_app(_runtime(tmp_path))
        with TestClient(app) as c:
            r = c.get("/api/connectors")
            assert r.status_code == 200
            body = r.json()
            assert "connectors" in body
            names = [e["name"] for e in body["connectors"]]
            assert "api-list-test" in names
    finally:
        _unregister("api-list-test")


def test_api_arm_round_trip(tmp_path: Path) -> None:
    _register_dummy("api-arm-test")
    try:
        app = create_app(_runtime(tmp_path))
        with TestClient(app) as c:
            # Arm it.
            r = c.post("/api/connectors/api-arm-test/arm", json={"enabled": True})
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True

            # List shows it running.
            r2 = c.get("/api/connectors")
            entries = {e["name"]: e for e in r2.json()["connectors"]}
            assert entries["api-arm-test"]["running"] is True

            # Disarm it.
            r3 = c.post("/api/connectors/api-arm-test/arm", json={"enabled": False})
            assert r3.status_code == 200
            assert r3.json()["ok"] is True
    finally:
        _unregister("api-arm-test")


def test_api_arm_unknown_returns_400(tmp_path: Path) -> None:
    app = create_app(_runtime(tmp_path))
    with TestClient(app) as c:
        r = c.post("/api/connectors/does-not-exist/arm", json={"enabled": True})
        assert r.status_code == 400
        assert r.json()["ok"] is False


def test_api_debug_running_connector(tmp_path: Path) -> None:
    _register_dummy("api-debug-test")
    try:
        app = create_app(_runtime(tmp_path))
        with TestClient(app) as c:
            c.post("/api/connectors/api-debug-test/arm", json={"enabled": True})
            r = c.get("/api/connectors/api-debug-test/debug")
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True
            assert "api-debug-test" in body["name"]
            assert isinstance(body["debug"], str)
    finally:
        _unregister("api-debug-test")


def test_api_debug_not_running_returns_ok_false(tmp_path: Path) -> None:
    _register_dummy("api-debug-notrunning")
    try:
        app = create_app(_runtime(tmp_path))
        with TestClient(app) as c:
            # Do NOT arm; connector is not running.
            r = c.get("/api/connectors/api-debug-notrunning/debug")
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is False
    finally:
        _unregister("api-debug-notrunning")


# --------------------------------------------------------------------------- #
# 4. End-to-end enforcement
# --------------------------------------------------------------------------- #


async def test_enforcement_no_control_set_mode_raises(tmp_path: Path) -> None:
    """A connector WITHOUT the control grant gets PermissionError on non-stop
    commands; handle_command is never invoked."""
    _register_dummy("enforce-test", control=False)
    try:
        rt = _runtime(tmp_path)

        # Spy on handle_command to verify it's never called.
        called: list[dict] = []
        orig = rt.handle_command
        rt.handle_command = lambda cmd: called.append(cmd) or orig(cmd)  # type: ignore[method-assign]

        await rt.set_connector_armed("enforce-test", True)
        conn = _LAST_BUILT.get("enforce-test")
        assert conn is not None and conn.ctx is not None

        with pytest.raises(PermissionError):
            conn.ctx.submit_command({"type": "set_mode", "mode": "anchor_hold"})

        assert called == [], "handle_command must not be called when permission denied"
    finally:
        _unregister("enforce-test")


async def test_enforcement_stop_always_reaches_handle_command(tmp_path: Path) -> None:
    """{"type": "stop"} ALWAYS reaches handle_command — no control grant needed."""
    _register_dummy("enforce-stop-test", control=False)
    try:
        rt = _runtime(tmp_path)

        called: list[dict] = []
        orig = rt.handle_command
        rt.handle_command = lambda cmd: called.append(cmd) or orig(cmd)  # type: ignore[method-assign]

        await rt.set_connector_armed("enforce-stop-test", True)
        conn = _LAST_BUILT.get("enforce-stop-test")
        assert conn is not None and conn.ctx is not None

        # {"type": "stop"} must go through (Constraint 3).
        conn.ctx.submit_command({"type": "stop"})
        assert any(c.get("type") == "stop" for c in called), (
            "handle_command must be called for stop"
        )
    finally:
        _unregister("enforce-stop-test")


async def test_enforcement_with_control_grant_commands_pass(tmp_path: Path) -> None:
    """A connector WITH the control grant can submit non-stop commands."""
    _register_dummy("enforce-ctrl-test", control=True)
    try:
        rt = _runtime(tmp_path)

        called: list[dict] = []
        orig = rt.handle_command
        rt.handle_command = lambda cmd: called.append(cmd) or orig(cmd)  # type: ignore[method-assign]

        await rt.set_connector_armed("enforce-ctrl-test", True)
        conn = _LAST_BUILT.get("enforce-ctrl-test")
        assert conn is not None and conn.ctx is not None

        # This should NOT raise (control=True).
        conn.ctx.submit_command({"type": "stop"})
        assert any(c.get("type") == "stop" for c in called)
    finally:
        _unregister("enforce-ctrl-test")


# --------------------------------------------------------------------------- #
# 5. Legacy migration: cfg.nmea_tcp.enabled -> auto-arm
# --------------------------------------------------------------------------- #


def test_legacy_auto_arm_writes_grant(tmp_path: Path) -> None:
    """With cfg.nmea_tcp.enabled=True and no connectors.json, the grant is
    written automatically at Runtime construction."""
    rt = _runtime(tmp_path, nmea_tcp_enabled=True)
    grants = load_grants(tmp_path)
    assert "nmea-tcp" in grants, "grant must be written"
    assert grants["nmea-tcp"]["enabled"] is True


async def test_legacy_auto_arm_connector_starts(tmp_path: Path) -> None:
    """The auto-armed nmea-tcp connector starts at Runtime.start()."""
    rt = _runtime(tmp_path, nmea_tcp_enabled=True)
    try:
        await rt._start_armed_connectors()
        assert "nmea-tcp" in rt.connectors, "nmea-tcp connector must be running"
        conn = rt.connectors["nmea-tcp"]
        assert conn.status()["running"] is True
    finally:
        for c in list(rt.connectors.values()):
            await c.stop()
        rt.connectors.clear()


async def test_legacy_nmea_tcp_client_visible_behaviour(tmp_path: Path) -> None:
    """The connector-wrapped NmeaTcpServer behaves identically to the old one:
    outbound sentences reach TCP clients; inbound checksummed lines reach the bus."""
    from vanchor.core import events as _ev

    rt = _runtime(tmp_path, nmea_tcp_enabled=True)
    try:
        await rt._start_armed_connectors()
        assert "nmea-tcp" in rt.connectors

        conn = rt.connectors["nmea-tcp"]
        srv = conn._server  # type: ignore[attr-defined]
        assert srv is not None

        # Verify the server is listening.
        port = srv.bound_port
        assert port is not None and port > 0

        # --- outbound: nmea.out -> TCP client ---
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        for _ in range(50):
            if srv.client_count == 1:
                break
            await asyncio.sleep(0.01)
        assert srv.client_count == 1

        await rt.bus.publish("nmea.out", "$GPHDM,123.4,M*hh")
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        assert line == b"$GPHDM,123.4,M*hh\r\n"

        # --- inbound: client -> bus (checksummed only) ---
        received: list[str] = []
        rt.bus.subscribe(_ev.NMEA_IN, lambda s: received.append(s))

        good = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"
        writer.write((good + "\r\n").encode())
        await writer.drain()

        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.01)
        assert received == [good]

        writer.close()
        await writer.wait_closed()
    finally:
        for c in list(rt.connectors.values()):
            await c.stop()
        rt.connectors.clear()


async def test_legacy_auto_arm_only_once(tmp_path: Path) -> None:
    """If connectors.json already has a grant for nmea-tcp, don't overwrite it."""
    # Write an explicit (disabled) grant first.
    from vanchor.connectors.nmea_tcp import MANIFEST

    initial_grants = {
        "nmea-tcp": {
            "enabled": False,
            "manifest_hash": manifest_hash(MANIFEST),
            "settings": {"host": "0.0.0.0", "port": 10110},
        }
    }
    save_grants(tmp_path, initial_grants)

    # Runtime with nmea_tcp.enabled=True should NOT overwrite the existing grant.
    rt = _runtime(tmp_path, nmea_tcp_enabled=True)
    grants = load_grants(tmp_path)
    assert grants["nmea-tcp"]["enabled"] is False, (
        "explicit grant must not be overwritten by legacy auto-arm"
    )


# --------------------------------------------------------------------------- #
# 6. Startup resilience: a failing connector must not crash startup
# --------------------------------------------------------------------------- #


async def test_failing_connector_does_not_crash_startup(tmp_path: Path) -> None:
    """A connector that raises in start() is skipped; runtime continues."""

    class _BrokenConnector(Connector):
        manifest = ConnectorManifest(
            name="broken-test",
            label="Broken",
            description="Always fails to start.",
        )

        async def start(self, ctx: ConnectorContext) -> None:
            raise RuntimeError("intentional start failure")

        async def stop(self) -> None:
            pass

    register_connector("broken-test", lambda _s: _BrokenConnector())
    try:
        rt = _runtime(tmp_path)
        _arm_grant("broken-test", tmp_path)
        rt._connector_grants = load_grants(tmp_path)

        # Should not raise.
        await rt._start_armed_connectors()

        # Broken connector must NOT be in the running set.
        assert "broken-test" not in rt.connectors
    finally:
        _unregister("broken-test")


# --------------------------------------------------------------------------- #
# 7. connector_debug method
# --------------------------------------------------------------------------- #


async def test_connector_debug_running(tmp_path: Path) -> None:
    _register_dummy("dbg-running")
    try:
        rt = _runtime(tmp_path)
        await rt.set_connector_armed("dbg-running", True)
        result = rt.connector_debug("dbg-running")
        assert result["ok"] is True
        assert result["name"] == "dbg-running"
        assert isinstance(result["debug"], str)
    finally:
        _unregister("dbg-running")


async def test_connector_debug_not_running(tmp_path: Path) -> None:
    _register_dummy("dbg-notrunning")
    try:
        rt = _runtime(tmp_path)
        result = rt.connector_debug("dbg-notrunning")
        assert result["ok"] is False
    finally:
        _unregister("dbg-notrunning")


async def test_connector_debug_unknown(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    result = rt.connector_debug("zzz-no-such")
    assert result["ok"] is False


# --------------------------------------------------------------------------- #
# 8. data_dir injection (Fix 1 / Constraint 9)
# --------------------------------------------------------------------------- #


async def test_metrics_connector_buffer_under_data_dir(tmp_path: Path) -> None:
    """Arming the metrics connector builds it with the runtime's data_dir so
    its buffer directory lands under tmp_path, not CWD (Fix 1 / Constraint 9).

    Previously, connectors were built with only the grant's frozen settings and
    no data_dir, causing the metrics buffer to default to CWD (``"."``).
    """
    from vanchor.connectors.metrics import MetricsConnector

    rt = _runtime(tmp_path)
    try:
        result = await rt.set_connector_armed("metrics", True)
        assert result["ok"] is True, f"arm failed: {result}"
        conn = rt.connectors.get("metrics")
        assert isinstance(conn, MetricsConnector), "metrics connector must be running"
        # _buf_dir must be a subdirectory of tmp_path, never CWD.
        assert conn._buf_dir.is_relative_to(tmp_path), (
            f"buffer dir {conn._buf_dir!r} must be under {tmp_path!r}, not CWD"
        )
    finally:
        for c in list(rt.connectors.values()):
            await c.stop()
        rt.connectors.clear()


# --------------------------------------------------------------------------- #
# 9. NMEA-TCP host/port resync on cfg change (Fix 2)
# --------------------------------------------------------------------------- #


def test_nmea_tcp_grant_settings_resynced_on_port_change(tmp_path: Path) -> None:
    """If a nmea-tcp grant exists with a stale port, Runtime.__init__ rewrites
    the settings from cfg while leaving the enabled flag untouched (Fix 2).

    Previously, the grant's settings were frozen at auto-arm time and never
    updated, so editing nmea_tcp.port in devices.json and restarting silently
    used the old port.
    """
    from vanchor.connectors import registry as _creg_inner

    old_port = 10110
    old_host = "127.0.0.1"

    sp = _creg_inner.spec("nmea-tcp")
    assert sp is not None, "nmea-tcp connector must be registered"
    tmp_conn = sp.build({"host": old_host, "port": old_port})
    stale_grants = {
        "nmea-tcp": {
            "enabled": False,  # explicit disable — must survive the resync
            "manifest_hash": manifest_hash(tmp_conn.manifest),
            "settings": {"host": old_host, "port": old_port},
        }
    }
    save_grants(tmp_path, stale_grants)

    # Boot a Runtime with a different port.
    new_port = 10999
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.nmea_tcp.host = old_host
    cfg.nmea_tcp.port = new_port
    Runtime(cfg)  # resync happens in __init__

    updated = load_grants(tmp_path)
    assert updated["nmea-tcp"]["settings"]["port"] == new_port, (
        "port must be resynced from cfg"
    )
    assert updated["nmea-tcp"]["settings"]["host"] == old_host
    assert updated["nmea-tcp"]["enabled"] is False, (
        "enabled flag must survive unchanged after resync"
    )
