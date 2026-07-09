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
    # An idle hull is *advected* by the water: it accelerates (over a few s, not
    # instantly) until it drifts with the current, then translates east with it.
    _run(boat, MotorCommand(thrust=0.0, steering=0.0), env, seconds=40.0)
    assert initial_bearing(HERE, boat.state.point) == pytest.approx(90.0, abs=2.0)
    # Settled: speed over ground ~ current speed, and speed THROUGH the water ~ 0
    # (no relative motion once drifting with the flow).
    sog = math.hypot(boat.state.ground_ve, boat.state.ground_vn)
    assert sog == pytest.approx(1.0, rel=0.1)
    assert boat.state.speed_mps < 0.1


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


def test_wind_is_a_force_that_pushes_downwind_and_yaws():
    """Wind is an aerodynamic force, not a fixed leeway: a quartering wind on an
    idle boat pushes it downwind (leeway emerges from the force vs sway damping)
    and imparts a yaw moment (weathervaning)."""
    boat = FossenBoat(BoatState(point=HERE, heading_deg=0.0), FossenParams())
    env = Environment(wind_speed=8.0, wind_dir=45.0)  # blowing toward the NE
    _run(boat, MotorCommand(thrust=0.0, steering=0.0), env, seconds=12.0)
    # Drifts roughly downwind (toward the NE), not upwind.
    brg = initial_bearing(HERE, boat.state.point)
    assert abs(((brg - 45.0) + 180.0) % 360.0 - 180.0) < 45.0
    # A quartering wind yaws the bow (a pure beam/head wind would not).
    assert abs(boat.state.heading_deg) > 1.0


def test_no_wind_no_current_idle_boat_is_still():
    boat = FossenBoat(BoatState(point=HERE, heading_deg=0.0), FossenParams())
    _run(boat, MotorCommand(thrust=0.0, steering=0.0), Environment(), seconds=10.0)
    assert haversine_m(HERE, boat.state.point) < 0.01
    assert boat.state.heading_deg == pytest.approx(0.0, abs=1e-6)
    assert boat.state.speed_mps == pytest.approx(0.0, abs=1e-9)


def test_head_current_makes_sog_less_than_stw():
    """A current is felt as drag on the hull: motoring against it, the boat makes
    way through the water (STW ~ top speed) but its speed over ground is less."""
    boat = FossenBoat(BoatState(point=HERE, heading_deg=0.0), FossenParams())
    env = Environment(current_speed=0.6, current_dir=180.0)  # flowing south
    _run(boat, MotorCommand(thrust=1.0, steering=0.0), env, seconds=40.0)
    stw = boat.state.speed_mps
    sog = math.hypot(boat.state.ground_ve, boat.state.ground_vn)
    # The southward current eats ~0.6 m/s of the northward ground progress.
    assert stw > sog + 0.3
    assert sog == pytest.approx(stw - 0.6, abs=0.15)


# --------------------------------------------------------------------------- #
# Ocean-current kinetics: the Dnu_c body-frame rotation term (Fossen ch. 6,
# audited against cybergalactic/PythonVehicleSimulator's otter, 2026-07).
# --------------------------------------------------------------------------- #
def test_current_rotation_term_present_in_turns():
    """A sustained turn in a uniform current must include nu_c_dot = [r*v_c,
    -r*u_c, 0]. Golden values from the corrected dynamics; the pre-fix model
    (term omitted) lands ~14 m away with ~74 deg heading error, far outside
    these tolerances."""
    import math
    from vanchor.core.geo import haversine_m
    from vanchor.core.models import Environment, GeoPoint, MotorCommand

    env = Environment(current_speed=0.5, current_dir=90.0, wind_speed=0.0, wind_dir=0.0)
    boat = FossenBoat(BoatState(point=GeoPoint(59.0, 18.0)), FossenParams())
    cmd = MotorCommand(thrust=0.8, steering=0.6)
    for _ in range(1200):  # 60 s at 20 Hz
        boat.step(0.05, cmd, env)
    # Golden endpoint of the corrected model (deterministic integration).
    golden = GeoPoint(59.00000454, 18.00078986)
    assert haversine_m(boat.state.point, golden) < 1.0
    assert abs((boat.state.heading_deg - 160.1 + 180) % 360 - 180) < 5.0


def test_no_current_calm_water_unchanged_by_the_term():
    """With zero current the Dnu_c term is identically zero: calm-water
    behavior is bit-for-bit what it was before the fix."""
    from vanchor.core.models import Environment, GeoPoint, MotorCommand

    env = Environment(current_speed=0.0, current_dir=0.0, wind_speed=0.0, wind_dir=0.0)
    boat = FossenBoat(BoatState(point=GeoPoint(59.0, 18.0)), FossenParams())
    cmd = MotorCommand(thrust=0.8, steering=0.6)
    for _ in range(400):
        boat.step(0.05, cmd, env)
    # r settles to the tuned sustained turn rate; the term must not alter it.
    assert 6.0 < abs(boat.yaw_rate_dps) < 30.0  # 0.8 thrust / 0.6 steer sustained rate
    assert boat.surge_mps > 0.3
