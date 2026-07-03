"""Tests for the battery-monitor driver — the reference registry-driven 4th
device kind (roadmap #42), built via the versioned capability API (#43).

Covers:
* the ``sim`` monitor mirrors the simulator's battery telemetry exactly;
* the ``ina226`` driver reads voltage/current/SoC from a FAKE shunt (no bench
  hardware) and reports health;
* SoC is seeded from resting voltage and coulomb-counted down under load;
* the driver is registered against the capability API and routes through a
  Runtime's ``battery_source`` — with the narrow capability object carrying no
  Runtime/motor/governor.
"""

from __future__ import annotations

import asyncio

import pytest

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.hardware import registry
from vanchor.hardware.drivers import load_drivers
from vanchor.hardware.drivers.battery import (
    FakeShunt,
    INA226BatteryMonitor,
    SimBatteryMonitor,
)
from vanchor.sim.battery import Battery, BatteryConfig as SimBatteryConfig

_SNAPSHOT_KEYS = {"soc_pct", "voltage_v", "current_a", "draw_w", "range_m", "time_to_empty_s"}


# --------------------------------------------------------------------------- #
# sim monitor: a faithful read-view of the simulated pack
# --------------------------------------------------------------------------- #
def test_sim_battery_monitor_mirrors_sim_battery():
    batt = Battery(SimBatteryConfig(capacity_ah=100.0), soc_pct=73.0)
    mon = SimBatteryMonitor(batt)
    assert mon.snapshot() == batt.to_dict()
    assert set(mon.snapshot()) == _SNAPSHOT_KEYS
    assert mon.health()["ok"] is True
    # Draining the underlying pack is reflected (same object, live view).
    batt.step(3600.0, thrust=1.0, sog_mps=1.0)
    assert mon.snapshot() == batt.to_dict()


# --------------------------------------------------------------------------- #
# ina226 driver: reads a FAKE shunt, estimates SoC, reports health
# --------------------------------------------------------------------------- #
def test_ina226_reads_voltage_and_current_from_fake_shunt():
    mon = INA226BatteryMonitor(FakeShunt(voltage_v=12.6, current_a=8.0), soc_pct=80.0)
    mon.read_once(dt=1.0, sog_mps=0.0)
    assert mon.voltage_v == 12.6
    assert mon.current_a == 8.0
    assert mon.health()["ok"] is True


def test_ina226_seeds_soc_from_resting_voltage():
    # full = nominal+0.7 = 12.7, empty = nominal-0.2 = 11.8.
    full = INA226BatteryMonitor(FakeShunt(voltage_v=12.7), SimBatteryConfig(nominal_v=12.0))
    full.read_once(dt=0.0)
    assert full.soc_pct == pytest.approx(100.0)
    empty = INA226BatteryMonitor(FakeShunt(voltage_v=11.8), SimBatteryConfig(nominal_v=12.0))
    empty.read_once(dt=0.0)
    assert empty.soc_pct == pytest.approx(0.0)


def test_fresh_ina226_monitor_reports_soc_unknown_not_zero():
    # #49 fix: before any successful shunt read the SoC is UNKNOWN (None), never
    # 0.0 -- reporting 0.0 makes the low-battery ladder read a critically-empty
    # pack and force a startup thrust derate + RTL.
    mon = INA226BatteryMonitor(FakeShunt())
    assert mon.soc_pct is None
    assert mon.snapshot()["soc_pct"] is None


def test_uninitialized_battery_applies_no_ladder_cap(tmp_path):
    # An unread INA226 (soc None) must not be derated: the ladder's soc-is-None
    # guard leaves the governor's thrust cap at 1.0.
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    rt.battery_monitor = INA226BatteryMonitor(FakeShunt())  # fresh, never read
    assert rt.battery_snapshot()["soc_pct"] is None
    assert rt.evaluate_battery_ladder() == pytest.approx(1.0)
    assert rt.controller.safety.thrust_cap == pytest.approx(1.0)


def test_ina226_coulomb_counts_soc_down_under_load():
    mon = INA226BatteryMonitor(
        FakeShunt(voltage_v=12.4, current_a=10.0),
        SimBatteryConfig(capacity_ah=100.0),
        soc_pct=100.0,
    )
    mon.read_once(dt=3600.0, sog_mps=0.0)  # 10 A for 1 h = 10 Ah = 10 % of 100 Ah
    assert mon.soc_pct == pytest.approx(90.0)


def test_ina226_reports_unhealthy_on_read_failure():
    shunt = FakeShunt()
    shunt.fail = True
    mon = INA226BatteryMonitor(shunt, soc_pct=100.0)
    mon.read_once(dt=1.0)
    assert mon.health()["ok"] is False
    assert "read failed" in mon.health()["detail"]
    # A failed read must not advance the integrator.
    assert mon.soc_pct == 100.0


def test_ina226_snapshot_shape_and_estimates():
    cfg = SimBatteryConfig(capacity_ah=100.0, reserve_pct=0.0, draw_tau_s=0.0)
    mon = INA226BatteryMonitor(FakeShunt(voltage_v=12.0, current_a=10.0), cfg, soc_pct=100.0)
    mon.read_once(dt=1.0, sog_mps=2.0)
    snap = mon.snapshot()
    assert set(snap) == _SNAPSHOT_KEYS
    assert snap["voltage_v"] == 12.0
    assert snap["current_a"] == 10.0
    assert snap["draw_w"] == pytest.approx(120.0, abs=0.1)
    # With a non-trivial draw + speed, both estimates are finite + positive.
    assert snap["time_to_empty_s"] is not None and snap["time_to_empty_s"] > 0
    assert snap["range_m"] > 0


def test_ina226_time_to_empty_infinite_becomes_null():
    mon = INA226BatteryMonitor(FakeShunt(voltage_v=12.6, current_a=0.0), soc_pct=100.0)
    mon.read_once(dt=1.0, sog_mps=0.0)  # no draw -> tte inf -> JSON null
    assert mon.snapshot()["time_to_empty_s"] is None


async def test_ina226_poll_loop_reads_and_closes():
    shunt = FakeShunt(voltage_v=12.4, current_a=3.0)
    mon = INA226BatteryMonitor(shunt, poll_hz=200.0, soc_pct=50.0)
    await mon.start()
    try:
        await asyncio.sleep(0.05)  # several poll periods
    finally:
        await mon.stop()
    assert mon.current_a == 3.0
    assert mon.voltage_v == 12.4
    assert shunt.closed is True  # stop() closes the shunt


# --------------------------------------------------------------------------- #
# registry: ina226 is a context (capability-API) battery driver
# --------------------------------------------------------------------------- #
def test_ina226_registered_as_context_battery_driver():
    load_drivers()
    assert registry.has("battery", "ina226")
    assert registry.uses_context("battery", "ina226")
    assert "ina226" in registry.sources("battery")


# --------------------------------------------------------------------------- #
# Runtime integration
# --------------------------------------------------------------------------- #
def test_runtime_default_uses_sim_battery_monitor(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    assert isinstance(rt.battery_monitor, SimBatteryMonitor)
    # Telemetry is unchanged from the pre-existing sim battery path.
    assert rt.battery_snapshot() == rt.simulator.battery.to_dict()


def test_battery_source_none_disables_monitor(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    cfg.hardware.battery_source = "none"
    rt = Runtime(cfg)
    assert rt.battery_monitor is None
    # Falls back to the sim battery directly, so telemetry still works.
    assert rt.battery_snapshot() == rt.simulator.battery.to_dict()


def test_runtime_battery_source_routes_through_capability_api(tmp_path):
    """A ``battery_source`` selecting a capability-API driver is built with the
    NARROW context object (no runtime/motor/governor), and its telemetry flows
    through ``battery_snapshot``."""
    captured: dict = {}

    def _build(ctx):
        captured["ctx"] = ctx
        return INA226BatteryMonitor(
            FakeShunt(voltage_v=12.5, current_a=4.0), soc_pct=90.0,
            now=ctx.now, motion=ctx.motion,
        )

    registry.register_context_driver("battery", "_fake_ina", _build)
    try:
        cfg = load(None)
        cfg.data_dir = str(tmp_path)
        cfg.hardware.battery_source = "_fake_ina"
        rt = Runtime(cfg)
        assert isinstance(rt.battery_monitor, INA226BatteryMonitor)
        # The capability object handed to the driver leaked nothing dangerous.
        ctx = captured["ctx"]
        for forbidden in ("runtime", "motor", "governor", "controller", "simulator", "state"):
            assert not hasattr(ctx, forbidden)
        # Its config slice is the app battery config (narrow, read-only intent).
        assert ctx.config is rt.config.battery
        # Telemetry flows through battery_snapshot once the driver has read once.
        rt.battery_monitor.read_once(dt=1.0, sog_mps=0.0)
        snap = rt.battery_snapshot()
        assert snap["voltage_v"] == 12.5
        assert snap["current_a"] == 4.0
    finally:
        registry._REGISTRY.pop(("battery", "_fake_ina"), None)


def test_bad_battery_driver_is_skipped_not_fatal(tmp_path):
    """A battery driver that can't be built (no hardware/lib) must not crash
    startup — the rest of the boat still runs (mirrors the compass resilience)."""

    def _boom(ctx):
        raise RuntimeError("no shunt here")

    registry.register_context_driver("battery", "_boom_batt", _boom)
    try:
        cfg = load(None)
        cfg.data_dir = str(tmp_path)
        cfg.hardware.battery_source = "_boom_batt"
        rt = Runtime(cfg)  # must not raise
        assert rt.battery_monitor is None
        assert rt.simulator is not None
    finally:
        registry._REGISTRY.pop(("battery", "_boom_batt"), None)


def test_set_device_config_validates_battery_source(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    with pytest.raises(ValueError):
        rt.set_device_config({"hardware": {"battery_source": "bogus"}})
    res = rt.set_device_config({"hardware": {"battery_source": "ina226"}})
    assert res["ok"] is True
    assert rt.config.hardware.battery_source == "ina226"
