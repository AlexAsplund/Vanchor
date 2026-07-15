"""Per-waypoint speed attributes (engine % / boat knots).

A waypoint may carry ``throttle_pct`` (engine power %) or ``speed_kn`` (SOG
target). ARRIVING at that waypoint adopts the speed as the new default for the
legs that follow, by routing it into the same channels manual speed commands
use (throttle override / Cruise Control) -- so a manual speed set mid-route
overrides it, and the next speed-carrying waypoint overrides that in turn.
"""

import pytest

from vanchor.controller.controller import Controller, _wp_speed
from vanchor.core.models import ControlModeName, GeoPoint, GpsFix
from vanchor.core.state import NavigationState
from vanchor.core.geo import destination_point
from vanchor.sim.devices import SimMotorController

START = GeoPoint(59.0, 18.0)


def _controller_at(point: GeoPoint, heading: float = 0.0) -> Controller:
    state = NavigationState()
    state.fix = GpsFix(point=point)
    state.heading_deg = heading
    return Controller(state, SimMotorController(), bus=None)


def _tick(ctrl: Controller, n: int = 1, dt: float = 0.2):
    for _ in range(n):
        ctrl.state.fix_seq += 1  # keep the fix "fresh" for the governor
        ctrl.control_tick(dt)


def _wp(point: GeoPoint, **extra) -> dict:
    return {"name": "WP", "lat": point.lat, "lon": point.lon, **extra}


def _route(ctrl: Controller, wps: list[dict]) -> None:
    ctrl.handle_command({"type": "goto", "waypoints": wps, "throttle": 0.6})


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_goto_parses_and_telemetry_carries_speed_fields():
    ctrl = _controller_at(START)
    far = destination_point(START, 100.0, 0.0)
    _route(ctrl, [
        _wp(far, throttle_pct=40),
        _wp(far, speed_kn=2.5),
        _wp(far),
    ])
    wps = ctrl.state.waypoints
    assert wps[0].throttle_pct == 40.0 and wps[0].speed_kn is None
    assert wps[1].speed_kn == 2.5 and wps[1].throttle_pct is None
    assert wps[2].throttle_pct is None and wps[2].speed_kn is None
    tele = ctrl.state.to_dict()["waypoints"]
    assert tele[0]["throttle_pct"] == 40.0
    assert tele[1]["speed_kn"] == 2.5
    assert tele[2]["throttle_pct"] is None and tele[2]["speed_kn"] is None


@pytest.mark.parametrize("raw, want", [
    ({"throttle_pct": 40}, (40.0, None)),
    ({"speed_kn": 2.5}, (None, 2.5)),
    ({"throttle_pct": 150}, (100.0, None)),          # clamped
    ({"throttle_pct": 30, "speed_kn": 2.0}, (30.0, None)),  # % wins
    ({"throttle_pct": 0}, (None, None)),             # non-positive -> none
    ({"speed_kn": -1}, (None, None)),
    ({"throttle_pct": "junk"}, (None, None)),        # malformed -> none
    ({}, (None, None)),
])
def test_wp_speed_parsing(raw, want):
    assert _wp_speed(raw) == want


# --------------------------------------------------------------------------- #
# Arrival semantics
# --------------------------------------------------------------------------- #
def test_arrival_adopts_throttle_pct_for_following_legs():
    ctrl = _controller_at(START)
    far = destination_point(START, 100.0, 0.0)
    # The boat starts ON WP1 -> it "arrives" on the first tick.
    _route(ctrl, [_wp(START, throttle_pct=40), _wp(far)])
    assert ctrl.throttle_override is None
    _tick(ctrl)
    assert ctrl.state.active_waypoint == 1
    assert ctrl.throttle_override == pytest.approx(0.40)
    assert ctrl.cruise_knots is None


def test_arrival_adopts_speed_kn_via_cruise_and_clears_pct():
    ctrl = _controller_at(START)
    far = destination_point(START, 100.0, 0.0)
    ctrl.handle_command({"type": "set_throttle", "percent": 80})
    _route(ctrl, [_wp(START, speed_kn=2.5), _wp(far)])
    _tick(ctrl)
    assert ctrl.cruise_knots == pytest.approx(2.5)
    assert ctrl.throttle_override is None  # knots hold replaced the % override


def test_waypoint_without_speed_keeps_the_previous_one():
    ctrl = _controller_at(START)
    mid = destination_point(START, 100.0, 0.0)
    far = destination_point(START, 200.0, 0.0)
    _route(ctrl, [_wp(START, throttle_pct=40), _wp(mid), _wp(far)])
    _tick(ctrl)
    assert ctrl.throttle_override == pytest.approx(0.40)
    # Arrive at WP2 (no speed attribute): the adopted 40% keeps applying.
    ctrl.state.fix = GpsFix(point=mid)
    _tick(ctrl)
    assert ctrl.state.active_waypoint == 2
    assert ctrl.throttle_override == pytest.approx(0.40)


def test_manual_speed_overrides_until_next_speed_waypoint():
    ctrl = _controller_at(START)
    mid = destination_point(START, 100.0, 0.0)
    far = destination_point(START, 200.0, 0.0)
    _route(ctrl, [_wp(START, throttle_pct=40), _wp(mid, throttle_pct=70), _wp(far)])
    _tick(ctrl)
    assert ctrl.throttle_override == pytest.approx(0.40)
    # Manual change mid-leg wins...
    ctrl.handle_command({"type": "set_throttle", "percent": 55})
    _tick(ctrl)
    assert ctrl.throttle_override == pytest.approx(0.55)
    # ...until the boat arrives at the next waypoint that carries a speed.
    ctrl.state.fix = GpsFix(point=mid)
    _tick(ctrl)
    assert ctrl.throttle_override == pytest.approx(0.70)


def test_multi_advance_applies_the_last_arrived_speed():
    ctrl = _controller_at(START)
    near = destination_point(START, 1.0, 90.0)   # inside the arrival radius
    far = destination_point(START, 200.0, 0.0)
    _route(ctrl, [_wp(START, throttle_pct=30), _wp(near, throttle_pct=90), _wp(far)])
    _tick(ctrl)
    # Both stacked marks consumed in one tick -> the LAST one's speed applies.
    assert ctrl.state.active_waypoint == 2
    assert ctrl.throttle_override == pytest.approx(0.90)


def test_speed_waypoints_survive_pause_resume():
    ctrl = _controller_at(START)
    far = destination_point(START, 100.0, 0.0)
    _route(ctrl, [_wp(far, throttle_pct=40)])
    ctrl.handle_command({"type": "pause_nav"})
    assert ctrl.state.mode == ControlModeName.ANCHOR_HOLD
    ctrl.handle_command({"type": "resume_nav"})
    assert ctrl.state.mode == ControlModeName.WAYPOINT
    assert ctrl.state.waypoints[0].throttle_pct == 40.0
