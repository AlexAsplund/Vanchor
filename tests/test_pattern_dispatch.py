"""Command-dispatch tests for the three guided pattern modes (#57/#58/#59):
contour_follow, orbit, trolling. Confirms each command sets the right mode,
records its parameters on the state, and routes ``speed_knots`` into the shared
Cruise Control (SOG) hold while ``null`` leaves the mode's default thrust.
"""

from vanchor.controller.controller import Controller
from vanchor.core.models import ControlModeName, GeoPoint, GpsFix
from vanchor.core.state import NavigationState
from vanchor.sim.devices import SimMotorController

HERE = GeoPoint(59.3293, 18.0686)


def _ctrl(heading=0.0):
    state = NavigationState()
    state.fix = GpsFix(point=HERE)
    state.heading_deg = heading
    return Controller(state, SimMotorController()), state


def test_contour_follow_command():
    ctrl, state = _ctrl()
    ctrl.handle_command(
        {
            "type": "contour_follow",
            "target_depth_m": 6.5,
            "side": "shallow",
            "speed_knots": 1.2,
        }
    )
    assert state.mode == ControlModeName.CONTOUR_FOLLOW
    assert state.contour_target_depth_m == 6.5
    assert state.contour_side == "shallow"
    assert ctrl.cruise_knots == 1.2  # speed routed to cruise


def test_contour_follow_null_speed_uses_default_thrust():
    ctrl, state = _ctrl()
    ctrl.handle_command(
        {"type": "contour_follow", "target_depth_m": 6.5, "side": "deep",
         "speed_knots": None}
    )
    assert state.mode == ControlModeName.CONTOUR_FOLLOW
    assert ctrl.cruise_knots is None  # no cruise => mode default thrust applies
    # And the mode produces forward thrust on a tick.
    state.depth_m = 5.0
    cmd = ctrl.control_tick(0.2)
    assert cmd.thrust > 0.0


def test_orbit_command():
    ctrl, state = _ctrl()
    ctrl.handle_command(
        {
            "type": "orbit",
            "center_lat": HERE.lat,
            "center_lon": HERE.lon,
            "radius_m": 25.0,
            "direction": "ccw",
            "speed_knots": 2.0,
        }
    )
    assert state.mode == ControlModeName.ORBIT
    assert state.orbit_center == HERE
    assert state.orbit_radius_m == 25.0
    assert state.orbit_direction == "ccw"
    assert ctrl.cruise_knots == 2.0


def test_trolling_command_defaults_base_to_heading():
    ctrl, state = _ctrl(heading=210.0)
    ctrl.handle_command(
        {
            "type": "trolling",
            "base_heading": None,
            "amplitude_deg": 18.0,
            "period_s": 14.0,
            "speed_knots": None,
        }
    )
    assert state.mode == ControlModeName.TROLLING
    assert state.trolling_base_heading == 210.0  # defaulted to current heading
    assert state.trolling_amplitude_deg == 18.0
    assert state.trolling_period_s == 14.0
    assert ctrl.cruise_knots is None


def test_trolling_explicit_base_heading():
    ctrl, state = _ctrl(heading=10.0)
    ctrl.handle_command(
        {"type": "trolling", "base_heading": 300.0, "amplitude_deg": 20.0,
         "period_s": 20.0, "speed_knots": 1.5}
    )
    assert state.trolling_base_heading == 300.0
    assert ctrl.cruise_knots == 1.5


def test_pattern_modes_are_cruisable():
    from vanchor.controller.controller import _CRUISING_MODES

    assert ControlModeName.CONTOUR_FOLLOW in _CRUISING_MODES
    assert ControlModeName.ORBIT in _CRUISING_MODES
    assert ControlModeName.TROLLING in _CRUISING_MODES


def test_goto_sets_route_loop_flag():
    ctrl, state = _ctrl()
    wps = [{"lat": 59.33, "lon": 18.07}, {"lat": 59.34, "lon": 18.08}]
    ctrl.handle_command({"type": "goto", "waypoints": wps, "loop": True})
    assert state.mode == ControlModeName.WAYPOINT
    assert state.route_loop is True
    # A plain goto leaves looping off.
    ctrl.handle_command({"type": "goto", "waypoints": wps})
    assert state.route_loop is False


def test_load_route_sets_route_loop_flag():
    ctrl, state = _ctrl()
    from vanchor.core.models import Waypoint

    state.waypoints = [Waypoint("WP1", GeoPoint(59.33, 18.07))]
    ctrl.handle_command({"type": "load_route", "loop": True})
    assert state.mode == ControlModeName.WAYPOINT
    assert state.route_loop is True
