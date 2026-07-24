"""Trace-tightly routes (hand-drawn "paint" routes and any exact-shape route).

The ``trace_tight`` goto/load_route flag makes WaypointMode use a much smaller
arrival radius so the boat HUGS each corner instead of turning early and cutting
inside it. Two things must hold:

  1. the flag is plumbed onto the state (fresh goto, load_route, pause/resume);
  2. it demonstrably hugs a corner closer than the normal radius, while STILL
     completing the route (the abeam perpendicular-pass gate keeps the tight
     radius from stalling the boat when GPS noise/drift holds it just outside).
"""

from __future__ import annotations

import math

from vanchor.controller.controller import Controller
from vanchor.core.geo import haversine_m
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

def test_goto_sets_and_defaults_trace_tight():
    ctl, state = _ctl()
    ctl.handle_command({"type": "goto", "waypoints": _wps()})
    assert state.route_trace_tight is False          # default off
    ctl.handle_command({"type": "goto", "waypoints": _wps(), "trace_tight": True})
    assert state.route_trace_tight is True


def test_load_route_sets_trace_tight():
    ctl, state = _ctl()
    state.waypoints = []  # load_route expects waypoints already placed; empty is fine here
    ctl.handle_command({"type": "load_route", "trace_tight": True})
    assert state.route_trace_tight is True


def test_pause_resume_preserves_trace_tight():
    ctl, state = _ctl()
    ctl.handle_command({"type": "goto", "waypoints": _wps(), "trace_tight": True})
    ctl.handle_command({"type": "pause_nav"})
    assert state.mode == ControlModeName.ANCHOR_HOLD
    ctl.handle_command({"type": "resume_nav"})
    assert state.mode == ControlModeName.WAYPOINT
    assert state.route_trace_tight is True


# --- closed-loop corner behaviour ------------------------------------------ #

_LAT, _LON = 59.3293, 18.0686
_MLAT = 111320.0
_MLON = 111320.0 * math.cos(math.radians(_LAT))


def _off(base: GeoPoint, east_m: float, north_m: float) -> GeoPoint:
    return GeoPoint(base.lat + north_m / _MLAT, base.lon + east_m / _MLON)


def _run_corner(tight: bool):
    """Drive a straight leg into a 90° corner; return (closest approach to the
    corner apex, route_complete)."""
    a = GeoPoint(_LAT, _LON)
    b = _off(a, 120.0, 0.0)       # 120 m east — the corner apex
    c = _off(a, 120.0, 120.0)     # then 120 m north — a 90° left turn
    h = Harness(start=a, model="fossen")
    h.sim.truth().heading_deg = 90.0   # pointed straight down the first leg
    cmd = {
        "type": "goto",
        "waypoints": [
            {"name": "B", "lat": b.lat, "lon": b.lon},
            {"name": "C", "lat": c.lat, "lon": c.lon},
        ],
        "throttle": 0.6,
    }
    if tight:
        cmd["trace_tight"] = True
    h.command(cmd)

    dt = h.physics_dt
    next_gps = next_compass = next_ctrl = 0.0
    min_to_apex = 1e9
    t = 0.0
    while t < 360.0:
        h.sim.step(dt)
        if t >= next_gps:
            h.nav.handle_sentence(h.gps.sample(h.sim.truth())); next_gps += 1.0 / h.gps_hz
        if t >= next_compass:
            h.nav.handle_sentence(h.compass.sample(h.sim.truth())); next_compass += 1.0 / h.compass_hz
        if t >= next_ctrl:
            h.controller.control_tick(1.0 / h.control_hz); next_ctrl += 1.0 / h.control_hz
        min_to_apex = min(min_to_apex, haversine_m(h.sim.truth().point, b))
        t += dt
    return min_to_apex, h.state.route_complete


def test_trace_tight_hugs_corner_and_completes():
    normal, normal_done = _run_corner(tight=False)
    tight, tight_done = _run_corner(tight=True)
    # Both must finish — a tight radius must not wedge the route.
    assert normal_done and tight_done
    # Tight tracing passes closer to the apex (hugs the corner instead of cutting).
    assert tight < normal
    assert tight < 0.5          # essentially reaches the corner
