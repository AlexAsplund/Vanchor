"""Tests for Tier-2 features: Drift mode and go-to with on-arrival action."""

import statistics

import pytest

from vanchor.analysis.runner import Command, Scenario, run_scenario
from vanchor.controller.controller import Controller
from vanchor.core.geo import angle_difference, destination_point
from vanchor.core.models import ControlModeName, Environment, GeoPoint, GpsFix, Waypoint
from vanchor.core.state import NavigationState
from vanchor.sim.devices import SimMotorController

START = GeoPoint(59.66275, 13.32247)


def _ctrl():
    state = NavigationState()
    state.fix = GpsFix(point=START)
    return Controller(state, SimMotorController()), state


# --- Drift mode ----------------------------------------------------------- #
def test_drift_command_sets_mode_and_target():
    ctrl, state = _ctrl()
    ctrl.handle_command({"type": "drift", "heading": 120.0, "knots": 0.8})
    assert state.mode == ControlModeName.DRIFT
    assert state.target_heading == 120.0
    assert state.drift_target_knots == 0.8


def test_drift_holds_low_speed_under_wind():
    sc = Scenario(
        name="drift",
        start=START,
        model="fossen",
        duration_s=80.0,
        environment=Environment(wind_speed=5.0, wind_dir=90.0),
        commands=[Command(2.0, {"type": "drift", "heading": 90.0, "knots": 0.5})],
    )
    log = run_scenario(sc)
    tail = [s for s in log.samples if s.t > 60]
    sog = statistics.mean(s.sog_knots for s in tail)
    hdg_err = statistics.mean(abs(angle_difference(s.truth_heading, 90.0)) for s in tail)
    assert sog == pytest.approx(0.5, abs=0.15)  # holds the low (ground) drift speed
    assert hdg_err < 15.0  # roughly holds heading under a strong beam wind


# --- Go-to with on-arrival action ---------------------------------------- #
def test_waypoint_sets_route_complete_at_end():
    from vanchor.controller.modes import WaypointMode

    state = NavigationState()
    state.fix = GpsFix(point=START)
    wp = destination_point(START, 2.0, 0.0)  # within arrival radius
    state.waypoints = [Waypoint("A", wp)]
    mode = WaypointMode()
    mode.activate(state)
    assert state.route_complete is False
    mode.update(state, 0.2)  # arrives immediately
    assert state.route_complete is True


def test_goto_on_arrival_anchor():
    wp = destination_point(START, 25.0, 45.0)
    sc = Scenario(
        name="goto_anchor",
        start=START,
        model="fossen",
        duration_s=120.0,
        commands=[
            Command(
                2.0,
                {
                    "type": "goto",
                    "throttle": 0.8,
                    "on_arrival": "anchor",
                    "waypoints": [{"name": "A", "lat": wp.lat, "lon": wp.lon}],
                },
            )
        ],
    )
    log = run_scenario(sc)
    # The boat reaches the mark, then auto-engages anchor hold.
    assert log.samples[-1].mode == "anchor_hold"


def test_goto_on_arrival_stop():
    wp = destination_point(START, 25.0, 45.0)
    sc = Scenario(
        name="goto_stop",
        start=START,
        model="fossen",
        duration_s=120.0,
        commands=[
            Command(
                2.0,
                {
                    "type": "goto",
                    "throttle": 0.8,
                    "on_arrival": "stop",
                    "waypoints": [{"name": "A", "lat": wp.lat, "lon": wp.lon}],
                },
            )
        ],
    )
    log = run_scenario(sc)
    assert log.samples[-1].mode == "manual"  # stop -> manual idle
