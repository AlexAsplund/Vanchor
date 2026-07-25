"""PathTrackMode -- the pure-pursuit path follower (#35), the opt-in alternative
to WaypointMode enabled via the goto ``follow:"path"`` flag.

Checks: the flag routes to PATH_TRACK (and default stays WAYPOINT); it survives
pause/resume; and in the Fossen sim it tracks a route, completes, hugs a corner
at least as tightly as leg-tracking, and honours loop / patrol.
"""

from __future__ import annotations

import math

from vanchor.controller.controller import Controller
from vanchor.core.geo import cross_track, haversine_m
from vanchor.core.models import ControlModeName, GeoPoint
from vanchor.core.state import NavigationState
from vanchor.sim.devices import SimMotorController

from harness import Harness


def _ctl():
    state = NavigationState()
    return Controller(state, SimMotorController()), state


def _wps(n=3):
    return [{"name": f"W{i}", "lat": 59.0 + i * 0.001, "lon": 18.0} for i in range(n)]


# --- command plumbing ------------------------------------------------------ #

def test_goto_follow_flag_selects_mode():
    ctl, state = _ctl()
    ctl.handle_command({"type": "goto", "waypoints": _wps()})
    assert state.mode == ControlModeName.WAYPOINT      # default = leg-based
    assert state.route_follow == "leg"
    ctl.handle_command({"type": "goto", "waypoints": _wps(), "follow": "path"})
    assert state.mode == ControlModeName.PATH_TRACK    # switch = pure pursuit
    assert state.route_follow == "path"


def test_pause_resume_preserves_path_follow():
    ctl, state = _ctl()
    ctl.handle_command({"type": "goto", "waypoints": _wps(), "follow": "path"})
    ctl.handle_command({"type": "pause_nav"})
    assert state.mode == ControlModeName.ANCHOR_HOLD
    ctl.handle_command({"type": "resume_nav"})
    assert state.mode == ControlModeName.PATH_TRACK
    assert state.route_follow == "path"


def test_live_edit_stays_in_path_mode():
    ctl, state = _ctl()
    ctl.handle_command({"type": "goto", "waypoints": _wps(), "follow": "path"})
    # a live edit (resume index present) must not fall back to WaypointMode
    ctl.handle_command({"type": "goto", "waypoints": _wps(), "follow": "path", "active": 1})
    assert state.mode == ControlModeName.PATH_TRACK


# --- closed-loop behaviour ------------------------------------------------- #

_LAT, _LON = 59.3293, 18.0686
_MLAT = 111320.0
_MLON = 111320.0 * math.cos(math.radians(_LAT))


def _off(base: GeoPoint, east_m: float, north_m: float) -> GeoPoint:
    return GeoPoint(base.lat + north_m / _MLAT, base.lon + east_m / _MLON)


def _run_corner(follow: str):
    """Straight leg into a 90° corner; return (closest approach to the apex,
    max cross-track off the drawn line, completed)."""
    a = GeoPoint(_LAT, _LON)
    pts = [a, _off(a, 120.0, 0.0), _off(a, 120.0, 120.0)]   # corner apex = pts[1]
    h = Harness(start=a, model="fossen")
    h.sim.truth().heading_deg = 90.0
    cmd = {
        "type": "goto", "throttle": 0.6,
        "waypoints": [{"name": f"W{i}", "lat": p.lat, "lon": p.lon} for i, p in enumerate(pts)],
    }
    if follow == "path":
        cmd["follow"] = "path"
    h.command(cmd)
    dt = h.physics_dt
    ng = nc = nt = 0.0
    t = 0.0
    apex_min = 1e9
    max_xte = 0.0
    while t < 360.0:
        h.sim.step(dt)
        if t >= ng:
            h.nav.handle_sentence(h.gps.sample(h.sim.truth())); ng += 1.0 / h.gps_hz
        if t >= nc:
            h.nav.handle_sentence(h.compass.sample(h.sim.truth())); nc += 1.0 / h.compass_hz
        if t >= nt:
            h.controller.control_tick(1.0 / h.control_hz); nt += 1.0 / h.control_hz
        pos = h.sim.truth().point
        apex_min = min(apex_min, haversine_m(pos, pts[1]))
        if not h.state.route_complete:
            near = min(abs(cross_track(pts[i], pts[i + 1], pos).distance_m) for i in range(len(pts) - 1))
            max_xte = max(max_xte, near)
        if h.state.route_complete:
            break
        t += dt
    return apex_min, max_xte, h.state.route_complete


def test_path_track_completes_and_hugs_corner():
    leg_apex, leg_xte, leg_done = _run_corner("leg")
    path_apex, path_xte, path_done = _run_corner("path")
    assert leg_done and path_done                       # both finish, no stall
    assert path_apex <= leg_apex + 0.05                 # hugs the corner at least as tightly
    assert path_apex < 1.5                              # and genuinely reaches it
    assert path_xte <= leg_xte + 0.5                    # cross-track no worse than leg-tracking


def _run(follow_wps, loop=False, patrol=False, secs=90.0):
    a = GeoPoint(_LAT, _LON)
    pts = [_off(a, e, n) for e, n in follow_wps]
    h = Harness(start=a, model="fossen")
    h.sim.truth().heading_deg = 90.0
    cmd = {
        "type": "goto", "throttle": 0.6, "follow": "path",
        "waypoints": [{"name": f"W{i}", "lat": p.lat, "lon": p.lon} for i, p in enumerate(pts)],
    }
    if loop:
        cmd["loop"] = True
    if patrol:
        cmd["patrol"] = True
    h.command(cmd)
    dt = h.physics_dt
    ng = nc = nt = 0.0
    t = 0.0
    while t < secs:
        h.sim.step(dt)
        if t >= ng:
            h.nav.handle_sentence(h.gps.sample(h.sim.truth())); ng += 1.0 / h.gps_hz
        if t >= nc:
            h.nav.handle_sentence(h.compass.sample(h.sim.truth())); nc += 1.0 / h.compass_hz
        if t >= nt:
            h.controller.control_tick(1.0 / h.control_hz); nt += 1.0 / h.control_hz
        t += dt
    return h.state


def test_path_track_loop_never_completes():
    st = _run([(0, 0), (80, 0), (80, 80), (0, 80)], loop=True)
    assert st.mode == ControlModeName.PATH_TRACK
    assert st.route_complete is False   # a loop circles continuously


def test_path_track_patrol_runs():
    st = _run([(0, 0), (150, 0)], patrol=True)
    assert st.mode == ControlModeName.PATH_TRACK
    assert st.route_complete is False   # patrol reverses at each end, never done
