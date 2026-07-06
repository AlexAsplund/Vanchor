"""Enforcement tests for the connector core (manifest, context, registry, grants).

These are the safety contract for the connector framework (default-deny allowlist,
control-as-capability, STOP-always-works, manifest-hash re-consent). They talk to the
pure core only — no Runtime, no API wiring (that is Task 2).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from vanchor.connectors.base import Connector, ConnectorManifest, manifest_hash
from vanchor.connectors.context import INGRESS_TOPICS, ConnectorContext
from vanchor.connectors import registry
from vanchor.core import events
from vanchor.core.events import EventBus


def _manifest(**kw) -> ConnectorManifest:
    base = dict(name="demo", label="Demo", description="a demo connector")
    base.update(kw)
    return ConnectorManifest(**base)


def _ctx(manifest: ConnectorManifest, sink=None, bus=None) -> ConnectorContext:
    return ConnectorContext(
        bus=bus if bus is not None else EventBus(),
        manifest=manifest,
        command_sink=sink if sink is not None else (lambda cmd: None),
    )


# --------------------------------------------------------------------------- #
# subscribe (consumes allowlist)
# --------------------------------------------------------------------------- #
def test_subscribe_outside_consumes_raises() -> None:
    ctx = _ctx(_manifest(consumes=("telemetry",)))
    with pytest.raises(PermissionError):
        ctx.subscribe("nmea.out", lambda payload: None)


def test_subscribe_inside_consumes_receives_event() -> None:
    bus = EventBus()
    ctx = _ctx(_manifest(consumes=("telemetry",)), bus=bus)
    seen: list = []
    ctx.subscribe("telemetry", lambda payload: seen.append(payload))
    asyncio.run(bus.publish("telemetry", {"mode": "idle"}))
    assert seen == [{"mode": "idle"}]


# --------------------------------------------------------------------------- #
# publish (produces ∩ ingress allowlist; control topics never)
# --------------------------------------------------------------------------- #
def test_publish_outside_produces_raises() -> None:
    ctx = _ctx(_manifest(produces=()))
    with pytest.raises(PermissionError):
        asyncio.run(ctx.publish("nmea.in", "$GPGGA"))


def test_publish_inside_produces_reaches_bus() -> None:
    bus = EventBus()
    ctx = _ctx(_manifest(produces=("nmea.in",)), bus=bus)
    seen: list = []
    bus.subscribe("nmea.in", lambda payload: seen.append(payload))
    asyncio.run(ctx.publish("nmea.in", "$GPGGA,123"))
    assert seen == ["$GPGGA,123"]


def test_publish_non_ingress_topic_raises_even_if_produced() -> None:
    # "telemetry" is a legit bus topic but NOT an ingress topic — produces alone
    # must not let a connector inject onto it.
    ctx = _ctx(_manifest(produces=("telemetry",)))
    assert "telemetry" not in INGRESS_TOPICS
    with pytest.raises(PermissionError):
        asyncio.run(ctx.publish("telemetry", {"x": 1}))


def test_publish_control_topic_raises_even_when_produced() -> None:
    # Global Constraint 2: a control topic can never be granted via produces.
    bus = EventBus()
    ctx = _ctx(_manifest(produces=(events.MOTOR_COMMAND,)), bus=bus)
    seen: list = []
    bus.subscribe(events.MOTOR_COMMAND, lambda payload: seen.append(payload))
    with pytest.raises(PermissionError):
        asyncio.run(ctx.publish(events.MOTOR_COMMAND, {"type": "manual"}))
    assert seen == []  # never reached the bus


# --------------------------------------------------------------------------- #
# submit_command (control-as-capability; STOP always works)
# --------------------------------------------------------------------------- #
def test_stop_forwarded_without_control_grant() -> None:
    # Global Constraint 3: {"type": "stop"} is ALWAYS accepted.
    calls: list = []
    ctx = _ctx(_manifest(control=False), sink=calls.append)
    ctx.submit_command({"type": "stop"})
    assert calls == [{"type": "stop"}]


def test_non_stop_without_control_raises_and_sink_untouched() -> None:
    calls: list = []
    ctx = _ctx(_manifest(control=False), sink=calls.append)
    with pytest.raises(PermissionError):
        ctx.submit_command({"type": "set_mode", "mode": "anchor"})
    assert calls == []


def test_non_stop_with_control_forwards() -> None:
    calls: list = []
    ctx = _ctx(_manifest(control=True), sink=calls.append)
    ctx.submit_command({"type": "set_mode", "mode": "anchor"})
    assert calls == [{"type": "set_mode", "mode": "anchor"}]


# --------------------------------------------------------------------------- #
# manifest_hash
# --------------------------------------------------------------------------- #
def test_manifest_hash_deterministic() -> None:
    a = _manifest(produces=("nmea.in",))
    b = _manifest(produces=("nmea.in",))
    assert manifest_hash(a) == manifest_hash(b)
    assert len(manifest_hash(a)) == 16


def test_manifest_hash_changes_on_any_field_change() -> None:
    base = _manifest(produces=("nmea.in",))
    h0 = manifest_hash(base)
    assert manifest_hash(_manifest(produces=("nmea.in", "gps.fix_in"))) != h0
    assert manifest_hash(_manifest(produces=("nmea.in",), control=True)) != h0
    assert manifest_hash(_manifest(produces=("nmea.in",), label="Other")) != h0
    assert manifest_hash(_manifest(produces=("nmea.in",), consumes=("telemetry",))) != h0
    assert manifest_hash(_manifest(produces=("nmea.in",), grant_lines=("x",))) != h0


# --------------------------------------------------------------------------- #
# armed / needs_reconsent
# --------------------------------------------------------------------------- #
def test_armed_and_reconsent_states() -> None:
    m = _manifest(produces=("nmea.in",))
    h = manifest_hash(m)
    enabled_match = {"demo": {"enabled": True, "manifest_hash": h, "settings": {}}}
    enabled_stale = {"demo": {"enabled": True, "manifest_hash": "deadbeef", "settings": {}}}
    disabled = {"demo": {"enabled": False, "manifest_hash": h, "settings": {}}}

    assert registry.armed("demo", m, enabled_match) is True
    assert registry.needs_reconsent("demo", m, enabled_match) is False

    assert registry.armed("demo", m, enabled_stale) is False
    assert registry.needs_reconsent("demo", m, enabled_stale) is True

    assert registry.armed("demo", m, disabled) is False
    assert registry.needs_reconsent("demo", m, disabled) is False

    # Absent grant -> neither.
    assert registry.armed("demo", m, {}) is False
    assert registry.needs_reconsent("demo", m, {}) is False


# --------------------------------------------------------------------------- #
# grants persistence
# --------------------------------------------------------------------------- #
def test_grants_round_trip(tmp_path) -> None:
    grants = {"demo": {"enabled": True, "manifest_hash": "abc123", "settings": {"port": 10110}}}
    registry.save_grants(tmp_path, grants)
    assert registry.load_grants(tmp_path) == grants


def test_grants_missing_file_is_empty(tmp_path) -> None:
    assert registry.load_grants(tmp_path) == {}


def test_grants_corrupt_file_is_empty(tmp_path) -> None:
    (tmp_path / "connectors.json").write_text("{not valid json", encoding="utf-8")
    assert registry.load_grants(tmp_path) == {}
    # A valid-JSON-but-not-a-mapping file is also tolerated.
    (tmp_path / "connectors.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert registry.load_grants(tmp_path) == {}


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
class _DemoConnector(Connector):
    def __init__(self, settings: dict) -> None:
        self._settings = settings

    @property
    def manifest(self) -> ConnectorManifest:
        return _manifest(name="demo")

    async def start(self, ctx: ConnectorContext) -> None:  # pragma: no cover - trivial
        pass

    async def stop(self) -> None:  # pragma: no cover - trivial
        pass


def test_registry_register_and_build_round_trip() -> None:
    registry.register_connector("demo-reg", lambda settings: _DemoConnector(settings))
    assert registry.has("demo-reg")
    assert "demo-reg" in registry.names()
    built = registry.build("demo-reg", {"k": "v"})
    assert isinstance(built, _DemoConnector)
    assert built._settings == {"k": "v"}
    assert registry.spec("demo-reg") is not None


def test_registry_reregister_is_idempotent() -> None:
    registry.register_connector("demo-idem", lambda settings: _DemoConnector(settings))
    registry.register_connector("demo-idem", lambda settings: _DemoConnector(settings))
    assert registry.names().count("demo-idem") == 1


def test_registry_version_mismatch_still_registers() -> None:
    registry.register_connector(
        "demo-ver", lambda settings: _DemoConnector(settings), api_version=999
    )
    assert registry.has("demo-ver")


# --------------------------------------------------------------------------- #
# Connector default debug()
# --------------------------------------------------------------------------- #
def test_default_debug_never_raises() -> None:
    c = _DemoConnector({})
    out = c.debug()
    assert isinstance(out, str)
    assert "_DemoConnector" in out
    assert c.status() == {}


# --------------------------------------------------------------------------- #
# load_connectors failure isolation (Fix 4f)
# --------------------------------------------------------------------------- #
def test_load_connectors_skips_bad_module_and_succeeds(monkeypatch) -> None:
    """A connector module whose import raises is logged + skipped; the rest of
    the in-tree connectors still load and load_connectors() does not raise
    (Fix 4f).  Mirrors how the driver loader handles bad entries."""
    import importlib
    import pkgutil
    import vanchor.connectors as _conn_pkg

    real_import = importlib.import_module
    bad_name = "vanchor.connectors.fake_bad_module"

    def _patched_import(name, *args, **kwargs):
        if name == bad_name:
            raise ImportError("simulated bad connector import")
        return real_import(name, *args, **kwargs)

    # Inject a fake bad module entry before the real ones.
    real_iter = pkgutil.iter_modules

    class _FakeMod:
        name = "fake_bad_module"
        ispkg = False
        module_finder = None

    def _patched_iter(path):
        yield _FakeMod()
        yield from real_iter(path)

    monkeypatch.setattr(importlib, "import_module", _patched_import)
    monkeypatch.setattr(pkgutil, "iter_modules", _patched_iter)

    # Reset the idempotency flag so the load loop actually runs.
    orig_loaded = _conn_pkg._loaded
    _conn_pkg._loaded = False
    try:
        _conn_pkg.load_connectors()   # must NOT raise
    finally:
        _conn_pkg._loaded = orig_loaded  # restore so other tests are unaffected

    # The real in-tree connectors must still be registered after the failed load.
    assert registry.has("metrics"), "metrics connector must still be registered"
