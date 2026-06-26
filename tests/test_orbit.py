"""Circle / orbit mode (#58): orbit a centre at a fixed radius.

Deterministic closed-loop geometry: drive the boat by the mode's target heading
over a few simulated minutes and assert it (a) converges to ~radius from the
centre, (b) holds that radius, and (c) actually circles the centre (the bearing
from centre to boat sweeps through a full turn) in the requested direction.
"""

import math

from vanchor.controller.modes import OrbitMode
from vanchor.core.geo import (
    angle_difference,
    destination_point,
    haversine_m,
    initial_bearing,
    offset_meters,
)
from vanchor.core.models import GeoPoint, GpsFix, GuidedSetpoint
from vanchor.core.state import NavigationState

CENTER = GeoPoint(59.3293, 18.0686)


def _run(direction, start, radius=30.0, seconds=400.0, speed_mps=1.5):
    state = NavigationState()
    state.orbit_center = CENTER
    state.orbit_radius_m = radius
    state.orbit_direction = direction
    mode = OrbitMode()
    mode.activate(state)

    pos = start
    heading = 0.0
    dt = 0.2
    ranges = []
    bearings = []
    for _ in range(int(seconds / dt)):
        state.fix = GpsFix(point=pos)
        state.heading_deg = heading
        sp = mode.update(state, dt)
        assert isinstance(sp, GuidedSetpoint)
        ranges.append(haversine_m(CENTER, pos))
        bearings.append(initial_bearing(CENTER, pos))
        heading = sp.target_heading
        brg = math.radians(heading)
        pos = offset_meters(
            pos, speed_mps * dt * math.sin(brg), speed_mps * dt * math.cos(brg)
        )
    return ranges, bearings


def _swept(bearings):
    """Total signed bearing swept (deg) -- > +360 means a full cw loop."""
    total = 0.0
    for a, b in zip(bearings, bearings[1:]):
        total += angle_difference(a, b)
    return total


def test_idles_without_center():
    state = NavigationState()
    state.fix = GpsFix(point=CENTER)
    mode = OrbitMode()
    sp = mode.update(state, 0.2)
    assert isinstance(sp, GuidedSetpoint)
    assert sp.thrust == 0.0


def test_converges_to_radius_from_outside_cw():
    start = destination_point(CENTER, 60.0, 0.0)  # 60 m north, radius 30
    ranges, _ = _run("cw", start, radius=30.0)
    # Settle near the ring.
    settled = ranges[-200:]
    assert all(abs(r - 30.0) < 6.0 for r in settled)
    assert abs(sum(settled) / len(settled) - 30.0) < 3.0


def test_converges_to_radius_from_inside():
    start = destination_point(CENTER, 8.0, 90.0)  # 8 m east, radius 30
    ranges, _ = _run("ccw", start, radius=30.0)
    settled = ranges[-200:]
    assert all(abs(r - 30.0) < 6.0 for r in settled)


def test_circles_clockwise():
    start = destination_point(CENTER, 30.0, 0.0)
    _, bearings = _run("cw", start, radius=30.0)
    swept = _swept(bearings)
    assert swept > 360.0  # at least one full clockwise (increasing-bearing) loop


def test_circles_counterclockwise():
    start = destination_point(CENTER, 30.0, 0.0)
    _, bearings = _run("ccw", start, radius=30.0)
    swept = _swept(bearings)
    assert swept < -360.0  # at least one full ccw loop


def test_range_telemetry_set():
    start = destination_point(CENTER, 45.0, 120.0)
    state = NavigationState()
    state.orbit_center = CENTER
    state.orbit_radius_m = 30.0
    state.fix = GpsFix(point=start)
    mode = OrbitMode()
    mode.update(state, 0.2)
    assert mode.range_m == haversine_m(CENTER, start)
    assert state.distance_to_anchor_m == mode.range_m
