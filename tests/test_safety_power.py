"""Tests for the five safety/power backend features:

* #60 battery monitor (drain + range/time estimates),
* #61 Return-to-Launch (plan + follow + auto-recommend threshold),
* #62 shallow-water / no-go auto-stop (cuts thrust),
* #63 man-overboard return,
* #64 lost-connection failsafe (engages hold-position after timeout).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.controller.safety import SafetyConfig, SafetyGovernor
from vanchor.core.config import AppConfig
from vanchor.core.models import ControlModeName, GeoPoint, GpsFix, MotorCommand
from vanchor.core.state import NavigationState
from vanchor.sim.battery import Battery, BatteryConfig
from vanchor.ui.server import create_app


# --------------------------------------------------------------------------- #
# #60 Battery monitor
# --------------------------------------------------------------------------- #
def test_battery_drains_under_thrust():
    bat = Battery(BatteryConfig(capacity_ah=10.0, idle_a=0.0, load_a=40.0))
    assert bat.soc_pct == 100.0
    # Full thrust draws 40 A; over 360 s that's 4 Ah = 40% of a 10 Ah pack.
    for _ in range(360):
        bat.step(1.0, thrust=1.0, sog_mps=1.0)
    assert bat.soc_pct == pytest.approx(60.0, abs=0.5)
    assert bat.current_a == pytest.approx(40.0)


def test_battery_idle_draw_only():
    bat = Battery(BatteryConfig(capacity_ah=100.0, idle_a=1.0, load_a=40.0))
    bat.step(1.0, thrust=0.0, sog_mps=0.0)
    assert bat.current_a == pytest.approx(1.0)


def test_battery_range_and_time_estimates():
    bat = Battery(BatteryConfig(capacity_ah=100.0, idle_a=0.0, load_a=10.0, reserve_pct=0.0))
    # Steady half-thrust (5 A) at 1 m/s for a while to settle the averages.
    for _ in range(200):
        bat.step(1.0, thrust=0.5, sog_mps=1.0)
    # ~100 Ah / 5 A = 20 h = 72000 s, minus what's been drawn (~0.28 Ah).
    assert bat.time_to_empty_s > 60000.0
    # range = avg_sog (~1 m/s) * time_to_empty -> very large, and positive.
    assert bat.range_m > 60000.0


def test_battery_no_draw_time_is_infinite():
    bat = Battery(BatteryConfig(idle_a=0.0, load_a=0.0))
    bat.step(1.0, thrust=0.0, sog_mps=0.0)
    assert bat.time_to_empty_s == float("inf")
    assert bat.range_m == 0.0
    assert bat.to_dict()["time_to_empty_s"] is None


def test_battery_set_soc_clamps():
    bat = Battery()
    bat.set_soc(150.0)
    assert bat.soc_pct == 100.0
    bat.set_soc(-5.0)
    assert bat.soc_pct == 0.0


def test_battery_telemetry_shape():
    bat = Battery()
    bat.step(1.0, thrust=0.3, sog_mps=0.8)
    d = bat.to_dict()
    assert set(d) == {
        "soc_pct",
        "voltage_v",
        "current_a",
        "draw_w",
        "range_m",
        "time_to_empty_s",
    }


def test_simulator_drains_battery():
    rt = Runtime()
    start = rt.simulator.battery.soc_pct
    rt.simulator.motor.apply(MotorCommand(thrust=1.0))
    for _ in range(100):
        rt.simulator.step(0.5)
    assert rt.simulator.battery.soc_pct < start


def test_set_battery_command():
    rt = Runtime()
    rt.handle_command({"type": "set_battery", "soc_pct": 42.0})
    assert rt.simulator.battery.soc_pct == pytest.approx(42.0)


# --------------------------------------------------------------------------- #
# #90 Sim teleport
# --------------------------------------------------------------------------- #
def test_teleport_moves_truth_and_zeroes_velocity():
    rt = Runtime()
    # Give the boat some momentum first so we can prove the teleport stops it.
    rt.simulator.motor.apply(MotorCommand(thrust=1.0, steering=0.5))
    for _ in range(40):
        rt.simulator.step(0.1)
    assert rt.simulator.truth().speed_mps > 0.0

    rt.handle_command({"type": "teleport", "lat": 60.5, "lon": 14.25, "heading": 90.0})

    truth = rt.simulator.truth()
    assert truth.point.lat == pytest.approx(60.5)
    assert truth.point.lon == pytest.approx(14.25)
    assert truth.heading_deg == pytest.approx(90.0)
    # Velocity is reset so the boat doesn't keep coasting.
    assert truth.speed_mps == pytest.approx(0.0)
    assert truth.ground_ve == pytest.approx(0.0)
    assert truth.ground_vn == pytest.approx(0.0)


def test_teleport_without_heading_keeps_heading():
    rt = Runtime()
    rt.simulator.boat.state.heading_deg = 123.0
    rt.handle_command({"type": "teleport", "lat": 58.0, "lon": 12.0})
    truth = rt.simulator.truth()
    assert truth.point.lat == pytest.approx(58.0)
    assert truth.heading_deg == pytest.approx(123.0)
    assert truth.speed_mps == pytest.approx(0.0)


def test_teleport_no_simulator_is_noop():
    rt = Runtime()
    rt.simulator = None  # emulate real-hardware (no simulator) wiring
    # Must not raise -- a safe no-op on real hardware.
    rt.handle_command({"type": "teleport", "lat": 60.0, "lon": 14.0})


# --------------------------------------------------------------------------- #
# #62 Shallow-water / no-go auto-stop
# --------------------------------------------------------------------------- #
def _state(depth=0.0, lat=59.0, lon=18.0, mode=ControlModeName.WAYPOINT):
    s = NavigationState()
    s.mode = mode
    s.depth_m = depth
    s.fix = GpsFix(point=GeoPoint(lat, lon))
    return s


def test_shallow_water_cuts_thrust():
    gov = SafetyGovernor(SafetyConfig(min_depth_m=1.0, max_thrust_slew_per_s=100.0))
    cmd, status = gov.govern(MotorCommand(thrust=0.8), _state(depth=0.5), 0.2, True)
    assert status.shallow_stop
    assert cmd.thrust == 0.0
    assert status.min_depth_m == 1.0


def test_deep_water_does_not_stop():
    gov = SafetyGovernor(SafetyConfig(min_depth_m=1.0, max_thrust_slew_per_s=100.0))
    cmd, status = gov.govern(MotorCommand(thrust=0.8), _state(depth=3.0), 0.2, True)
    assert not status.shallow_stop
    assert cmd.thrust > 0.0


def test_unknown_depth_never_false_triggers():
    # depth <= 0 means no sounding -> must not trip the shallow stop.
    gov = SafetyGovernor(SafetyConfig(min_depth_m=2.0, max_thrust_slew_per_s=100.0))
    cmd, status = gov.govern(MotorCommand(thrust=0.8), _state(depth=0.0), 0.2, True)
    assert not status.shallow_stop
    assert cmd.thrust > 0.0


def test_min_depth_zero_disables_check():
    gov = SafetyGovernor(SafetyConfig(min_depth_m=0.0, max_thrust_slew_per_s=100.0))
    _, status = gov.govern(MotorCommand(thrust=0.8), _state(depth=0.1), 0.2, True)
    assert not status.shallow_stop


def test_nogo_zone_cuts_thrust_inside():
    gov = SafetyGovernor(SafetyConfig(max_thrust_slew_per_s=100.0, nogo_lookahead_m=0.0))
    # A small box around (59.0, 18.0).
    gov.set_nogo_zones([[(58.99, 17.99), (58.99, 18.01), (59.01, 18.01), (59.01, 17.99)]])
    cmd, status = gov.govern(MotorCommand(thrust=0.8), _state(lat=59.0, lon=18.0), 0.2, True)
    assert status.nogo_stop
    assert cmd.thrust == 0.0


def test_nogo_zone_clear_outside():
    gov = SafetyGovernor(SafetyConfig(max_thrust_slew_per_s=100.0, nogo_lookahead_m=5.0))
    gov.set_nogo_zones([[(58.99, 17.99), (58.99, 18.01), (59.01, 18.01), (59.01, 17.99)]])
    # Far away.
    cmd, status = gov.govern(MotorCommand(thrust=0.8), _state(lat=59.5, lon=18.5), 0.2, True)
    assert not status.nogo_stop
    assert cmd.thrust > 0.0


def test_nogo_lookahead_stops_before_entering():
    gov = SafetyGovernor(SafetyConfig(max_thrust_slew_per_s=100.0, nogo_lookahead_m=50.0))
    gov.set_nogo_zones([[(58.99, 17.99), (58.99, 18.01), (59.01, 18.01), (59.01, 17.99)]])
    # Just north of the box edge (58.99 lat), ~30 m outside -> within 50 m lookahead.
    cmd, status = gov.govern(
        MotorCommand(thrust=0.8), _state(lat=58.9897, lon=18.0), 0.2, True
    )
    assert status.nogo_stop
    assert cmd.thrust == 0.0


def test_set_nogo_and_min_depth_commands(_runtime):
    rt = _runtime
    rt.handle_command({"type": "set_min_depth", "min_depth_m": 1.5})
    assert rt.controller.safety.config.min_depth_m == 1.5
    rt.handle_command(
        {"type": "set_nogo_zones", "zones": [[[59.0, 18.0], [59.0, 18.1], [59.1, 18.1]]]}
    )
    assert rt.controller.safety.nogo_zone_count == 1


# --------------------------------------------------------------------------- #
# #63 Man-overboard
# --------------------------------------------------------------------------- #
def test_mob_records_point_and_navigates(_runtime):
    rt = _runtime
    rt.state.fix = GpsFix(point=GeoPoint(59.5, 18.2))
    rt.handle_command({"type": "mob"})
    assert rt.state.mob_active
    assert rt.state.mob == GeoPoint(59.5, 18.2)
    assert rt.state.mode == ControlModeName.WAYPOINT
    assert len(rt.state.waypoints) == 1
    assert rt.state.waypoints[0].point == GeoPoint(59.5, 18.2)
    assert rt.state.route_on_arrival == "stop"


def test_mob_clear(_runtime):
    rt = _runtime
    rt.state.fix = GpsFix(point=GeoPoint(59.5, 18.2))
    rt.handle_command({"type": "mob"})
    rt.handle_command({"type": "mob_clear"})
    assert not rt.state.mob_active


def test_mob_ignored_without_fix(_runtime):
    rt = _runtime
    rt.state.fix = None
    rt.handle_command({"type": "mob"})
    assert not rt.state.mob_active


def test_mob_telemetry_shape(_runtime):
    rt = _runtime
    rt.state.fix = GpsFix(point=GeoPoint(59.5, 18.2))
    rt.handle_command({"type": "mob"})
    mob = rt.state.to_dict()["mob"]
    assert mob == {"active": True, "lat": 59.5, "lon": 18.2}


# --------------------------------------------------------------------------- #
# #61 Return-to-Launch (auto-record + recommend threshold)
# --------------------------------------------------------------------------- #
def test_launch_auto_recorded_on_first_fix(_runtime):
    rt = _runtime
    assert rt.state.launch is None
    rt.state.fix = GpsFix(point=GeoPoint(59.6, 13.3))
    rt.controller.maybe_record_launch()
    assert rt.state.launch == GeoPoint(59.6, 13.3)
    # Idempotent: a later fix doesn't move it.
    rt.state.fix = GpsFix(point=GeoPoint(59.7, 13.4))
    rt.controller.maybe_record_launch()
    assert rt.state.launch == GeoPoint(59.6, 13.3)


def test_set_launch_command(_runtime):
    rt = _runtime
    rt.state.fix = GpsFix(point=GeoPoint(59.6, 13.3))
    rt.state.launch = GeoPoint(0.1, 0.1)  # pre-existing
    rt.handle_command({"type": "set_launch"})
    assert rt.state.launch == GeoPoint(59.6, 13.3)


def test_rtl_recommended_when_battery_range_near_home(_runtime):
    rt = _runtime
    rt.state.launch = GeoPoint(59.0, 18.0)
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.05))  # ~2.8 km east of launch
    # Force a known battery range just above the distance-home + margin.
    rt.config.safety.rtl_margin_m = 100.0

    class _FakeBat:
        @staticmethod
        def to_dict():
            return {"range_m": 50000.0}  # plenty -> no recommend

    rt.simulator.battery.to_dict = _FakeBat.to_dict
    assert rt.evaluate_rtl_recommend() is False
    assert rt.state.rtl_recommended is False

    # Now make range *just* enough to make it home (within the margin).
    from vanchor.core.geo import haversine_m

    dist = haversine_m(rt.state.position, rt.state.launch)
    rt.simulator.battery.to_dict = lambda: {"range_m": dist + 50.0}
    assert rt.evaluate_rtl_recommend() is True
    assert rt.state.rtl_recommended is True


def test_return_to_launch_plans_and_follows(_runtime, monkeypatch):
    rt = _runtime
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    rt.state.launch = GeoPoint(59.01, 18.02)
    # Stub the (network/CPU-heavy) water plan with a fixed route home.
    monkeypatch.setattr(
        rt,
        "plan_route",
        lambda lat, lon, mode="fastest", offset_m=25.0: {
            "ok": True,
            "waypoints": [
                {"name": "WP1", "lat": 59.005, "lon": 18.01},
                {"name": "DEST", "lat": 59.01, "lon": 18.02},
            ],
            "message": "ok",
        },
    )
    res = rt.return_to_launch()
    assert res["ok"]
    assert rt.state.mode == ControlModeName.WAYPOINT
    assert len(rt.state.waypoints) == 2
    assert rt.state.waypoints[-1].point == GeoPoint(59.01, 18.02)
    assert rt.state.route_on_arrival == "anchor"


def test_return_to_launch_without_launch(_runtime):
    rt = _runtime
    rt.state.launch = None
    res = rt.return_to_launch()
    assert res["ok"] is False


def test_return_to_launch_plans_real_water_route():
    """End-to-end: plan_route over the real water fixture, then follow it."""
    from pathlib import Path

    from vanchor.nav import routing, water

    poly = water.load_geojson(Path(__file__).parent / "data" / "water_sim.geojson")
    rt = Runtime()
    # Pick a start + launch both inside the lake (the sim start is inside it).
    start = GeoPoint(rt.config.sim.start_lat, rt.config.sim.start_lon)
    rt.state.fix = GpsFix(point=start)
    rt.state.launch = start  # route home to self -> trivially ok
    res = routing.plan_route(
        start_lat=start.lat,
        start_lon=start.lon,
        dest_lat=start.lat,
        dest_lon=start.lon,
        water_ll=poly,
    )
    assert res.ok


def test_auto_rtl_engages(_runtime, monkeypatch):
    rt = _runtime
    rt.config.safety.auto_rtl = True
    rt.config.safety.rtl_margin_m = 1e9  # always within margin
    rt.state.launch = GeoPoint(59.0, 18.0)
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.05))
    rt.simulator.battery.to_dict = lambda: {"range_m": 10.0}
    engaged = {"n": 0}
    monkeypatch.setattr(rt, "return_to_launch", lambda: engaged.__setitem__("n", 1) or {})
    rt.evaluate_rtl_recommend()
    assert engaged["n"] == 1


# --------------------------------------------------------------------------- #
# #64 Lost-connection failsafe (clock-injected)
# --------------------------------------------------------------------------- #
def _underway_runtime(now_box):
    # The lost-link failsafe measures DURATION on the injectable MONOTONIC clock
    # (mono_fn), so drive that seam in tests -- not the wall-clock now_fn.
    rt = Runtime(mono_fn=lambda: now_box[0])
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    rt.state.mode = ControlModeName.HEADING_HOLD  # making way
    return rt


def test_link_failsafe_engages_after_timeout():
    now = [1000.0]
    rt = _underway_runtime(now)
    rt.config.safety.link_loss_timeout_s = 20.0
    rt.config.safety.link_loss_continue_mission = False  # test the hold path
    # A client connected, then disconnected at t=1000.
    rt.client_connected()
    rt.client_disconnected()
    # Still underway; clock not yet past the timeout.
    now[0] = 1015.0
    assert rt.evaluate_link_failsafe() is False
    assert rt.state.mode == ControlModeName.HEADING_HOLD
    # Past the timeout -> hold-position engaged.
    now[0] = 1021.0
    assert rt.evaluate_link_failsafe() is True
    assert rt.state.mode == ControlModeName.ANCHOR_HOLD
    assert rt._link_failsafe_engaged


def test_link_failsafe_not_engaged_while_connected():
    now = [0.0]
    rt = _underway_runtime(now)
    rt.client_connected()  # still connected
    now[0] = 1000.0
    assert rt.evaluate_link_failsafe() is False


def test_link_failsafe_not_engaged_when_idle():
    now = [0.0]
    rt = Runtime(mono_fn=lambda: now[0])
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    rt.state.mode = ControlModeName.MANUAL  # idle-manual: zero thrust -> not underway
    rt.client_connected()
    rt.client_disconnected()
    now[0] = 1000.0
    assert rt.evaluate_link_failsafe() is False


def test_link_failsafe_clears_on_reconnect():
    now = [0.0]
    rt = _underway_runtime(now)
    rt.config.safety.link_loss_timeout_s = 5.0
    rt.client_connected()
    rt.client_disconnected()
    now[0] = 10.0
    assert rt.evaluate_link_failsafe() is True
    assert rt._link_failsafe_engaged
    # Reconnect clears the failsafe flag.
    rt.client_connected()
    assert not rt._link_failsafe_engaged


def test_link_failsafe_stops_manual_driving():
    """A client loss while DRIVING MANUALLY (thrust up) must STOP the motor --
    not anchor-hold (there's no target to hold) and definitely not keep
    motoring forever (#64)."""
    now = [1000.0]
    rt = Runtime(mono_fn=lambda: now[0])
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    rt.state.mode = ControlModeName.MANUAL
    # Actually driving by hand at 0.8 thrust.
    rt.state.motor_command = MotorCommand(thrust=0.8, steering=0.3)
    rt.controller.manual.set(0.8, 0.3)
    rt.config.safety.link_loss_timeout_s = 10.0
    rt.client_connected()
    rt.client_disconnected()
    now[0] = 1011.0  # past the timeout
    assert rt.evaluate_link_failsafe() is True
    # STOP, not anchor-hold: mode stays MANUAL and commanded thrust is zeroed.
    assert rt.state.mode == ControlModeName.MANUAL
    assert rt.controller.manual.thrust == 0.0
    assert rt._link_failsafe_engaged


def test_link_failsafe_not_engaged_manual_below_thrust_eps():
    """MANUAL with only a whisper of thrust is still idle -> no failsafe."""
    now = [1000.0]
    rt = Runtime(mono_fn=lambda: now[0])
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    rt.state.mode = ControlModeName.MANUAL
    rt.state.motor_command = MotorCommand(thrust=0.005)  # below eps
    rt.config.safety.link_loss_timeout_s = 10.0
    rt.client_connected()
    rt.client_disconnected()
    now[0] = 1011.0
    assert rt.evaluate_link_failsafe() is False


def test_link_failsafe_engages_in_work_area():
    """WORK_AREA counts as underway (a visiting/holding tour), so a link loss
    there must engage the failsafe like any other guided mode."""
    now = [1000.0]
    rt = Runtime(mono_fn=lambda: now[0])
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    rt.state.mode = ControlModeName.WORK_AREA
    rt.config.safety.link_loss_timeout_s = 10.0
    rt.config.safety.link_loss_continue_mission = False  # test the hold path
    rt.client_connected()
    rt.client_disconnected()
    now[0] = 1011.0
    assert rt.evaluate_link_failsafe() is True
    assert rt.state.mode == ControlModeName.ANCHOR_HOLD  # guided -> hold position


async def test_auto_rtl_schedules_off_the_loop():
    """#61: auto-RTL must NOT call the heavy planner inline on the live path --
    it schedules the plan on the executor and guards against duplicates."""
    rt = Runtime()
    rt.config.safety.auto_rtl = True
    rt.config.safety.rtl_margin_m = 1e9  # always within margin
    rt.state.launch = GeoPoint(59.0, 18.0)
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.05))
    rt.simulator.battery.to_dict = lambda: {"range_m": 10.0}
    calls = {"n": 0}
    rt.return_to_launch = lambda: (calls.__setitem__("n", calls["n"] + 1), {"ok": True})[1]

    rt.evaluate_rtl_recommend()
    # Scheduled, not called inline (the planner can hit a 60 s Overpass timeout).
    assert calls["n"] == 0
    assert rt._rtl_in_flight is True

    # A second evaluation while in flight must NOT launch a duplicate plan.
    rt.evaluate_rtl_recommend()

    # Let the executor-backed task run to completion.
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not rt._rtl_in_flight:
            break
    assert calls["n"] == 1              # exactly one plan ran, despite two evals
    assert rt._rtl_in_flight is False   # flag cleared for the next attempt


def test_link_telemetry_shape():
    rt = Runtime()
    link = rt.telemetry()["link"]
    assert set(link) == {"client_connected", "since_s", "failsafe_engaged", "failsafe_action"}


# --------------------------------------------------------------------------- #
# Telemetry surface smoke (battery/launch/mob through the API)
# --------------------------------------------------------------------------- #
def test_state_endpoint_has_new_fields():
    app = create_app(Runtime())
    with TestClient(app) as c:
        data = c.get("/api/state").json()
        assert set(data["battery"]) >= {"soc_pct", "voltage_v", "range_m", "time_to_empty_s"}
        assert set(data["link"]) == {"client_connected", "since_s", "failsafe_engaged", "failsafe_action"}
        assert "rtl_recommended" in data
        assert set(data["launch"]) == {"lat", "lon", "set"}
        assert set(data["mob"]) == {"active", "lat", "lon"}


@pytest.fixture()
def _runtime():
    return Runtime()
