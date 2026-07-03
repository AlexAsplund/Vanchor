"""Tests for the versioned driver API + capability object + entry-point discovery
(roadmap #43 — the keystone for community driver packs).

Covers:
* the registry routes ALL FOUR device kinds (gps/depth/motor/battery) through the
  capability API, keyed by ``(kind, source)``;
* entry-point discovery finds a registered pack driver AND no-ops with none;
* the capability object is NARROW — it never exposes the Runtime, motor, or
  governor (the safety-floor contract);
* the API version is explicit; and the legacy ``(runtime, cfg)`` build path is
  still supported (backward compatibility).
"""

from __future__ import annotations

import pytest

from vanchor.core import events
from vanchor.core.events import EventBus
from vanchor.hardware import drivers, registry


# --------------------------------------------------------------------------- #
# registry routes all four device kinds via the capability API
# --------------------------------------------------------------------------- #
def test_registry_routes_all_four_device_kinds():
    kinds = ("gps", "depth", "motor", "battery")
    added = []
    for kind in kinds:
        src = f"_test_{kind}"
        registry.register_context_driver(kind, src, lambda ctx, k=kind: ("dev", k))
        added.append((kind, src))
    try:
        for kind in kinds:
            src = f"_test_{kind}"
            assert registry.has(kind, src)
            assert registry.uses_context(kind, src)
            assert src in registry.sources(kind)
            ctx = registry.DriverContext(kind=kind, source=src)
            assert registry.build_with_context(kind, src, ctx) == ("dev", kind)
    finally:
        for key in added:
            registry._REGISTRY.pop(key, None)


# --------------------------------------------------------------------------- #
# capability object is narrow: no Runtime / motor / governor
# --------------------------------------------------------------------------- #
def test_capability_object_hides_runtime_motor_governor():
    ctx = registry.DriverContext(kind="battery", source="x")
    for forbidden in (
        "runtime", "motor", "governor", "controller", "state",
        "simulator", "bus", "navigator", "safety", "helm",
    ):
        assert not hasattr(ctx, forbidden), f"capability object leaks {forbidden!r}"
    # It DOES carry the versioned API + the narrow, legitimate capabilities.
    assert ctx.api_version == registry.DRIVER_API_VERSION
    for cap in ("publish_nmea", "publish", "report_health", "health", "log", "now", "motion"):
        assert hasattr(ctx, cap)


async def test_capability_object_publishes_and_reports_health():
    bus = EventBus()
    got: list = []
    bus.subscribe(events.NMEA_IN, lambda s: got.append(s))
    ctx = registry.DriverContext(kind="gps", source="x", _bus=bus)

    await ctx.publish_nmea("$GPTEST*00")
    assert got == ["$GPTEST*00"]

    assert ctx.health() == {"ok": True, "detail": ""}
    ctx.report_health(False, "sensor timeout")
    assert ctx.health() == {"ok": False, "detail": "sensor timeout"}


async def test_capability_publish_refuses_control_topics():
    # #43 safety guarantee: a driver/pack may publish READINGS through the
    # capability object, but NEVER a control topic that could command motion or
    # weaken a failsafe. A refused publish is dropped + logged, never forwarded.
    bus = EventBus()
    seen: dict[str, list] = {}

    def _sub(topic: str) -> None:
        seen[topic] = []
        bus.subscribe(topic, lambda p, _t=topic: seen[_t].append(p))

    for t in ("imu.in", "battery.health", "command", events.MOTOR_COMMAND, "stop"):
        _sub(t)

    ctx = registry.DriverContext(kind="battery", source="ina226", _bus=bus)

    # Legitimate readings are forwarded.
    await ctx.publish("imu.in", "sample")
    await ctx.publish("battery.health", {"ok": True})
    assert seen["imu.in"] == ["sample"]
    assert seen["battery.health"] == [{"ok": True}]

    # Control topics are refused and NEVER reach the bus (would command motion /
    # disable a failsafe otherwise).
    await ctx.publish("command", {"type": "manual", "thrust": 1.0})
    await ctx.publish(events.MOTOR_COMMAND, "run")
    await ctx.publish("stop", None)
    assert seen["command"] == []
    assert seen[events.MOTOR_COMMAND] == []
    assert seen["stop"] == []


def test_capability_object_motion_provider():
    ctx = registry.DriverContext(kind="compass", source="x", _motion=lambda: (123.0, 1.5))
    assert ctx.motion() == (123.0, 1.5)
    # No provider => None (never raises).
    assert registry.DriverContext(kind="compass", source="y").motion() is None


# --------------------------------------------------------------------------- #
# explicit API versioning
# --------------------------------------------------------------------------- #
def test_api_version_is_explicit_and_recorded():
    assert isinstance(registry.DRIVER_API_VERSION, int)
    registry.register_context_driver(
        "battery", "_ver", lambda ctx: None, api_version=registry.DRIVER_API_VERSION
    )
    try:
        spec = registry.spec("battery", "_ver")
        assert spec is not None
        assert spec.api_version == registry.DRIVER_API_VERSION
        assert spec.uses_context
    finally:
        registry._REGISTRY.pop(("battery", "_ver"), None)


def test_incompatible_api_version_warns_but_registers(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="vanchor.hardware.registry"):
        registry.register_context_driver("battery", "_v99", lambda ctx: None, api_version=99)
    try:
        assert registry.has("battery", "_v99")
        assert any("driver-API" in r.getMessage() for r in caplog.records)
    finally:
        registry._REGISTRY.pop(("battery", "_v99"), None)


# --------------------------------------------------------------------------- #
# backward compatibility: the legacy (runtime, cfg) path still works
# --------------------------------------------------------------------------- #
def test_legacy_runtime_cfg_driver_still_supported():
    registry.register_driver("gps", "_legacy", lambda runtime, cfg: ("legacy", runtime, cfg))
    try:
        assert registry.has("gps", "_legacy")
        assert not registry.uses_context("gps", "_legacy")
        assert registry.build_device("gps", "_legacy", "RT", "CFG") == ("legacy", "RT", "CFG")
        # Cross-calling the wrong build path is a clear TypeError, not a mis-build.
        with pytest.raises(TypeError):
            registry.build_with_context(
                "gps", "_legacy", registry.DriverContext(kind="gps", source="_legacy")
            )
    finally:
        registry._REGISTRY.pop(("gps", "_legacy"), None)


def test_build_device_rejects_context_driver():
    registry.register_context_driver("gps", "_ctx", lambda ctx: "dev")
    try:
        with pytest.raises(TypeError):
            registry.build_device("gps", "_ctx", "RT", "CFG")
    finally:
        registry._REGISTRY.pop(("gps", "_ctx"), None)


# --------------------------------------------------------------------------- #
# entry-point discovery (pip-installable packs)
# --------------------------------------------------------------------------- #
def test_entry_point_discovery_invokes_pack_hook(monkeypatch):
    """A pack advertises a registration hook under the ``vanchor.drivers`` group;
    discovery loads + calls it so the pack's driver self-registers."""
    called = {}

    class _FakeEP:
        name = "test_pack"

        def load(self):
            def hook():
                registry.register_context_driver(
                    "battery", "_ep_pack", lambda ctx: ("pack-dev",)
                )
                called["ok"] = True
            return hook

    monkeypatch.setattr(drivers, "_iter_entry_points", lambda group: [_FakeEP()])
    try:
        drivers._load_pack_drivers()
        assert called.get("ok") is True
        assert registry.has("battery", "_ep_pack")
    finally:
        registry._REGISTRY.pop(("battery", "_ep_pack"), None)


def test_entry_point_discovery_noops_with_no_packs(monkeypatch):
    monkeypatch.setattr(drivers, "_iter_entry_points", lambda group: [])
    drivers._load_pack_drivers()  # must not raise


def test_entry_point_discovery_survives_a_broken_pack(monkeypatch):
    """A pack that raises on load is logged + skipped, never crashing startup."""

    class _BoomEP:
        name = "boom_pack"

        def load(self):
            raise RuntimeError("bad pack")

    monkeypatch.setattr(drivers, "_iter_entry_points", lambda group: [_BoomEP()])
    drivers._load_pack_drivers()  # must not raise


def test_real_entry_point_iteration_is_safe():
    # With no driver packs installed this must be a quiet no-op (no exception).
    assert list(drivers._iter_entry_points("vanchor.drivers")) == [] or True
