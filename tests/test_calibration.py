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


# --------------------------------------------------------------------------- #
# Turn-test steering authority + the garbage-measurement guardrail.
#
# Regression: the turn test used manual FULL-LOCK (steering=1.0). Manual full
# scale is the full mechanical swing (max_steer_angle_deg = 180°), so the
# "hard turn" physically pointed the prop dead astern — the boat backed up
# instead of turning, and the measured max_turn_rate/steering_sign were
# garbage that then got APPLIED to the boat config.
# --------------------------------------------------------------------------- #

def _runner_with_boat(**boat_overrides):
    from vanchor.controller.calibration import CalibrationRunner
    from vanchor.core.config import load

    class _StubRuntime:
        def __init__(self):
            self.config = load(None)
            for k, v in boat_overrides.items():
                setattr(self.config.boat, k, v)

    return CalibrationRunner(_StubRuntime())


def test_hard_over_is_the_autopilot_authority_not_full_lock():
    r = _runner_with_boat()  # defaults: autopilot 35° of a 180° full scale
    assert abs(r._hard_over_norm() - 35.0 / 180.0) < 1e-9
    assert r._hard_over_norm() < 0.25  # far from full lock


def test_hard_over_clamps_to_valid_command_range():
    assert _runner_with_boat(autopilot_steer_deg=999.0)._hard_over_norm() == 1.0
    assert _runner_with_boat(autopilot_steer_deg=0.0)._hard_over_norm() == 0.05
    # Degenerate full scale -> fall back to full command range.
    assert _runner_with_boat(max_steer_angle_deg=0.0)._hard_over_norm() == 1.0


def test_guard_turn_aborts_on_no_meaningful_turn():
    import pytest

    from vanchor.controller.calibration import CalibrationAbort, _guard_turn

    with pytest.raises(CalibrationAbort):
        _guard_turn(0.02)   # the astern-push signature: essentially no turn
    _guard_turn(6.0)        # a healthy hard-turn rate passes
