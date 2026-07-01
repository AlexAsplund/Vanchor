"""Tests for the safety governor."""

from __future__ import annotations

import pytest

from vanchor.controller.safety import SafetyConfig, SafetyGovernor, SafetyStatus
from vanchor.core.models import ControlModeName, GeoPoint, GpsFix, MotorCommand
from vanchor.core.state import NavigationState


def _gov(**kw) -> SafetyGovernor:
    return SafetyGovernor(SafetyConfig(**kw))


def _state(mode=ControlModeName.MANUAL, dist=0.0, radius=5.0, anchor=True) -> NavigationState:
    s = NavigationState()
    s.mode = mode
    s.distance_to_anchor_m = dist
    s.anchor_radius_m = radius
    # The drag alarm only makes sense when an anchor is actually set; give the
    # station-keeping states one so those tests exercise the gate.
    if anchor:
        s.anchor = GeoPoint(59.0, 18.0)
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
    gov = _gov(max_thrust_slew_per_s=100.0, fix_timeout_s=3.0, fix_failsafe_enabled=True)
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


def test_fix_failsafe_on_by_default_forces_stop():
    """The loss-of-fix failsafe is ON by default now (the conservative coast for
    a trolling motor): a long fix dropout forces thrust to zero. Turning it off
    is opt-in via config."""
    gov = _gov(max_thrust_slew_per_s=100.0, fix_timeout_s=3.0)  # default: enabled
    for _ in range(10):  # 20 s without a fresh fix, way past the timeout
        cmd, status = gov.govern(MotorCommand(thrust=0.9), _state(), dt=2.0, fix_is_fresh=False)
    assert status.fix_lost
    assert cmd.thrust == 0.0
    # And it can still be disabled explicitly, holding the last command.
    off = _gov(max_thrust_slew_per_s=100.0, fix_timeout_s=3.0, fix_failsafe_enabled=False)
    for _ in range(10):
        cmd, status = off.govern(MotorCommand(thrust=0.9), _state(), dt=2.0, fix_is_fresh=False)
    assert not status.fix_lost
    assert cmd.thrust > 0


def test_fresh_fix_resets_loss_timer():
    gov = _gov(max_thrust_slew_per_s=100.0, fix_timeout_s=1.0, fix_failsafe_enabled=True)
    gov.govern(MotorCommand(thrust=0.5), _state(), dt=0.9, fix_is_fresh=False)
    # Fresh fix arrives, clearing the accumulated gap.
    cmd, status = gov.govern(MotorCommand(thrust=0.5), _state(), dt=0.9, fix_is_fresh=True)
    assert not status.fix_lost
    assert cmd.thrust > 0


def test_heading_stale_in_guided_mode_forces_coast():
    # A guided (autopilot) mode with a stale compass must coast (zero thrust) and
    # hold the steering head, raising the heading_stale flag.
    gov = _gov(max_thrust_slew_per_s=100.0, max_steer_slew_per_s=100.0, heading_stale_s=3.0)
    st = _state(mode=ControlModeName.HEADING_HOLD)
    cmd, status = gov.govern(
        MotorCommand(thrust=0.8, steering=0.5), st, dt=0.2, fix_is_fresh=True,
        heading_age_s=5.0,
    )
    assert status.heading_stale
    assert cmd.thrust == 0.0
    assert cmd.steering == 0.0  # held at the (zero) last-applied steering


def test_heading_stale_ignored_in_manual_mode():
    # Manual mode is unaffected -- a human is steering, so a silent compass must
    # not cut their thrust.
    gov = _gov(max_thrust_slew_per_s=100.0, heading_stale_s=3.0)
    st = _state(mode=ControlModeName.MANUAL)
    cmd, status = gov.govern(
        MotorCommand(thrust=0.8), st, dt=0.2, fix_is_fresh=True, heading_age_s=99.0
    )
    assert not status.heading_stale
    assert cmd.thrust > 0


def test_heading_fresh_or_unknown_does_not_coast():
    gov = _gov(max_thrust_slew_per_s=100.0, heading_stale_s=3.0)
    st = _state(mode=ControlModeName.HEADING_HOLD)
    # Fresh heading -> steers normally.
    cmd, status = gov.govern(
        MotorCommand(thrust=0.8), st, dt=0.2, fix_is_fresh=True, heading_age_s=0.5
    )
    assert not status.heading_stale and cmd.thrust > 0
    # Never stamped (None) -> treated as fresh, no coast.
    cmd, status = gov.govern(
        MotorCommand(thrust=0.8), st, dt=0.2, fix_is_fresh=True, heading_age_s=None
    )
    assert not status.heading_stale and cmd.thrust > 0


def test_depth_stale_makes_shallow_check_treat_depth_as_unknown():
    # A frozen shallow sounding must NOT keep stopping the boat once the sounder
    # has gone stale -- treat it as unknown (same as depth <= 0), so thrust flows.
    gov = _gov(max_thrust_slew_per_s=100.0, min_depth_m=2.0, depth_stale_s=10.0)
    st = _state()
    st.depth_m = 1.0  # below min_depth, but...
    cmd, status = gov.govern(
        MotorCommand(thrust=0.8), st, dt=0.2, fix_is_fresh=True, depth_age_s=30.0
    )
    assert not status.shallow_stop
    assert cmd.thrust > 0
    # Fresh (or unknown-age) shallow sounding still stops.
    cmd, status = gov.govern(
        MotorCommand(thrust=0.8), st, dt=0.2, fix_is_fresh=True, depth_age_s=1.0
    )
    assert status.shallow_stop
    assert cmd.thrust == 0.0


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
    # A bare reset() (no seed) still ramps the slew anchor from zero.
    cmd, status = gov.govern(MotorCommand(thrust=1.0), _state(), dt=0.2, fix_is_fresh=True)
    assert abs(cmd.thrust - 0.2) < 1e-9
    assert status.thrust_limited


def test_reset_can_seed_slew_anchor_from_last_command():
    # A seeded reset (on a genuine mode change) must NOT snap the slew anchor back
    # to zero -- it starts from the last applied command so the prop doesn't surge
    # from 0 again. Here the boat is already at 0.6 thrust; re-commanding 0.6 must
    # pass straight through, not ramp up from zero.
    gov = _gov(max_thrust_slew_per_s=1.0)
    gov.govern(MotorCommand(thrust=0.6, steering=0.3), _state(), dt=0.2, fix_is_fresh=True)
    gov.reset(thrust=0.6, steering=0.3)
    cmd, status = gov.govern(MotorCommand(thrust=0.6, steering=0.3), _state(),
                             dt=0.2, fix_is_fresh=True)
    assert cmd.thrust == pytest.approx(0.6)
    assert not status.thrust_limited


def test_reverse_through_single_zero_tick_is_still_blocked():
    # A PID crossing zero (+0.8 -> 0 for ONE tick -> -0.5) must still be caught by
    # the reverse interlock; the last APPLIED direction is sticky through zero.
    gov = _gov(max_thrust_slew_per_s=100.0, reverse_delay_s=1.0)
    cmd, _ = gov.govern(MotorCommand(thrust=0.8), _state(), dt=0.2, fix_is_fresh=True)
    assert cmd.thrust > 0
    # One tick at exactly zero -- not enough rest to permit a reversal.
    gov.govern(MotorCommand(thrust=0.0), _state(), dt=0.2, fix_is_fresh=True)
    cmd, status = gov.govern(MotorCommand(thrust=-0.5), _state(), dt=0.2, fix_is_fresh=True)
    assert status.reverse_blocked
    assert cmd.thrust == 0.0


def test_drag_alarm_trips_in_anchor_ml_mode():
    # The learned spot-lock (ANCHOR_ML) holds via an anchor, so it must be inside
    # the drag-alarm net too.
    gov = _gov(drag_alarm_factor=2.0)
    st = _state(mode=ControlModeName.ANCHOR_ML, dist=11.0, radius=5.0)
    _, status = gov.govern(MotorCommand(thrust=0.1), st, dt=0.2, fix_is_fresh=True)
    assert status.drag_alarm


def test_drag_alarm_needs_an_anchor():
    # Even in an anchor-hold mode, a stale distance with no anchor set must not
    # raise the alarm.
    gov = _gov(drag_alarm_factor=2.0)
    st = _state(mode=ControlModeName.ANCHOR_HOLD, dist=100.0, radius=5.0, anchor=False)
    _, status = gov.govern(MotorCommand(thrust=0.1), st, dt=0.2, fix_is_fresh=True)
    assert not status.drag_alarm


def test_nogo_lookahead_covers_east_west_at_high_latitude():
    # A point ~4 m due EAST of a no-go polygon at 60°N must be caught by a 5 m
    # lookahead. With the old single-axis (latitude) conversion the E-W reach
    # shrank by cos(60°)=0.5, so a ~4 m eastward gap slipped through.
    lat = 60.0
    # A small square polygon; the boat sits just to its east.
    zone = [(lat, 18.0000), (lat + 0.001, 18.0000),
            (lat + 0.001, 18.0005), (lat, 18.0005)]
    # 4 m east of the polygon's east edge (18.0005). 1 deg lon at 60N ~= 55.66 km.
    dlon = 4.0 / (111320.0 * 0.5)
    boat_lon = 18.0005 + dlon
    gov = _gov(nogo_lookahead_m=5.0)
    gov.set_nogo_zones([zone])
    st = _state()
    st.fix = GpsFix(point=GeoPoint(lat + 0.0005, boat_lon))
    assert gov._in_or_near_nogo(st) is True
    # And a point well beyond the lookahead (30 m east) is NOT caught.
    st.fix = GpsFix(point=GeoPoint(lat + 0.0005, 18.0005 + 30.0 / (111320.0 * 0.5)))
    assert gov._in_or_near_nogo(st) is False


def test_status_to_dict_shape():
    s = SafetyStatus()
    d = s.to_dict()
    assert set(d) == {
        "thrust_limited",
        "steer_limited",
        "reverse_blocked",
        "fix_lost",
        "drag_alarm",
        "heading_stale",
        "shallow_stop",
        "nogo_stop",
        "min_depth_m",
        "messages",
    }


# --------------------------------------------------------------------------- #
# Fix 3: slew limiting — zero / negative means DISABLED (not "freeze")
# --------------------------------------------------------------------------- #
def test_thrust_slew_zero_means_disabled():
    # max_thrust_slew_per_s=0 must pass thrust through immediately (disabled),
    # not freeze it at 0 (which was the broken behaviour: 0*dt=0 step).
    gov = _gov(max_thrust_slew_per_s=0.0)
    cmd, status = gov.govern(MotorCommand(thrust=1.0), _state(), dt=0.2, fix_is_fresh=True)
    assert not status.thrust_limited
    assert cmd.thrust == pytest.approx(1.0)


def test_thrust_slew_negative_means_disabled():
    gov = _gov(max_thrust_slew_per_s=-5.0)
    cmd, status = gov.govern(MotorCommand(thrust=0.8), _state(), dt=0.2, fix_is_fresh=True)
    assert not status.thrust_limited
    assert cmd.thrust == pytest.approx(0.8)


def test_steer_slew_zero_means_disabled():
    # Steering already treated 0 as disabled; verify the symmetry holds.
    gov = _gov(max_steer_slew_per_s=0.0, max_thrust_slew_per_s=100.0)
    cmd, status = gov.govern(
        MotorCommand(thrust=0.1, steering=1.0), _state(), dt=0.2, fix_is_fresh=True
    )
    assert not status.steer_limited
    assert cmd.steering == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Fix 4: Work Area drag alarm — fires while holding, silent while travelling
# --------------------------------------------------------------------------- #
def test_drag_alarm_fires_in_work_area_while_holding():
    gov = _gov(drag_alarm_factor=2.0)
    st = _state(mode=ControlModeName.WORK_AREA, dist=11.0, radius=5.0)
    st.work_holding = True   # spot-locked at a spot
    _, status = gov.govern(MotorCommand(thrust=0.1), st, dt=0.2, fix_is_fresh=True)
    assert status.drag_alarm


def test_drag_alarm_silent_in_work_area_while_travelling():
    # state.anchor is stale from the last hold; drag alarm must NOT fire.
    gov = _gov(drag_alarm_factor=2.0)
    st = _state(mode=ControlModeName.WORK_AREA, dist=11.0, radius=5.0)
    st.work_holding = False  # travelling to next spot
    _, status = gov.govern(MotorCommand(thrust=0.1), st, dt=0.2, fix_is_fresh=True)
    assert not status.drag_alarm
