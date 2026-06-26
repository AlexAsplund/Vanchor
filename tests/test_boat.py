import pytest

from vanchor.core.geo import haversine_m, initial_bearing
from vanchor.core.models import BoatState, Environment, GeoPoint, MotorCommand
from vanchor.sim.boat import Boat, BoatParams

HERE = GeoPoint(59.3293, 18.0686)


def test_full_thrust_moves_north_when_heading_north():
    boat = Boat(BoatState(point=HERE, heading_deg=0.0), BoatParams())
    for _ in range(200):  # 10 s at dt=0.05
        boat.step(0.05, MotorCommand(thrust=1.0, steering=0.0), Environment())
    moved = haversine_m(HERE, boat.state.point)
    assert moved > 5.0
    assert initial_bearing(HERE, boat.state.point) == pytest.approx(0.0, abs=2.0)


def test_speed_approaches_max():
    boat = Boat(BoatState(point=HERE), BoatParams(max_speed_mps=1.6))
    for _ in range(400):
        boat.step(0.05, MotorCommand(thrust=1.0), Environment())
    assert boat.state.speed_mps == pytest.approx(1.6, rel=0.05)


def test_steering_changes_heading():
    # Steering needs thrust for authority (a trolling motor can't steer without
    # running), so command full thrust + full steering.
    boat = Boat(BoatState(point=HERE, heading_deg=0.0), BoatParams(max_turn_rate_deg=25))
    boat.step(1.0, MotorCommand(thrust=1.0, steering=1.0), Environment())
    assert boat.state.heading_deg == pytest.approx(25.0, abs=0.1)


def test_no_steering_authority_without_thrust():
    # With zero thrust the boat must not turn, even with full steering.
    boat = Boat(BoatState(point=HERE, heading_deg=10.0), BoatParams())
    boat.step(1.0, MotorCommand(thrust=0.0, steering=1.0), Environment())
    assert boat.state.heading_deg == pytest.approx(10.0, abs=1e-9)


def test_current_drift_moves_idle_boat():
    boat = Boat(BoatState(point=HERE, heading_deg=0.0), BoatParams())
    env = Environment(current_speed=1.0, current_dir=90.0)  # flowing east
    for _ in range(100):  # 5 s
        boat.step(0.05, MotorCommand(thrust=0.0, steering=0.0), env)
    assert haversine_m(HERE, boat.state.point) == pytest.approx(5.0, rel=0.1)
    assert initial_bearing(HERE, boat.state.point) == pytest.approx(90.0, abs=2.0)


def test_zero_dt_is_noop():
    boat = Boat(BoatState(point=HERE), BoatParams())
    boat.step(0.0, MotorCommand(thrust=1.0), Environment())
    assert boat.state.point == HERE
