"""Tests for the safety governor."""

from __future__ import annotations

from vanchor.controller.safety import SafetyConfig, SafetyGovernor, SafetyStatus
from vanchor.core.models import ControlModeName, MotorCommand
from vanchor.core.state import NavigationState


def _gov(**kw) -> SafetyGovernor:
    return SafetyGovernor(SafetyConfig(**kw))


def _state(mode=ControlModeName.MANUAL, dist=0.0, radius=5.0) -> NavigationState:
    s = NavigationState()
    s.mode = mode
    s.distance_to_anchor_m = dist
    s.anchor_radius_m = radius
    return s


def test_thrust_step_is_slew_limited():
    gov = _gov(max_thrust_slew_per_s=1.0)
    cmd, status = gov.govern(MotorCommand(thrust=1.0), _state(), dt=0.2, fix_is_fresh=True)
    assert status.thrust_limited
    # Max change over 0.2s at 1.0/s is 0.2.
    assert abs(cmd.thrust - 0.2) < 1e-9


def test_slew_converges_over_several_ticks():
    gov = _gov(max_thrust_slew_per_s=1.0)
    last = 0.0
    for _ in range(10):
        cmd, _ = gov.govern(MotorCommand(thrust=1.0), _state(), dt=0.2, fix_is_fresh=True)
        # Never jumps more than 0.2 per tick.
        assert cmd.thrust - last <= 0.2 + 1e-9
        last = cmd.thrust
    assert abs(last - 1.0) < 1e-9


def test_small_thrust_change_not_limited():
    gov = _gov(max_thrust_slew_per_s=10.0)
    cmd, status = gov.govern(MotorCommand(thrust=0.1), _state(), dt=0.2, fix_is_fresh=True)
    assert not status.thrust_limited
    assert abs(cmd.thrust - 0.1) < 1e-9


def test_steering_passes_through_when_within_slew():
    # A steering change within the slew limit is unchanged.
    gov = _gov(max_thrust_slew_per_s=0.1, max_steer_slew_per_s=10.0)
    cmd, _ = gov.govern(
        MotorCommand(thrust=1.0, steering=-0.7), _state(), dt=0.1, fix_is_fresh=True
    )
    assert cmd.steering == -0.7


def test_steering_slew_limited():
    # A large steering step from rest is rate-limited to max_steer_slew_per_s*dt.
    gov = _gov(max_steer_slew_per_s=1.0)
    cmd, status = gov.govern(
        MotorCommand(thrust=1.0, steering=1.0), _state(), dt=0.2, fix_is_fresh=True
    )
    assert abs(cmd.steering - 0.2) < 1e-9  # 1.0/s * 0.2 s
    assert status.steer_limited is True


def test_reverse_blocked_right_after_forward_then_allowed_after_delay():
    gov = _gov(max_thrust_slew_per_s=100.0, reverse_delay_s=1.0)
    # Establish forward thrust.
    cmd, status = gov.govern(MotorCommand(thrust=0.8), _state(), dt=0.2, fix_is_fresh=True)
    assert cmd.thrust > 0 and not status.reverse_blocked

    # Immediately request reverse -> blocked, held at 0.
    cmd, status = gov.govern(MotorCommand(thrust=-0.8), _state(), dt=0.2, fix_is_fresh=True)
    assert status.reverse_blocked
    assert cmd.thrust == 0.0

    # Rest near zero for >= reverse_delay_s.
    for _ in range(5):
        cmd, status = gov.govern(
            MotorCommand(thrust=0.0), _state(), dt=0.3, fix_is_fresh=True
        )
    assert not status.reverse_blocked

    # Now reverse is permitted.
    cmd, status = gov.govern(MotorCommand(thrust=-0.8), _state(), dt=0.2, fix_is_fresh=True)
    assert not status.reverse_blocked
    assert cmd.thrust < 0.0


def test_coming_to_stop_is_not_a_blocked_reversal():
    gov = _gov(max_thrust_slew_per_s=100.0, reverse_delay_s=1.0)
    gov.govern(MotorCommand(thrust=0.8), _state(), dt=0.2, fix_is_fresh=True)
    # Going to zero is never blocked.
    cmd, status = gov.govern(MotorCommand(thrust=0.0), _state(), dt=0.2, fix_is_fresh=True)
    assert not status.reverse_blocked
    assert cmd.thrust == 0.0


def test_fix_lost_after_timeout_forces_zero():
    gov = _gov(max_thrust_slew_per_s=100.0, fix_timeout_s=3.0)
    # Build up thrust with a fresh fix.
    cmd, status = gov.govern(MotorCommand(thrust=0.9), _state(), dt=0.2, fix_is_fresh=True)
    assert cmd.thrust > 0 and not status.fix_lost

    # No fresh fix; below timeout -> still running.
    cmd, status = gov.govern(MotorCommand(thrust=0.9), _state(), dt=2.0, fix_is_fresh=False)
    assert not status.fix_lost
    assert cmd.thrust > 0

    # Now exceed the timeout -> forced to zero.
    cmd, status = gov.govern(MotorCommand(thrust=0.9), _state(), dt=2.0, fix_is_fresh=False)
    assert status.fix_lost
    assert cmd.thrust == 0.0


def test_fresh_fix_resets_loss_timer():
    gov = _gov(max_thrust_slew_per_s=100.0, fix_timeout_s=1.0)
    gov.govern(MotorCommand(thrust=0.5), _state(), dt=0.9, fix_is_fresh=False)
    # Fresh fix arrives, clearing the accumulated gap.
    cmd, status = gov.govern(MotorCommand(thrust=0.5), _state(), dt=0.9, fix_is_fresh=True)
    assert not status.fix_lost
    assert cmd.thrust > 0


def test_drag_alarm_trips_beyond_threshold_in_anchor_mode():
    gov = _gov(drag_alarm_factor=2.0)
    st = _state(mode=ControlModeName.ANCHOR_HOLD, dist=11.0, radius=5.0)
    _, status = gov.govern(MotorCommand(thrust=0.1), st, dt=0.2, fix_is_fresh=True)
    assert status.drag_alarm


def test_drag_alarm_does_not_trip_within_threshold():
    gov = _gov(drag_alarm_factor=2.0)
    st = _state(mode=ControlModeName.ANCHOR_HOLD, dist=9.0, radius=5.0)
    _, status = gov.govern(MotorCommand(thrust=0.1), st, dt=0.2, fix_is_fresh=True)
    assert not status.drag_alarm


def test_drag_alarm_only_in_anchor_mode():
    gov = _gov(drag_alarm_factor=2.0)
    st = _state(mode=ControlModeName.MANUAL, dist=100.0, radius=5.0)
    _, status = gov.govern(MotorCommand(thrust=0.1), st, dt=0.2, fix_is_fresh=True)
    assert not status.drag_alarm


def test_reset_clears_internal_state():
    gov = _gov(max_thrust_slew_per_s=1.0)
    gov.govern(MotorCommand(thrust=1.0), _state(), dt=0.2, fix_is_fresh=True)
    gov.reset()
    # After reset the slew anchor is back at zero.
    cmd, status = gov.govern(MotorCommand(thrust=1.0), _state(), dt=0.2, fix_is_fresh=True)
    assert abs(cmd.thrust - 0.2) < 1e-9
    assert status.thrust_limited


def test_status_to_dict_shape():
    s = SafetyStatus()
    d = s.to_dict()
    assert set(d) == {
        "thrust_limited",
        "steer_limited",
        "reverse_blocked",
        "fix_lost",
        "drag_alarm",
        "shallow_stop",
        "nogo_stop",
        "min_depth_m",
        "messages",
    }
