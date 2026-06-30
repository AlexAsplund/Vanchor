"""Editing an ACTIVE route (drag/insert/delete/reorder a committed waypoint) must
not restart navigation from the first waypoint. The frontend re-sends the route
as a ``goto`` carrying an ``active`` resume index; the handler must preserve
progress (and the route's loop/patrol/on_arrival flags) on a live edit, while a
fresh ``goto`` (no ``active``) still starts at waypoint 0.
"""

from vanchor.controller.controller import Controller
from vanchor.core.models import ControlModeName
from vanchor.core.state import NavigationState
from vanchor.sim.devices import SimMotorController


def _ctl():
    state = NavigationState()
    return Controller(state, SimMotorController()), state


def _wps(n=3):
    return [{"name": f"W{i}", "lat": 59.0 + i * 0.001, "lon": 18.0} for i in range(n)]


def test_fresh_goto_starts_at_first_waypoint():
    ctl, state = _ctl()
    ctl.handle_command({"type": "goto", "waypoints": _wps()})
    assert state.mode == ControlModeName.WAYPOINT
    assert state.active_waypoint == 0


def test_live_edit_preserves_active_waypoint():
    ctl, state = _ctl()
    ctl.handle_command({"type": "goto", "waypoints": _wps()})
    state.active_waypoint = 2  # boat has progressed to the 3rd mark
    # user drags/reorders a committed waypoint -> live edit re-sends the route
    # with the (possibly remapped) resume index.
    ctl.handle_command({"type": "goto", "waypoints": _wps(), "active": 2})
    assert state.active_waypoint == 2  # NOT reset to 0
    assert state.mode == ControlModeName.WAYPOINT


def test_live_edit_clamps_resume_index():
    ctl, state = _ctl()
    ctl.handle_command({"type": "goto", "waypoints": _wps(3)})
    state.active_waypoint = 2
    # a delete shrank the route to 2 waypoints; the resume index clamps into range
    ctl.handle_command({"type": "goto", "waypoints": _wps(2), "active": 2})
    assert state.active_waypoint == 1  # clamped to len-1


def test_live_edit_preserves_loop_and_patrol_flags():
    ctl, state = _ctl()
    ctl.handle_command({"type": "goto", "waypoints": _wps(), "loop": True})
    assert state.route_loop is True
    state.active_waypoint = 1
    # a live edit that doesn't re-send loop/patrol must not silently drop them
    ctl.handle_command({"type": "goto", "waypoints": _wps(), "active": 1})
    assert state.route_loop is True
    assert state.active_waypoint == 1
