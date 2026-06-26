"""Tests for the four nav-control backend features (#45, #49, #50, #54):

* GPS offset calibration in the navigator,
* throttle % override for guided modes,
* pause / resume / stop navigation,
* cancellable route planning.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.controller.controller import Controller
from vanchor.core.models import ControlModeName, GeoPoint, GpsFix
from vanchor.core.state import NavigationState
from vanchor.nav import nmea, routing, water
from vanchor.nav.navigator import Navigator
from vanchor.sim.devices import SimMotorController

FIXTURE = Path(__file__).parent / "data" / "water_sim.geojson"


@pytest.fixture()
def client():
    app = create_app_runtime()
    with TestClient(app) as c:
        yield c


def create_app_runtime():
    from vanchor.ui.server import create_app

    return create_app(Runtime())


def _controller_at(point: GeoPoint, heading: float = 0.0) -> Controller:
    state = NavigationState()
    state.fix = GpsFix(point=point)
    state.heading_deg = heading
    ctrl = Controller(state, SimMotorController(), bus=None)
    return ctrl


def _settle(ctrl: Controller, dt: float = 0.2, ticks: int = 60):
    """Tick the control loop with a fresh GPS fix each step so the safety
    governor's loss-of-fix failsafe never fires and the thrust slew settles."""
    cmd = ctrl.state.motor_command
    for _ in range(ticks):
        ctrl.state.fix_seq += 1  # keep the fix "fresh"
        cmd = ctrl.control_tick(dt)
    return cmd


# --------------------------------------------------------------------------- #
# #45 GPS offset calibration
# --------------------------------------------------------------------------- #
def test_gps_offset_shifts_reported_position():
    state = NavigationState()
    nav = Navigator(state, bus=None)
    nav.handle_sentence(nmea.encode_rmc(GeoPoint(59.0, 18.0), sog_knots=1, cog_deg=0))
    assert state.position.lat == pytest.approx(59.0, abs=1e-4)

    # Correct so the current fix should read (59.001, 18.002).
    nav.set_gps_offset(59.001, 18.002)
    assert nav.gps_offset_active
    assert nav.gps_dlat == pytest.approx(0.001, abs=1e-6)
    assert nav.gps_dlon == pytest.approx(0.002, abs=1e-6)

    # The current fix is corrected immediately on the next fix and shifted by Δ.
    nav.handle_sentence(nmea.encode_rmc(GeoPoint(59.0, 18.0), sog_knots=1, cog_deg=0))
    assert state.position.lat == pytest.approx(59.001, abs=1e-5)
    assert state.position.lon == pytest.approx(18.002, abs=1e-5)


def test_gps_offset_applies_to_subsequent_fixes_by_delta():
    state = NavigationState()
    nav = Navigator(state, bus=None)
    nav.handle_sentence(nmea.encode_rmc(GeoPoint(59.0, 18.0), sog_knots=1, cog_deg=0))
    nav.set_gps_offset(59.00005, 18.00005)  # Δ = (5e-5, 5e-5)

    # A new raw fix a small step away is shifted by the same delta.
    nav.handle_sentence(nmea.encode_rmc(GeoPoint(59.0001, 18.0001), sog_knots=1, cog_deg=0))
    assert state.position.lat == pytest.approx(59.0001 + 5e-5, abs=1e-6)
    assert state.position.lon == pytest.approx(18.0001 + 5e-5, abs=1e-6)


def test_gps_offset_clear_restores_raw():
    state = NavigationState()
    nav = Navigator(state, bus=None)
    nav.handle_sentence(nmea.encode_rmc(GeoPoint(59.0, 18.0), sog_knots=1, cog_deg=0))
    nav.set_gps_offset(59.001, 18.001)
    nav.clear_gps_offset()
    assert not nav.gps_offset_active
    nav.handle_sentence(nmea.encode_rmc(GeoPoint(59.0, 18.0), sog_knots=1, cog_deg=0))
    assert state.position.lat == pytest.approx(59.0, abs=1e-5)
    assert state.position.lon == pytest.approx(18.0, abs=1e-5)


def test_gps_offset_telemetry_and_commands(client):
    client.post(
        "/api/command",
        json={"type": "inject_nmea", "sentence": nmea.encode_rmc(GeoPoint(59.0, 18.0), 1, 0)},
    )
    # Give the bus a tick to apply the injected fix.
    for _ in range(5):
        client.get("/api/state")
    client.post(
        "/api/command", json={"type": "set_gps_offset", "true_lat": 59.0, "true_lon": 18.0}
    )
    off = client.get("/api/state").json()["gps_offset"]
    assert set(off) == {"dlat", "dlon", "active"}

    client.post("/api/command", json={"type": "clear_gps_offset"})
    off = client.get("/api/state").json()["gps_offset"]
    assert off["active"] is False
    assert off["dlat"] == 0.0 and off["dlon"] == 0.0


# --------------------------------------------------------------------------- #
# #49 throttle % override
# --------------------------------------------------------------------------- #
def test_throttle_override_tracks_percent_for_heading_hold():
    ctrl = _controller_at(GeoPoint(59.0, 18.0), heading=90.0)
    # Heading-hold with a built-in throttle of 0.4.
    ctrl.modes[ControlModeName.HEADING_HOLD].throttle = 0.4
    ctrl.handle_command({"type": "heading_hold", "heading": 90.0})

    ctrl.handle_command({"type": "set_throttle", "percent": 75})
    assert ctrl.throttle_override == pytest.approx(0.75)
    # Let the safety thrust-slew settle to the commanded magnitude.
    cmd = _settle(ctrl)
    # On-heading => steering small; thrust magnitude tracks the override.
    assert abs(cmd.thrust) == pytest.approx(0.75, abs=0.02)


def test_throttle_override_cleared_restores_default():
    ctrl = _controller_at(GeoPoint(59.0, 18.0), heading=90.0)
    ctrl.modes[ControlModeName.HEADING_HOLD].throttle = 0.4
    ctrl.handle_command({"type": "heading_hold", "heading": 90.0})
    ctrl.handle_command({"type": "set_throttle", "percent": 75})
    _settle(ctrl)
    ctrl.handle_command({"type": "set_throttle", "percent": None})
    assert ctrl.throttle_override is None
    cmd = _settle(ctrl)
    assert abs(cmd.thrust) == pytest.approx(0.4, abs=0.02)


def test_throttle_override_zero_clears():
    ctrl = _controller_at(GeoPoint(59.0, 18.0))
    ctrl.handle_command({"type": "set_throttle", "percent": 50})
    assert ctrl.throttle_override == pytest.approx(0.5)
    ctrl.handle_command({"type": "set_throttle", "percent": 0})
    assert ctrl.throttle_override is None


def test_throttle_override_does_not_affect_manual():
    ctrl = _controller_at(GeoPoint(59.0, 18.0))
    ctrl.handle_command({"type": "set_throttle", "percent": 80})
    ctrl.handle_command({"type": "manual", "thrust": 0.3, "steering": 0.0})
    # Settle past the safety thrust-slew; manual must reach exactly 0.3, NOT 0.8.
    cmd = _settle(ctrl)
    assert cmd.thrust == pytest.approx(0.3, abs=1e-6)


def test_throttle_override_telemetry(client):
    client.post("/api/command", json={"type": "set_throttle", "percent": 60})
    t = client.get("/api/state").json()["throttle_override"]
    assert t["active"] is True
    assert t["percent"] == pytest.approx(60.0)
    client.post("/api/command", json={"type": "set_throttle", "percent": None})
    t = client.get("/api/state").json()["throttle_override"]
    assert t["active"] is False


# --------------------------------------------------------------------------- #
# #50 pause / resume / stop
# --------------------------------------------------------------------------- #
def _waypoint_controller() -> Controller:
    ctrl = _controller_at(GeoPoint(59.0, 18.0))
    ctrl.handle_command(
        {
            "type": "goto",
            "waypoints": [
                {"lat": 59.01, "lon": 18.0},
                {"lat": 59.02, "lon": 18.0},
            ],
        }
    )
    ctrl.state.active_waypoint = 1  # pretend we advanced a leg
    return ctrl


def test_pause_waypoint_stores_and_switches_to_anchor_hold():
    ctrl = _waypoint_controller()
    ctrl.handle_command({"type": "pause_nav"})
    assert ctrl.state.mode == ControlModeName.ANCHOR_HOLD
    assert ctrl.suspended is not None
    assert ctrl.suspended["mode"] == ControlModeName.WAYPOINT
    assert ctrl.suspended["active_waypoint"] == 1
    assert len(ctrl.suspended["waypoints"]) == 2
    # Holds at the current position.
    assert ctrl.state.anchor == GeoPoint(59.0, 18.0)


def test_resume_restores_waypoint_mode_and_params():
    ctrl = _waypoint_controller()
    wps_before = list(ctrl.state.waypoints)
    ctrl.handle_command({"type": "pause_nav"})
    ctrl.handle_command({"type": "resume_nav"})
    assert ctrl.state.mode == ControlModeName.WAYPOINT
    assert ctrl.suspended is None
    assert ctrl.state.active_waypoint == 1
    assert [w.point for w in ctrl.state.waypoints] == [w.point for w in wps_before]


def test_resume_with_nothing_suspended_is_noop():
    ctrl = _controller_at(GeoPoint(59.0, 18.0))
    ctrl.handle_command({"type": "manual", "thrust": 0.0, "steering": 0.0})
    ctrl.handle_command({"type": "resume_nav"})
    assert ctrl.state.mode == ControlModeName.MANUAL
    assert ctrl.suspended is None


def test_stop_clears_suspended():
    ctrl = _waypoint_controller()
    ctrl.handle_command({"type": "pause_nav"})
    assert ctrl.suspended is not None
    ctrl.handle_command({"type": "stop"})
    assert ctrl.suspended is None
    assert ctrl.state.mode == ControlModeName.MANUAL


def test_pause_preserves_throttle_override():
    ctrl = _waypoint_controller()
    ctrl.handle_command({"type": "set_throttle", "percent": 55})
    ctrl.handle_command({"type": "pause_nav"})
    # Anchor hold should not depend on the override; resume restores it.
    ctrl.handle_command({"type": "resume_nav"})
    assert ctrl.throttle_override == pytest.approx(0.55)


def test_nav_telemetry(client):
    client.post(
        "/api/command",
        json={"type": "goto", "waypoints": [{"lat": 59.4, "lon": 18.1}]},
    )
    client.post("/api/command", json={"type": "pause_nav"})
    nav = client.get("/api/state").json()["nav"]
    assert nav["paused"] is True
    assert nav["suspended_mode"] == "waypoint"
    client.post("/api/command", json={"type": "resume_nav"})
    nav = client.get("/api/state").json()["nav"]
    assert nav["paused"] is False
    assert nav["suspended_mode"] is None


# --------------------------------------------------------------------------- #
# #54 cancellable route planning
# --------------------------------------------------------------------------- #
def test_plan_route_cancelled_returns_cancelled_result():
    poly = water.load_geojson(FIXTURE)
    from vanchor.core.config import SimConfig

    cfg = SimConfig()
    result = routing.plan_route(
        start_lat=cfg.start_lat,
        start_lon=cfg.start_lon,
        dest_lat=59.66430488913581,
        dest_lon=13.368675408442506,
        water_ll=poly,
        mode="fastest",
        cancelled=lambda: True,
    )
    assert result.ok is False
    assert result.waypoints == []
    assert result.message == "Route planning cancelled."


def test_runtime_cancel_flag_makes_plan_cancel():
    rt = Runtime()
    rt._route_plan_cancelled = False
    rt.cancel_route_plan()
    assert rt._route_plan_cancelled is True
    # plan_route resets the flag at the start, so to observe cancellation we set
    # it via a monkeypatched plan that re-asserts after reset; simplest: call the
    # planner path directly through routing with the runtime predicate.
    poly = water.load_geojson(FIXTURE)
    from vanchor.core.config import SimConfig

    cfg = SimConfig()
    rt._route_plan_cancelled = True
    result = routing.plan_route(
        start_lat=cfg.start_lat,
        start_lon=cfg.start_lon,
        dest_lat=59.66430488913581,
        dest_lon=13.368675408442506,
        water_ll=poly,
        cancelled=lambda: rt._route_plan_cancelled,
    )
    assert result.ok is False and "cancelled" in result.message.lower()


def test_route_plan_cancel_endpoint(client):
    r = client.post("/api/route/plan/cancel")
    assert r.json() == {"cancelled": True}
