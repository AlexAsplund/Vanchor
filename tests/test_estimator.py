"""Tests for the shared, persistent wind/current (drift) estimator (roadmap #27).

Covers the estimator itself (convergence, dt-scale invariance, thrust decoupling,
turn gating, settling, no-reset-on-mode-change) and its consumers (waypoint crab
feed-forward and Spot-Lock reading the estimate immediately on activate).
"""

import math

import pytest

from vanchor.controller.controller import Controller
from vanchor.controller.estimator import (
    EstimatorConfig,
    WindCurrentEstimator,
    crab_offset_deg,
)
from vanchor.controller.modes import AnchorConfig, AnchorHoldMode, WaypointConfig, WaypointMode
from vanchor.core.geo import angle_difference, destination_point, knots_to_mps
from vanchor.core.models import (
    ControlModeName,
    GeoPoint,
    GpsFix,
    ImuSample,
    MotorCommand,
    Waypoint,
)
from vanchor.core.state import NavigationState
from vanchor.sim.devices import SimMotorController

HERE = GeoPoint(59.3293, 18.0686)


def _state(point=HERE, heading=0.0, sog=0.0, cog=0.0):
    s = NavigationState()
    s.fix = GpsFix(point=point, sog_knots=sog, cog_deg=cog)
    s.heading_deg = heading
    s.sog_knots = sog
    return s


# --------------------------------------------------------------------------- #
# Estimator core
# --------------------------------------------------------------------------- #
def test_converges_to_steady_drift_vector():
    # A constant eastward ground velocity with no own thrust -> the estimator
    # should learn a drift of that speed, pushing due east (~090).
    est = WindCurrentEstimator()
    state = _state(sog=1.0, cog=90.0)  # 1 kn due east
    for _ in range(400):
        est.update(state, 0.2)  # 80 s >> tau
    assert est.drift_speed_mps == pytest.approx(knots_to_mps(1.0), abs=0.02)
    assert abs(angle_difference(est.drift_dir_deg, 90.0)) < 3.0
    # Published onto the shared state.
    assert state.est_drift_mps == pytest.approx(est.drift_speed_mps, abs=1e-9)
    assert state.est_drift_settled is True


def test_dt_scale_invariant():
    # Same total learning time in one coarse vs many fine steps -> same estimate.
    def run(dt, steps):
        est = WindCurrentEstimator()
        state = _state(sog=1.0, cog=90.0)
        for _ in range(steps):
            est.update(state, dt)
        return est.drift_speed_mps

    assert run(0.5, 40) == pytest.approx(run(0.05, 400), abs=0.03)


def test_thrust_decoupling_learns_drift_while_holding_station():
    # Boat holds station (SOG ~0) by thrusting WEST (heading 270, thrust 0.3) to
    # fight an eastward set. Observed velocity is ~0, but decoupling the thrust
    # reveals the ~0.3*max_speed eastward drift it is cancelling.
    est = WindCurrentEstimator(EstimatorConfig(boat_max_speed_mps=1.6))
    state = _state(heading=270.0, sog=0.0)
    state.motor_command = MotorCommand(thrust=0.3, steering=0.0)
    for _ in range(400):
        est.update(state, 0.2)
    assert est.drift_east > 0.3  # ~0.3 * 1.6 = 0.48, pushing east
    assert abs(est.drift_north) < 0.05
    assert abs(angle_difference(est.drift_dir_deg, 90.0)) < 5.0


def test_gates_during_sharp_turns():
    # With a large IMU yaw rate every tick, the estimator must FREEZE (never learn)
    # even though a big drift sample is presented -- mid-turn geometry is untrusted.
    est = WindCurrentEstimator(EstimatorConfig(max_turn_dps=25.0))
    state = _state(sog=2.0, cog=90.0)
    state.imu = ImuSample(gz=60.0)  # 60 deg/s -> well past the gate
    for _ in range(200):
        est.update(state, 0.2)
    assert est.drift_speed_mps < 0.02  # nothing learned
    assert est.settled is False


def test_gate_uses_heading_rate_without_imu():
    # No IMU: the compass heading rate gates. A steady heading (no turn) learns;
    # this asserts the fallback path doesn't block a straight run.
    est = WindCurrentEstimator()
    state = _state(heading=10.0, sog=1.0, cog=90.0)
    for _ in range(200):
        est.update(state, 0.2)  # heading constant -> not gated
    assert est.drift_speed_mps == pytest.approx(knots_to_mps(1.0), abs=0.03)


def test_not_settled_after_one_sample():
    est = WindCurrentEstimator()
    est.update(_state(sog=1.0, cog=90.0), 0.2)
    assert est.settled is False
    assert 0.0 <= est.confidence < 1.0


def test_does_not_reset_across_mode_changes():
    # The persistent estimator lives on the Controller and must survive mode
    # switches (Spot-Lock etc. must NOT relearn the environment).
    state = _state(sog=1.0, cog=90.0)
    ctrl = Controller(state, SimMotorController(), bus=None)
    for _ in range(200):
        state.fix_seq += 1
        ctrl.control_tick(0.2)
    learned = (ctrl.estimator.drift_east, ctrl.estimator.drift_north)
    assert math.hypot(*learned) > 0.3  # something was learned

    ctrl.set_mode(ControlModeName.ANCHOR_HOLD)  # a real mode change
    # set_mode must not have touched the estimate.
    assert (ctrl.estimator.drift_east, ctrl.estimator.drift_north) == learned


def test_controller_feeds_estimator_every_tick_in_all_modes():
    # Even in MANUAL the estimate stays fresh (persistent service).
    state = _state(sog=1.2, cog=0.0)  # drifting north
    ctrl = Controller(state, SimMotorController(), bus=None)
    assert state.mode == ControlModeName.MANUAL
    for _ in range(200):
        state.fix_seq += 1
        ctrl.control_tick(0.2)
    assert state.est_drift_mps > 0.3
    assert abs(angle_difference(state.est_drift_dir, 0.0)) < 5.0


# --------------------------------------------------------------------------- #
# crab_offset_deg helper
# --------------------------------------------------------------------------- #
def test_crab_offset_points_into_beam_drift():
    # Leg due north (bearing 0), drift pushing EAST (to starboard) -> crab must be
    # NEGATIVE (aim to port / upwind), bounded by max_crab_deg.
    off = crab_offset_deg(0.0, drift_east=0.4, drift_north=0.0, water_speed_mps=1.0)
    assert off < 0.0
    assert off == pytest.approx(-math.degrees(math.asin(0.4)), abs=0.5)

    # Drift pushing WEST (to port) -> aim to starboard (positive).
    assert crab_offset_deg(0.0, drift_east=-0.4, drift_north=0.0, water_speed_mps=1.0) > 0.0

    # A pure along-track drift (north) has no cross component -> ~no crab.
    assert abs(crab_offset_deg(0.0, 0.0, 0.4, 1.0)) < 1e-6

    # Bounded.
    assert crab_offset_deg(0.0, 5.0, 0.0, 1.0) == pytest.approx(-25.0, abs=0.01)


# --------------------------------------------------------------------------- #
# Waypoint crab feed-forward consumer
# --------------------------------------------------------------------------- #
def _waypoint_state_on_leg():
    target = destination_point(HERE, 100.0, 0.0)  # 100 m due north
    state = _state(heading=0.0)
    state.waypoints = [Waypoint("WP0", target)]
    return state


def test_waypoint_crabs_into_beam_drift_when_settled():
    state = _waypoint_state_on_leg()
    # Settled beam drift pushing EAST (to starboard of the northbound leg).
    state.est_drift_east = 0.4
    state.est_drift_north = 0.0
    state.est_drift_mps = 0.4
    state.est_drift_dir = 90.0
    state.est_drift_settled = True

    mode = WaypointMode(WaypointConfig(allow_reverse=False))
    mode.activate(state)
    sp = mode.update(state, 0.2)
    # Commanded heading biased upwind (to port) of the bearing (000).
    assert angle_difference(0.0, sp.target_heading) < -5.0


def test_waypoint_no_crab_until_settled():
    state = _waypoint_state_on_leg()
    # Same drift, but NOT settled -> pure feedback, heading ~= bearing (000).
    state.est_drift_east = 0.4
    state.est_drift_mps = 0.4
    state.est_drift_dir = 90.0
    state.est_drift_settled = False

    mode = WaypointMode(WaypointConfig(allow_reverse=False))
    mode.activate(state)
    sp = mode.update(state, 0.2)
    assert abs(angle_difference(0.0, sp.target_heading)) < 1.0


def test_waypoint_crab_tightens_ground_track_vs_no_feedforward():
    # With a beam set, the crab feed-forward should point the bow further upwind
    # than pure feedback would at the same (small) cross-track error.
    def commanded_heading(crab_on: bool) -> float:
        state = _waypoint_state_on_leg()
        # Small cross-track: nudge the boat 1 m east of the leg start.
        state.fix = GpsFix(point=destination_point(HERE, 1.0, 90.0))
        state.est_drift_east = 0.4
        state.est_drift_north = 0.0
        state.est_drift_mps = 0.4
        state.est_drift_dir = 90.0
        state.est_drift_settled = True
        mode = WaypointMode(WaypointConfig(allow_reverse=False, crab_feedforward=crab_on))
        mode.activate(state)
        return mode.update(state, 0.2).target_heading

    with_ff = angle_difference(0.0, commanded_heading(True))
    without_ff = angle_difference(0.0, commanded_heading(False))
    # Both steer to port (negative), but the feed-forward steers MORE upwind.
    assert with_ff < without_ff


# --------------------------------------------------------------------------- #
# Spot-Lock reads the estimate immediately on activate (no relearn delay)
# --------------------------------------------------------------------------- #
def test_spot_lock_uses_drift_estimate_immediately_on_activate():
    # Pre-load a settled drift on the state; Spot-Lock (feed-forward on) must point
    # the bow INTO it on the very first update -- no ~10 s relearn.
    state = _state(heading=0.0, sog=0.0)
    state.anchor = HERE  # boat sitting on the mark, station-keeping
    state.anchor_radius_m = 5.0
    state.est_drift_east = 0.5
    state.est_drift_north = 0.0
    state.est_drift_mps = 0.5
    state.est_drift_dir = 90.0  # set pushes east
    state.est_drift_settled = True

    mode = AnchorHoldMode(AnchorConfig(feedforward=True))
    mode.activate(state)  # must NOT clear the drift knowledge
    assert state.est_drift_mps == 0.5  # untouched by activate
    sp = mode.update(state, 0.2)
    # Bow points INTO the drift (opposite the push direction) -> ~270.
    assert abs(angle_difference(sp.target_heading, 270.0)) < 5.0
    assert sp.thrust > 0.0  # holding counter-thrust, not idle


def test_spot_lock_activate_does_not_reset_shared_estimate():
    # Belt-and-braces: activate() has no drift-reset side effect at all.
    state = _state()
    state.est_drift_east = 0.3
    state.est_drift_north = 0.1
    state.est_drift_mps = math.hypot(0.3, 0.1)
    state.est_drift_settled = True
    before = (state.est_drift_east, state.est_drift_north, state.est_drift_settled)
    AnchorHoldMode().activate(state)
    assert (state.est_drift_east, state.est_drift_north, state.est_drift_settled) == before
