"""Contour-follow mode (#57): steer to hold a depth contour (isobath).

These are deterministic, hardware-free tests. The first checks the steering
*direction* (which side it turns toward to reduce the depth error); the second
drives a closed loop over a synthetic, planar sloping bottom and asserts the
depth error shrinks as the boat curves onto the target isobath.
"""

import math

from vanchor.controller.modes import ContourFollowMode
from vanchor.core.geo import angle_difference, offset_meters
from vanchor.core.models import GeoPoint, GpsFix, GuidedSetpoint
from vanchor.core.state import NavigationState

HERE = GeoPoint(59.3293, 18.0686)


def _state(point, heading=0.0, depth=10.0):
    s = NavigationState()
    s.fix = GpsFix(point=point)
    s.heading_deg = heading
    s.depth_m = depth
    return s


def test_holds_heading_when_no_sounding():
    state = _state(HERE, heading=42.0, depth=0.0)  # 0 = no return
    state.contour_target_depth_m = 8.0
    mode = ContourFollowMode()
    mode.activate(state)
    sp = mode.update(state, 0.2)
    assert isinstance(sp, GuidedSetpoint)
    assert sp.target_heading == 42.0
    assert sp.thrust > 0.0


def test_too_deep_turns_toward_shallow_side():
    # deep water on starboard ("deep" side = +90). Too deep => seek shallower =>
    # turn to PORT (negative offset relative to heading).
    state = _state(HERE, heading=0.0, depth=12.0)
    state.contour_target_depth_m = 8.0
    state.contour_side = "deep"
    mode = ContourFollowMode()
    mode.activate(state)
    sp = mode.update(state, 0.2)
    off = angle_difference(state.heading_deg, sp.target_heading)
    assert off < 0.0  # turned toward the shallow (port) side


def test_too_shallow_turns_toward_deep_side():
    state = _state(HERE, heading=0.0, depth=5.0)
    state.contour_target_depth_m = 8.0
    state.contour_side = "deep"
    mode = ContourFollowMode()
    mode.activate(state)
    sp = mode.update(state, 0.2)
    off = angle_difference(state.heading_deg, sp.target_heading)
    assert off > 0.0  # turned toward the deep (starboard) side


def test_side_shallow_mirrors_direction():
    deep = _state(HERE, heading=0.0, depth=12.0)
    deep.contour_target_depth_m = 8.0
    deep.contour_side = "deep"
    m1 = ContourFollowMode()
    m1.activate(deep)
    off_deep = angle_difference(deep.heading_deg, m1.update(deep, 0.2).target_heading)

    shallow = _state(HERE, heading=0.0, depth=12.0)
    shallow.contour_target_depth_m = 8.0
    shallow.contour_side = "shallow"
    m2 = ContourFollowMode()
    m2.activate(shallow)
    off_shallow = angle_difference(
        shallow.heading_deg, m2.update(shallow, 0.2).target_heading
    )
    assert math.copysign(1, off_deep) != math.copysign(1, off_shallow)


def test_offset_is_capped_gentle():
    state = _state(HERE, heading=0.0, depth=100.0)  # huge error
    state.contour_target_depth_m = 8.0
    mode = ContourFollowMode()
    mode.activate(state)
    sp = mode.update(state, 0.2)
    off = abs(angle_difference(state.heading_deg, sp.target_heading))
    assert off <= mode.config.max_offset_deg + 1e-6


def test_converges_on_synthetic_sloping_bottom():
    """Planar bottom that deepens toward the east (north-south isobaths).

    The target isobath runs north-south. Starting too shallow (west of it) the
    boat should curve east into deeper water until the depth error is small.
    """

    def depth_at(p: GeoPoint) -> float:
        # 5 m at the start meridian, +1 m per ~10 m of easting.
        east_m = (p.lon - HERE.lon) * math.cos(math.radians(HERE.lat)) * 111_320.0
        return 5.0 + east_m / 10.0

    start = HERE
    state = _state(start, heading=0.0, depth=depth_at(start))
    state.contour_target_depth_m = 10.0  # the 10 m isobath lies ~50 m east
    state.contour_side = "deep"
    mode = ContourFollowMode()
    mode.activate(state)

    pos = start
    heading = 0.0
    speed_mps = 1.0
    dt = 0.2
    errors = []
    for _ in range(2000):  # 400 s
        state.fix = GpsFix(point=pos)
        state.heading_deg = heading
        state.depth_m = depth_at(pos)
        sp = mode.update(state, dt)
        errors.append(abs(state.depth_m - state.contour_target_depth_m))
        # Steer (instantly, for the geometry check) toward the target heading,
        # then advance.
        heading = sp.target_heading
        brg = math.radians(heading)
        de = speed_mps * dt * math.sin(brg)
        dn = speed_mps * dt * math.cos(brg)
        pos = offset_meters(pos, de, dn)

    # It should have driven into deeper water and be tracking near the isobath,
    # with the error shrinking monotonically as it curves on (no spinning).
    assert errors[-1] < 0.5
    assert errors[-1] < errors[0]  # converged toward the target depth
    # And it actually moved east (toward the deeper target).
    assert pos.lon > start.lon
