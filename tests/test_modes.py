import pytest

from vanchor.controller.controller import Helm
from vanchor.controller.modes import (
    AnchorHoldMode,
    DriftMode,
    HeadingHoldMode,
    ManualMode,
    WaypointConfig,
    WaypointMode,
)
from vanchor.core.geo import angle_difference, destination_point
from vanchor.core.models import (
    GeoPoint,
    GpsFix,
    GuidedSetpoint,
    ManualSetpoint,
    Waypoint,
)
from vanchor.core.state import NavigationState

HERE = GeoPoint(59.3293, 18.0686)


def _state_at(point, heading=0.0):
    s = NavigationState()
    s.fix = GpsFix(point=point)
    s.heading_deg = heading
    return s


def test_manual_mode_passthrough():
    mode = ManualMode()
    mode.set(0.5, -0.3)
    sp = mode.update(_state_at(HERE), 0.2)
    assert isinstance(sp, ManualSetpoint)
    assert sp.thrust == 0.5 and sp.steering == -0.3


def test_heading_hold_targets_state_heading():
    mode = HeadingHoldMode(throttle=0.4)
    state = _state_at(HERE)
    state.target_heading = 123.0
    sp = mode.update(state, 0.2)
    assert isinstance(sp, GuidedSetpoint)
    assert sp.target_heading == 123.0
    assert sp.thrust == 0.4


def test_anchor_hold_points_home_with_thrust_when_far():
    anchor = HERE
    boat = destination_point(anchor, 30.0, 0.0)  # 30 m due north of anchor
    # Boat already pointing at the anchor (due south) => drive forward toward it.
    state = _state_at(boat, heading=180.0)
    state.anchor = anchor
    state.anchor_radius_m = 5.0
    mode = AnchorHoldMode()
    mode.activate(state)
    sp = mode.update(state, 0.2)
    assert isinstance(sp, GuidedSetpoint)
    assert sp.target_heading == pytest.approx(180.0, abs=1.0)
    assert sp.thrust > 0.0


def test_anchor_hold_reverses_when_anchor_is_behind():
    # Boat 30 m north of the anchor but pointing north (anchor astern): the
    # controller should back straight up (reverse) instead of looping around.
    anchor = HERE
    boat = destination_point(anchor, 30.0, 0.0)
    state = _state_at(boat, heading=0.0)  # facing away from the anchor
    state.anchor = anchor
    state.anchor_radius_m = 5.0
    mode = AnchorHoldMode()
    mode.activate(state)
    sp = mode.update(state, 0.2)
    assert isinstance(sp, GuidedSetpoint)
    assert sp.thrust < 0.0  # reverse toward the anchor
    # Heading target stays near the current heading (no 180° turn-around).
    assert abs(angle_difference(state.heading_deg, sp.target_heading)) < 20.0


def test_anchor_hold_idle_when_settled():
    # Sitting on the anchor at the held heading with no motion => ~no thrust
    # (no jitter), which the helm then turns into a frozen steering command.
    anchor = HERE
    state = _state_at(anchor, heading=0.0)
    state.anchor = anchor
    state.anchor_heading = 0.0
    state.anchor_radius_m = 5.0
    mode = AnchorHoldMode()
    mode.activate(state)
    sp = mode.update(state, 0.2)
    assert abs(sp.thrust) < 0.02


def test_waypoint_mode_advances_on_arrival():
    wp0 = destination_point(HERE, 50.0, 90.0)
    wp1 = destination_point(HERE, 100.0, 90.0)
    state = _state_at(HERE)
    state.waypoints = [Waypoint("WP0", wp0), Waypoint("WP1", wp1)]
    mode = WaypointMode()
    mode.activate(state)

    # Far from WP0: steers toward it, cruising.
    sp = mode.update(state, 0.2)
    assert isinstance(sp, GuidedSetpoint)
    assert sp.thrust > 0
    assert state.active_waypoint == 0

    # Move boat onto WP0 -> should advance to WP1.
    state.fix = GpsFix(point=wp0)
    mode.update(state, 0.2)
    assert state.active_waypoint == 1


def test_waypoint_mode_stops_after_last():
    wp0 = destination_point(HERE, 3.0, 90.0)
    state = _state_at(HERE)
    state.waypoints = [Waypoint("WP0", wp0)]
    mode = WaypointMode()
    mode.activate(state)
    state.fix = GpsFix(point=wp0)  # already within arrival radius
    sp = mode.update(state, 0.2)
    assert state.active_waypoint == 1
    assert sp.thrust == 0.0


def test_helm_steers_toward_starboard_target():
    helm = Helm()
    state = _state_at(HERE, heading=0.0)
    # Target 90 deg is to starboard => positive steering.
    cmd = helm.compute(GuidedSetpoint(target_heading=90.0, thrust=0.5), state, 0.2)
    assert cmd.steering > 0
    assert cmd.thrust == 0.5

    state2 = _state_at(HERE, heading=0.0)
    cmd2 = helm.compute(GuidedSetpoint(target_heading=270.0, thrust=0.5), state2, 0.2)
    assert cmd2.steering < 0  # target to port => negative steering


def test_helm_manual_passthrough_clamped():
    helm = Helm()
    cmd = helm.compute(ManualSetpoint(thrust=2.0, steering=-5.0), _state_at(HERE), 0.2)
    assert cmd.thrust == 1.0 and cmd.steering == -1.0


def test_drift_regulates_signed_along_heading_speed_not_magnitude():
    # Held heading north; the boat is drifting purely EAST (transverse) at 1.0 kn.
    # The along-heading component is ~0, so the controller must ADD thrust to make
    # way along the held heading -- NOT brake as it would if it PIDed the unsigned
    # SOG (which would see 1.0 kn > the 0.5 kn target and command reverse).
    mode = DriftMode()
    state = _state_at(HERE, heading=0.0)
    state.target_heading = 0.0
    state.drift_target_knots = 0.5
    state.fix = GpsFix(point=HERE, sog_knots=1.0, cog_deg=90.0)  # drifting east
    state.sog_knots = 1.0
    sp = mode.update(state, 0.2)
    assert isinstance(sp, GuidedSetpoint)
    assert sp.thrust > 0.0


def test_drift_brakes_when_too_fast_along_heading():
    # Moving ALONG the held heading faster than the target -> brake (reverse).
    mode = DriftMode()
    state = _state_at(HERE, heading=0.0)
    state.target_heading = 0.0
    state.drift_target_knots = 0.5
    state.fix = GpsFix(point=HERE, sog_knots=1.0, cog_deg=0.0)  # moving north, fast
    state.sog_knots = 1.0
    sp = mode.update(state, 0.2)
    assert sp.thrust < 0.0


def test_anchor_drift_ema_is_frame_rate_independent():
    # The drift-estimate EMA time constant must be fixed in SECONDS, not per tick:
    # stepping the same total time in one big dt vs. many small dt should land on
    # nearly the same estimate. (A fixed per-tick weight would diverge.) The drift
    # estimator is now the shared, persistent WindCurrentEstimator (promoted out of
    # AnchorHoldMode), so this frame-rate-independence property is tested directly
    # against it rather than via the mode's private (now removed) EMA.
    from vanchor.controller.estimator import WindCurrentEstimator

    boat = destination_point(HERE, 10.0, 0.0)

    def run(dt: float, steps: int) -> float:
        state = _state_at(boat, heading=0.0)
        # A steady eastward set/drift the estimator should learn (no own thrust).
        state.fix = GpsFix(point=boat, sog_knots=1.0, cog_deg=90.0)
        state.sog_knots = 1.0
        est = WindCurrentEstimator()  # default config (tau_s = 10 s)
        for _ in range(steps):
            est.update(state, dt)
        return state.est_drift_mps

    coarse = run(dt=0.5, steps=20)   # 10 s total
    fine = run(dt=0.05, steps=200)   # 10 s total
    assert coarse == pytest.approx(fine, abs=0.05)


# --------------------------------------------------------------------------- #
# Fix 2: waypoint arrival robustness
# --------------------------------------------------------------------------- #
def test_waypoint_mode_advances_when_boat_passes_perpendicular():
    """A boat that sails PAST a waypoint 10 m abeam must advance to the next,
    even though it never entered the arrival radius."""
    wp0 = destination_point(HERE, 100.0, 0.0)   # 100 m due north
    wp1 = destination_point(HERE, 200.0, 0.0)   # 200 m due north
    state = _state_at(HERE, heading=0.0)
    state.waypoints = [Waypoint("WP0", wp0), Waypoint("WP1", wp1)]
    mode = WaypointMode(WaypointConfig(arrival_radius_m=5.0, allow_reverse=False))
    mode.activate(state)

    # First tick with the boat at HERE sets up leg_start.
    mode.update(state, 0.2)
    assert state.active_waypoint == 0

    # Boat sails past wp0: 110 m north + 10 m east (well outside the 5 m radius,
    # but abeam-past the perpendicular with <15 m cross-track).
    boat_past = destination_point(destination_point(HERE, 110.0, 0.0), 10.0, 90.0)
    state.fix = GpsFix(point=boat_past)
    mode.update(state, 0.2)
    assert state.active_waypoint == 1  # advanced even without entering the circle


def test_waypoint_mode_perpendicular_advance_does_not_fire_on_wide_miss():
    """A boat far off to the side (> 3x radius) must NOT trigger the perpendicular
    advance, to prevent wild-miss false advances."""
    wp0 = destination_point(HERE, 100.0, 0.0)
    wp1 = destination_point(HERE, 200.0, 0.0)
    state = _state_at(HERE)
    state.waypoints = [Waypoint("WP0", wp0), Waypoint("WP1", wp1)]
    mode = WaypointMode(WaypointConfig(arrival_radius_m=5.0, allow_reverse=False))
    mode.activate(state)
    mode.update(state, 0.2)

    # 110 m north but 100 m off to the side — way beyond 3x arrival radius (15 m).
    boat_wide = destination_point(destination_point(HERE, 110.0, 0.0), 100.0, 90.0)
    state.fix = GpsFix(point=boat_wide)
    mode.update(state, 0.2)
    assert state.active_waypoint == 0  # NOT advanced


def test_waypoint_mode_multi_advance_stacked_waypoints():
    """Three consecutive waypoints all within the arrival radius must all be
    consumed in a single tick, leaving the boat heading for the next distant one."""
    r = 5.0
    wp0 = destination_point(HERE, 1.0, 0.0)    # 1 m away — within r
    wp1 = destination_point(HERE, 2.0, 90.0)   # 2 m away — within r
    wp2 = destination_point(HERE, 3.0, 180.0)  # 3 m away — within r
    wp3 = destination_point(HERE, 100.0, 0.0)  # 100 m away — outside r
    state = _state_at(HERE)
    state.waypoints = [
        Waypoint("WP0", wp0), Waypoint("WP1", wp1),
        Waypoint("WP2", wp2), Waypoint("WP3", wp3),
    ]
    mode = WaypointMode(WaypointConfig(arrival_radius_m=r, allow_reverse=False))
    mode.activate(state)
    mode.update(state, 0.2)
    # All three stacked waypoints consumed in one tick.
    assert state.active_waypoint == 3


def test_waypoint_mode_normal_arrival_unaffected():
    """Existing radius-based arrival still advances correctly after the fix."""
    wp0 = destination_point(HERE, 50.0, 90.0)
    wp1 = destination_point(HERE, 100.0, 90.0)
    state = _state_at(HERE)
    state.waypoints = [Waypoint("WP0", wp0), Waypoint("WP1", wp1)]
    mode = WaypointMode(WaypointConfig(arrival_radius_m=5.0, allow_reverse=False))
    mode.activate(state)
    state.fix = GpsFix(point=wp0)  # exactly on wp0
    mode.update(state, 0.2)
    assert state.active_waypoint == 1


def test_waypoint_mode_route_complete_fires_on_final_stacked_waypoint():
    """Multi-advance to the last waypoint must still fire route_complete."""
    r = 5.0
    wp0 = destination_point(HERE, 1.0, 0.0)
    wp1 = destination_point(HERE, 2.0, 90.0)
    state = _state_at(HERE)
    state.waypoints = [Waypoint("WP0", wp0), Waypoint("WP1", wp1)]
    mode = WaypointMode(WaypointConfig(arrival_radius_m=r, allow_reverse=False))
    mode.activate(state)
    sp = mode.update(state, 0.2)
    assert state.route_complete is True
    assert sp.thrust == 0.0
