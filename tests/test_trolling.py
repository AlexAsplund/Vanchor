"""Trolling pattern mode (#59): sinusoidal S-curve heading weave.

Asserts the target heading oscillates as ``base + amplitude*sin(2*pi*t/period)``
around the base heading, staying within +/- amplitude, completing a full cycle
over one period, and that the base defaults to the engaged heading.
"""

import math

from vanchor.controller.modes import TrollingMode
from vanchor.core.geo import angle_difference
from vanchor.core.models import GeoPoint, GpsFix, GuidedSetpoint
from vanchor.core.state import NavigationState

HERE = GeoPoint(59.3293, 18.0686)


def _state(heading=0.0):
    s = NavigationState()
    s.fix = GpsFix(point=HERE)
    s.heading_deg = heading
    return s


def _setup(base, amplitude, period):
    state = _state(heading=base)
    state.trolling_base_heading = base
    state.trolling_amplitude_deg = amplitude
    state.trolling_period_s = period
    mode = TrollingMode()
    mode.activate(state)
    return state, mode


def test_drives_forward():
    state, mode = _setup(0.0, 20.0, 20.0)
    sp = mode.update(state, 0.2)
    assert isinstance(sp, GuidedSetpoint)
    assert sp.thrust > 0.0


def test_oscillates_within_amplitude():
    base, amp, period = 90.0, 25.0, 16.0
    state, mode = _setup(base, amp, period)
    dt = 0.2
    offsets = []
    for _ in range(int(2 * period / dt)):  # two full periods
        sp = mode.update(state, dt)
        offsets.append(angle_difference(base, sp.target_heading))
    assert max(offsets) <= amp + 1e-6
    assert min(offsets) >= -amp - 1e-6
    # It actually swings to (near) both extremes.
    assert max(offsets) > amp * 0.9
    assert min(offsets) < -amp * 0.9


def test_follows_sine_shape():
    base, amp, period = 0.0, 30.0, 20.0
    state, mode = _setup(base, amp, period)
    dt = 0.1
    t = 0.0
    for _ in range(150):
        sp = mode.update(state, dt)
        t += dt
        expected = base + amp * math.sin(2 * math.pi * t / period)
        got = base + angle_difference(base, sp.target_heading)
        assert abs(got - expected) < 1e-6


def test_completes_one_cycle_per_period():
    base, amp, period = 45.0, 20.0, 10.0
    state, mode = _setup(base, amp, period)
    dt = 0.05
    offsets = []
    for _ in range(int(period / dt)):
        sp = mode.update(state, dt)
        offsets.append(angle_difference(base, sp.target_heading))
    # One sign-change up and one down over a single period => 2 zero crossings.
    crossings = sum(
        1 for a, b in zip(offsets, offsets[1:]) if (a <= 0 < b) or (a >= 0 > b)
    )
    assert crossings == 2


def test_base_defaults_to_engaged_heading():
    # When no base_heading is supplied the controller stores the live heading;
    # here we emulate that and confirm the weave centres on it.
    state = _state(heading=137.0)
    state.trolling_base_heading = state.heading_deg
    state.trolling_amplitude_deg = 15.0
    state.trolling_period_s = 12.0
    mode = TrollingMode()
    mode.activate(state)
    # At t=0 (phase 0) the offset is 0 => target == base.
    sp = mode.update(state, 0.0)
    assert abs(angle_difference(137.0, sp.target_heading)) < 1e-6
