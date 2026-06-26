"""Tests for the boat profile API and the calibration math."""

from vanchor.controller.calibration import _mps, _time_constant, _turn_rate


def test_mps_from_knots():
    assert round(_mps(1.0), 3) == 0.514


def test_rising_time_constant():
    # reaches 0.63*10=6.3 at t=2.0
    samples = [(i * 0.5, min(10.0, i * 2.0)) for i in range(10)]
    assert _time_constant(samples, 10.0, rising=True) > 0


def test_falling_time_constant():
    samples = [(i * 0.5, max(0.0, 10.0 - i * 2.0)) for i in range(10)]
    assert _time_constant(samples, 10.0, rising=False) > 0


def test_turn_rate_sign_positive():
    headings = [(i * 0.1, i * 2.0) for i in range(10)]  # steadily increasing
    rate, sign = _turn_rate(headings)
    assert rate > 0 and sign == 1


def test_turn_rate_sign_negative_and_wrap():
    headings = [(i * 0.1, (5 - i * 2.0) % 360) for i in range(10)]  # decreasing across 0
    rate, sign = _turn_rate(headings)
    assert sign == -1 and rate > 0
