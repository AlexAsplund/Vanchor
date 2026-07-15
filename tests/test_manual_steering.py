"""Manual steering: relative (boat-frame) vs absolute (compass-bearing) mode.

Absolute holds the motor head on a compass bearing (0=N, 180=S): the
normalized deflection is recomputed from the LIVE heading every tick, so the
head stays put in the world while the boat yaws underneath it.
"""

import pytest

from vanchor.controller.controller import Controller
from vanchor.controller.modes import ManualMode
from vanchor.core.models import GeoPoint, GpsFix, ManualSetpoint
from vanchor.core.state import NavigationState
from vanchor.sim.devices import SimMotorController


def _state(heading: float) -> NavigationState:
    st = NavigationState()
    st.heading_deg = heading
    st.max_steer_angle_deg = 180.0
    return st


def _steering(mode: ManualMode, heading: float) -> float:
    sp = mode.update(_state(heading), 0.1)
    assert isinstance(sp, ManualSetpoint)
    return sp.steering


def test_absolute_bearing_maps_to_boat_relative_offset():
    m = ManualMode()
    m.set_bearing(0.5, 90.0)                     # head east
    assert _steering(m, 90.0) == pytest.approx(0.0)    # bow already east
    assert _steering(m, 0.0) == pytest.approx(0.5)     # bow north -> 90° stbd
    assert _steering(m, 180.0) == pytest.approx(-0.5)  # bow south -> 90° port


def test_absolute_tracks_the_live_heading_as_the_boat_yaws():
    m = ManualMode()
    m.set_bearing(0.3, 0.0)                      # hold NORTH
    # As the boat swings 350 -> 10, the offset flips sign across the wrap.
    assert _steering(m, 350.0) == pytest.approx(10.0 / 180.0)
    assert _steering(m, 10.0) == pytest.approx(-10.0 / 180.0)


def test_relative_command_clears_the_absolute_hold():
    m = ManualMode()
    m.set_bearing(0.3, 90.0)
    m.set(0.3, 0.2)                              # back to relative
    assert m.steer_bearing is None
    assert _steering(m, 0.0) == pytest.approx(0.2)
    assert _steering(m, 123.0) == pytest.approx(0.2)   # heading-independent


def test_bearing_normalized_and_offset_clamped():
    m = ManualMode()
    m.set_bearing(0.1, 450.0)                    # 450 -> 90
    assert m.steer_bearing == pytest.approx(90.0)
    # Degenerate full scale falls back to 180 (no division blow-up).
    st = _state(270.0)
    st.max_steer_angle_deg = 0.0
    sp = m.update(st, 0.1)
    assert sp.steering == pytest.approx(180.0 / 180.0)  # wrap: 270->90 = +180


def test_manual_command_with_steer_bearing_holds_across_yaw():
    st = NavigationState()
    st.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    st.heading_deg = 0.0
    st.max_steer_angle_deg = 180.0
    ctrl = Controller(st, SimMotorController(), bus=None)
    ctrl.handle_command({"type": "manual", "thrust": 0.4, "steer_bearing": 90})
    st.fix_seq += 1
    ctrl.control_tick(0.2)
    first = st.motor_command.steering
    assert first > 0.0                            # steering toward east
    # The boat swings to 90 -> the commanded deflection relaxes toward 0.
    st.heading_deg = 90.0
    for _ in range(40):                           # let the governor slew settle
        st.fix_seq += 1
        ctrl.control_tick(0.2)
    assert abs(st.motor_command.steering) < 0.05
    # A plain relative manual command clears the hold.
    ctrl.handle_command({"type": "manual", "thrust": 0.0, "steering": 0.0})
    assert ctrl.manual.steer_bearing is None


# --------------------------------------------------------------------------- #
# COURSE hold: follow the ground-track line drawn from the engage position.
# --------------------------------------------------------------------------- #
from vanchor.core.geo import destination_point  # noqa: E402
from vanchor.core.models import GuidedSetpoint  # noqa: E402


def _course_target(m: ManualMode, pos: GeoPoint, heading: float = 0.0) -> float:
    st = _state(heading)
    st.fix = GpsFix(point=pos)
    sp = m.update(st, 0.1)
    assert isinstance(sp, GuidedSetpoint)
    return sp.target_heading


def test_course_on_the_line_steers_the_bearing():
    m = ManualMode()
    origin = GeoPoint(59.0, 18.0)
    m.set_course(0.4, 270.0, origin)                  # straight west
    assert _course_target(m, origin) == pytest.approx(270.0)
    on_line = destination_point(origin, 500.0, 270.0)
    assert _course_target(m, on_line) == pytest.approx(270.0, abs=0.2)


def test_course_corrects_back_toward_the_line():
    m = ManualMode()
    origin = GeoPoint(59.0, 18.0)
    m.set_course(0.4, 270.0, origin)
    # 10 m NORTH of a westbound track = starboard of track -> steer left
    # (target < 270), clamped at 45°.
    north = destination_point(origin, 10.0, 0.0)
    t = _course_target(m, north)
    assert 270.0 - 45.0 - 0.5 <= t < 269.0
    # 10 m SOUTH = port of track -> steer right (target > 270).
    south = destination_point(origin, 10.0, 180.0)
    t2 = _course_target(m, south)
    assert 271.0 < t2 <= 270.0 + 45.0 + 0.5
    # Far off the line: correction clamps at ±45°.
    far = destination_point(origin, 500.0, 0.0)
    assert _course_target(m, far) == pytest.approx(270.0 - 45.0, abs=0.5)


def test_course_line_anchors_once_and_rearms_on_new_bearing():
    m = ManualMode()
    origin = GeoPoint(59.0, 18.0)
    m.set_course(0.4, 270.0, origin)
    # Re-sending the SAME course from a drifted position must keep the line.
    drifted = destination_point(origin, 15.0, 0.0)
    m.set_course(0.7, 270.0, drifted)                 # thrust tweak, same course
    assert m.course_origin == origin
    t = _course_target(m, drifted)
    assert t < 269.0                                  # still correcting to old line
    # A NEW bearing re-anchors at the current position.
    m.set_course(0.7, 180.0, drifted)
    assert m.course_origin == drifted
    assert m.course_bearing == pytest.approx(180.0)


def test_course_cleared_by_relative_and_absolute_commands():
    m = ManualMode()
    m.set_course(0.4, 90.0, GeoPoint(59.0, 18.0))
    m.set_bearing(0.4, 90.0)
    assert m.course_bearing is None and m.course_origin is None
    m.set_course(0.4, 90.0, GeoPoint(59.0, 18.0))
    m.set(0.0, 0.0)
    assert m.course_bearing is None and m.steer_bearing is None


def test_manual_command_steer_course_via_controller():
    st = NavigationState()
    st.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    st.heading_deg = 270.0
    st.max_steer_angle_deg = 180.0
    ctrl = Controller(st, SimMotorController(), bus=None)
    ctrl.handle_command({"type": "manual", "thrust": 0.4, "steer_course": 270})
    assert ctrl.manual.course_bearing == pytest.approx(270.0)
    assert ctrl.manual.course_origin is not None
    st.fix_seq += 1
    ctrl.control_tick(0.2)
    assert st.target_heading == pytest.approx(270.0, abs=1.0)
