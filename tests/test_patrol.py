"""Patrol mode: WaypointMode runs the route there-and-back, vs loop / complete."""

from vanchor.controller.modes import WaypointMode
from vanchor.core.models import GeoPoint, GpsFix, Waypoint
from vanchor.core.state import NavigationState


def _sequence(patrol=False, loop=False, n=10):
    """Drive a 3-waypoint route, parking the boat on the active mark each tick so
    it 'arrives' every step, and return the active-waypoint sequence ('DONE' once
    the route completes)."""
    m = WaypointMode()
    st = NavigationState()
    st.waypoints = [Waypoint(name=f"W{i}", point=GeoPoint(59.0 + i * 0.001, 18.0)) for i in range(3)]
    st.route_patrol = patrol
    st.route_loop = loop
    st.active_waypoint = 0
    m.activate(st)
    seq = []
    for _ in range(n):
        st.fix = GpsFix(point=st.waypoints[st.active_waypoint].point)  # sit on the mark -> arrived
        m.update(st, 0.2)
        seq.append("DONE" if st.route_complete else st.active_waypoint)
        if st.route_complete:
            break
    return seq


def test_normal_route_completes_at_the_end():
    assert _sequence() == [1, 2, "DONE"]


def test_loop_route_wraps_to_start():
    assert _sequence(loop=True, n=7) == [1, 2, 0, 1, 2, 0, 1]


def test_patrol_route_runs_there_and_back():
    # 0 ->1 ->2 (end) -> bounce back ->1 ->0 (start) -> bounce forward ->1 ->2 ...
    assert _sequence(patrol=True, n=8) == [1, 2, 1, 0, 1, 2, 1, 0]


def test_patrol_needs_two_waypoints():
    # A single-waypoint "patrol" has nowhere to bounce -> it just completes.
    m = WaypointMode()
    st = NavigationState()
    st.waypoints = [Waypoint(name="W0", point=GeoPoint(59.0, 18.0))]
    st.route_patrol = True
    st.active_waypoint = 0
    m.activate(st)
    st.fix = GpsFix(point=st.waypoints[0].point)
    m.update(st, 0.2)
    assert st.route_complete is True
