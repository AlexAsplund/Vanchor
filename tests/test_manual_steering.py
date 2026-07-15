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
