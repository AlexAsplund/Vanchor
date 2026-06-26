"""Tests for the Tier-1 features: Spot-Lock Jog, Cruise Control, track replay."""

import pytest

from vanchor.controller.controller import Controller
from vanchor.core.geo import haversine_m, initial_bearing
from vanchor.core.models import ControlModeName, GeoPoint, GpsFix
from vanchor.core.state import NavigationState
from vanchor.sim.devices import SimMotorController

HERE = GeoPoint(59.66275, 13.32247)


def _ctrl(heading=0.0, anchor=None, sog=0.0):
    state = NavigationState()
    state.fix = GpsFix(point=HERE, sog_knots=sog)
    state.heading_deg = heading
    state.sog_knots = sog
    if anchor is not None:
        state.anchor = anchor
    return Controller(state, SimMotorController()), state


# --- Spot-Lock Jog -------------------------------------------------------- #
@pytest.mark.parametrize(
    "heading,direction,expected_bearing",
    [
        (0.0, "forward", 0.0),
        (0.0, "back", 180.0),
        (0.0, "right", 90.0),
        (0.0, "left", 270.0),
        (90.0, "forward", 90.0),  # boat-relative: forward follows the heading
    ],
)
def test_jog_moves_anchor_boat_relative(heading, direction, expected_bearing):
    ctrl, state = _ctrl(heading=heading, anchor=HERE)
    ctrl.handle_command({"type": "jog", "direction": direction})
    assert haversine_m(HERE, state.anchor) == pytest.approx(1.5, abs=0.05)
    assert initial_bearing(HERE, state.anchor) == pytest.approx(expected_bearing, abs=1.0)


def test_jog_distance_override():
    ctrl, state = _ctrl(heading=0.0, anchor=HERE)
    ctrl.handle_command({"type": "jog", "direction": "forward", "distance_m": 3.0})
    assert haversine_m(HERE, state.anchor) == pytest.approx(3.0, abs=0.05)


def test_jog_ignored_without_anchor():
    ctrl, state = _ctrl()
    ctrl.handle_command({"type": "jog", "direction": "forward"})
    assert state.anchor is None


# --- Cruise Control ------------------------------------------------------- #
def test_cruise_enable_disable():
    ctrl, state = _ctrl()
    ctrl.handle_command({"type": "cruise", "knots": 2.0})
    assert ctrl.cruise_knots == 2.0
    assert ctrl.cruise_pid.setpoint == 2.0
    ctrl.handle_command({"type": "cruise", "knots": 0})
    assert ctrl.cruise_knots is None


def test_cruise_overrides_throttle_in_heading_hold():
    # Boat well below target SOG -> cruise should demand more than the fixed
    # throttle to speed up.
    ctrl, state = _ctrl(sog=0.0)
    ctrl.handle_command({"type": "heading_hold", "heading": 0.0, "throttle": 0.3})
    ctrl.handle_command({"type": "cruise", "knots": 2.5})
    # Ramp a few ticks (the safety slew limiter caps per-tick thrust change).
    for _ in range(10):
        cmd = ctrl.control_tick(0.2)
    assert cmd.thrust > 0.3


def test_cruise_not_applied_in_manual():
    ctrl, state = _ctrl(sog=0.0)
    ctrl.handle_command({"type": "cruise", "knots": 2.5})
    ctrl.handle_command({"type": "manual", "thrust": 0.1, "steering": 0.0})
    cmd = ctrl.control_tick(0.2)
    assert cmd.thrust == pytest.approx(0.1)  # cruise ignored in manual


# --- Track replay / BackTrack -------------------------------------------- #
def test_replay_and_backtrack_feed_waypoints():
    ctrl, state = _ctrl()
    from vanchor.core.geo import destination_point

    pts = [HERE, destination_point(HERE, 20.0, 90.0), destination_point(HERE, 40.0, 90.0)]
    ctrl.track.points = list(pts)

    ctrl.handle_command({"type": "replay"})
    assert state.mode == ControlModeName.WAYPOINT
    assert [w.point for w in state.waypoints] == pts

    ctrl.handle_command({"type": "backtrack"})
    assert [w.point for w in state.waypoints] == list(reversed(pts))


def test_record_command_toggles_recorder():
    ctrl, state = _ctrl()
    ctrl.handle_command({"type": "record", "action": "start"})
    assert ctrl.track.recording is True
    ctrl.handle_command({"type": "record", "action": "stop"})
    assert ctrl.track.recording is False


def test_control_tick_records_track_while_underway():
    # With recording on and the boat moving, the controller should breadcrumb.
    ctrl, state = _ctrl()
    ctrl.track.min_distance_m = 1.0
    ctrl.handle_command({"type": "record", "action": "start"})
    ctrl.control_tick(0.2)  # records the start point
    from vanchor.core.geo import destination_point

    state.fix = GpsFix(point=destination_point(HERE, 10.0, 0.0))
    ctrl.control_tick(0.2)
    assert len(ctrl.track.points) == 2
