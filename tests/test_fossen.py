import math

import pytest

from vanchor.core.geo import haversine_m, initial_bearing
from vanchor.core.models import BoatState, Environment, GeoPoint, MotorCommand
from vanchor.sim.fossen import FossenBoat, FossenParams

HERE = GeoPoint(59.3293, 18.0686)


def _run(boat: FossenBoat, command: MotorCommand, env: Environment, seconds: float, dt: float = 0.05):
    for _ in range(int(seconds / dt)):
        boat.step(dt, command, env)


def test_params_are_all_default_constructible():
    p = FossenParams()
    assert p.length == 4.1
    assert p.beam == 1.7
    assert p.mass == 300.0
    assert p.max_thrust_n == 250.0
    assert p.max_speed_mps == 1.6
    assert p.thruster_x_m == 1.7  # bow mount, forward of CG
    assert p.max_steer_angle_deg == 35.0
    # Derived: yaw inertia from rectangle geometry, surge drag from thrust/speed.
    assert p.iz == pytest.approx(300.0 / 12.0 * (4.1**2 + 1.7**2))
    assert p.x_u < 0.0


def test_full_thrust_moves_north_and_approaches_top_speed():
    boat = FossenBoat(BoatState(point=HERE, heading_deg=0.0), FossenParams())
    _run(boat, MotorCommand(thrust=1.0, steering=0.0), Environment(), seconds=20.0)
    # Moved a useful distance roughly due north.
    assert haversine_m(HERE, boat.state.point) > 10.0
    assert initial_bearing(HERE, boat.state.point) == pytest.approx(0.0, abs=2.0)
    # Top speed in the ballpark of the 4.1 m bow-mount boat (~1.5-1.7 m/s).
    assert 1.5 <= boat.state.speed_mps <= 1.7
    # Sway is essentially zero in a straight line.
    assert abs(boat.sway_mps) < 0.01


def test_reaches_most_of_top_speed_within_a_few_seconds():
    boat = FossenBoat(BoatState(point=HERE), FossenParams())
    _run(boat, MotorCommand(thrust=1.0), Environment(), seconds=5.0)
    fast = boat.surge_mps
    _run(boat, MotorCommand(thrust=1.0), Environment(), seconds=30.0)
    top = boat.surge_mps
    # >85% of top speed within ~5 s.
    assert fast > 0.85 * top


def test_full_steering_produces_sustained_turn_to_starboard():
    boat = FossenBoat(BoatState(point=HERE, heading_deg=0.0), FossenParams())
    # Build up speed, then turn hard.
    _run(boat, MotorCommand(thrust=1.0, steering=0.0), Environment(), seconds=5.0)
    h0 = boat.state.heading_deg
    _run(boat, MotorCommand(thrust=1.0, steering=1.0), Environment(), seconds=6.0)
    # Sustained turn rate settles in the 12-25 deg/s range.
    rate = boat.yaw_rate_dps
    assert 12.0 < rate < 25.0
    # Bow mount + positive steering -> turn to starboard (heading increases).
    assert rate > 0.0
    assert boat.state.heading_deg > h0


def test_sway_appears_during_turn():
    boat = FossenBoat(BoatState(point=HERE, heading_deg=0.0), FossenParams())
    _run(boat, MotorCommand(thrust=1.0, steering=0.0), Environment(), seconds=5.0)
    assert abs(boat.sway_mps) < 0.01
    _run(boat, MotorCommand(thrust=1.0, steering=1.0), Environment(), seconds=3.0)
    # The boat visibly crabs/sways during the turn.
    assert abs(boat.sway_mps) > 0.05


def test_zero_thrust_full_steering_does_not_turn():
    """The bow-mount / vectored-thrust signature: with no thrust there is no
    force and therefore no yaw moment, so the boat cannot steer at all."""
    boat = FossenBoat(BoatState(point=HERE, heading_deg=0.0), FossenParams())
    _run(boat, MotorCommand(thrust=0.0, steering=1.0), Environment(), seconds=5.0)
    assert abs(boat.state.heading_deg) < 3.0
    assert boat.yaw_rate_dps == pytest.approx(0.0, abs=1e-9)


def test_negative_steering_turns_to_port():
    boat = FossenBoat(BoatState(point=HERE, heading_deg=90.0), FossenParams())
    _run(boat, MotorCommand(thrust=1.0, steering=0.0), Environment(), seconds=5.0)
    h0 = boat.state.heading_deg
    _run(boat, MotorCommand(thrust=1.0, steering=-1.0), Environment(), seconds=4.0)
    # Bow mount + negative steering -> turn to port (heading decreases).
    assert boat.yaw_rate_dps < 0.0
    assert boat.state.heading_deg < h0


def test_reverse_thrust_drives_backwards():
    boat = FossenBoat(BoatState(point=HERE, heading_deg=0.0), FossenParams())
    _run(boat, MotorCommand(thrust=-1.0, steering=0.0), Environment(), seconds=10.0)
    # Negative thrust -> negative surge (moving astern / south).
    assert boat.surge_mps < 0.0
    assert initial_bearing(HERE, boat.state.point) == pytest.approx(180.0, abs=2.0)


def test_current_drift_moves_idle_boat():
    boat = FossenBoat(BoatState(point=HERE, heading_deg=0.0), FossenParams())
    env = Environment(current_speed=1.0, current_dir=90.0)  # flowing east
    _run(boat, MotorCommand(thrust=0.0, steering=0.0), env, seconds=5.0)
    assert haversine_m(HERE, boat.state.point) == pytest.approx(5.0, rel=0.1)
    assert initial_bearing(HERE, boat.state.point) == pytest.approx(90.0, abs=2.0)


def test_zero_dt_is_noop():
    boat = FossenBoat(BoatState(point=HERE), FossenParams())
    boat.step(0.0, MotorCommand(thrust=1.0, steering=1.0), Environment())
    assert boat.state.point == HERE
    assert boat.state.heading_deg == 0.0
    assert boat.surge_mps == 0.0


def test_truth_is_a_snapshot():
    boat = FossenBoat(BoatState(point=HERE), FossenParams())
    _run(boat, MotorCommand(thrust=1.0), Environment(), seconds=2.0)
    t = boat.truth()
    p_before = t.point
    _run(boat, MotorCommand(thrust=1.0), Environment(), seconds=2.0)
    # The earlier snapshot is unaffected by subsequent stepping.
    assert t.point == p_before
