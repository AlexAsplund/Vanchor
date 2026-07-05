"""Deterministic tests for the GNSS/INS complementary fusion filter.

All timing is injected (``now``/``dt``); no test reads a real clock, so the
filter's behaviour is fully reproducible.
"""

from __future__ import annotations

import math

from vanchor.core.geo import haversine_m
from vanchor.core.models import GeoPoint
from vanchor.nav.fusion import NavFusion


def test_constant_compass_zero_gyro_holds_heading() -> None:
    """Constant compass + zero yaw rate -> fused heading equals the compass."""
    fusion = NavFusion(heading_gain=0.2)
    fusion.update_compass(90.0)  # seeds directly
    for _ in range(20):
        fusion.update_imu(0.0, dt=0.1)
        fusion.update_compass(90.0)
    state = fusion.step(now=2.0)
    assert state.heading_deg is not None
    assert math.isclose(state.heading_deg, 90.0, abs_tol=1e-9)
    assert state.yaw_rate_dps == 0.0


def test_gyro_integrates_then_compass_nudges_back() -> None:
    """Gyro advances the heading; a compass update pulls it partway back."""
    fusion = NavFusion(heading_gain=0.1)
    fusion.update_compass(0.0)  # seed at 0
    # Integrate +10 deg/s for 1 s across ten 0.1 s ticks -> heading ~ 10 deg.
    for _ in range(10):
        fusion.update_imu(10.0, dt=0.1)
    integrated = fusion.step(now=1.0).heading_deg
    assert integrated is not None
    assert math.isclose(integrated, 10.0, abs_tol=1e-6)

    # A compass reading of 0 nudges the heading back by gain * error.
    fusion.update_compass(0.0)
    nudged = fusion.step(now=1.0).heading_deg
    assert nudged is not None
    # error = angle_difference(10, 0) = -10; new = 10 + 0.1*(-10) = 9.
    assert math.isclose(nudged, 9.0, abs_tol=1e-6)
    assert 0.0 < nudged < integrated  # moved toward the compass, not all the way


def test_no_imu_heading_tracks_compass_and_yaw_rate_none() -> None:
    """Without any IMU sample, heading tracks the compass and yaw rate is None."""
    fusion = NavFusion(heading_gain=0.5)
    fusion.update_compass(120.0)
    state = fusion.step(now=0.0)
    assert state.heading_deg == 120.0
    assert state.yaw_rate_dps is None


def test_ground_velocity_converges_to_known_vel() -> None:
    """Low-pass converges to an explicit (vel_n, vel_e); sog ~ hypot(vn, ve)."""
    fusion = NavFusion(vel_tau_s=0.5)
    origin = GeoPoint(59.0, 18.0)
    # Seed the velocity at zero (a stationary first fix), then feed (3, 4).
    fusion.update_gps(origin, now=0.0, vel_n_mps=0.0, vel_e_mps=0.0)
    now = 0.0
    for _ in range(200):
        now += 0.1
        fusion.update_gps(origin, now=now, vel_n_mps=3.0, vel_e_mps=4.0)
    state = fusion.step(now=now)
    assert state.ground_vel_n_mps is not None
    assert state.ground_vel_e_mps is not None
    assert math.isclose(state.ground_vel_n_mps, 3.0, abs_tol=1e-3)
    assert math.isclose(state.ground_vel_e_mps, 4.0, abs_tol=1e-3)
    assert state.sog_mps is not None
    assert math.isclose(state.sog_mps, 5.0, abs_tol=1e-3)


def test_velocity_derived_from_position_delta() -> None:
    """With no velocity given, the filter derives it from position deltas."""
    fusion = NavFusion(vel_tau_s=0.5)
    start = GeoPoint(59.0, 18.0)
    fusion.update_gps(start, now=0.0)  # first fix: no velocity derivable yet
    # Move ~1 m north each fix (0.1 s apart) -> ~10 m/s north.
    from vanchor.core.geo import offset_meters

    p = start
    now = 0.0
    for _ in range(300):
        now += 0.1
        p = offset_meters(p, east_m=0.0, north_m=1.0)
        fusion.update_gps(p, now=now)
    state = fusion.step(now=now)
    assert state.ground_vel_n_mps is not None
    assert math.isclose(state.ground_vel_n_mps, 10.0, abs_tol=0.05)
    assert state.ground_vel_e_mps is not None
    assert abs(state.ground_vel_e_mps) < 0.05


def test_crab_positive_when_track_is_to_starboard() -> None:
    """Bow north, track NE -> crab ~ +45 deg (course is to starboard of heading).

    Sign convention: crab_deg = angle_difference(heading, course); positive means
    the boat's track lies clockwise (to starboard) of where the bow points.
    """
    fusion = NavFusion()
    fusion.update_compass(0.0)  # heading north
    # Velocity to the north-east: vel_n = vel_e = 5 -> course 045.
    origin = GeoPoint(59.0, 18.0)
    fusion.update_gps(origin, now=0.0, vel_n_mps=5.0, vel_e_mps=5.0)
    state = fusion.step(now=0.0)
    assert state.crab_deg is not None
    assert math.isclose(state.crab_deg, 45.0, abs_tol=1e-6)


def test_crab_none_below_min_sog() -> None:
    """Crab is undefined (None) when below the (measured-velocity) threshold."""
    fusion = NavFusion(crab_min_sog_measured_mps=0.5)  # this fix carries a vector
    fusion.update_compass(0.0)
    origin = GeoPoint(59.0, 18.0)
    fusion.update_gps(origin, now=0.0, vel_n_mps=0.1, vel_e_mps=0.1)  # sog ~0.14
    state = fusion.step(now=0.0)
    assert state.sog_mps is not None and state.sog_mps < 0.5
    assert state.crab_deg is None


def test_measured_velocity_unlocks_low_speed_crab() -> None:
    """The capability a real 3D velocity unlocks: at a low speed where a DERIVED
    velocity yields no crab, a MEASURED velocity vector still does."""
    origin = GeoPoint(59.0, 18.0)
    # Derived (SOG/COG) at 0.2 m/s -> below the 0.3 derived threshold -> None.
    f1 = NavFusion()
    f1.update_compass(0.0)
    f1.update_gps(origin, now=0.0, sog_mps=0.2, cog_deg=45.0)
    s1 = f1.step(now=0.0)
    assert s1.crab_deg is None and s1.velocity_measured is False
    # Same 0.2 m/s as a measured vector -> above the 0.05 measured threshold -> crab.
    f2 = NavFusion()
    f2.update_compass(0.0)
    f2.update_gps(origin, now=0.0,
                  vel_n_mps=0.2 * math.cos(math.radians(45)),
                  vel_e_mps=0.2 * math.sin(math.radians(45)))
    s2 = f2.step(now=0.0)
    assert s2.velocity_measured is True
    assert s2.crab_deg is not None and abs(s2.crab_deg - 45.0) < 1.0


def test_crab_none_without_heading() -> None:
    """Crab needs a heading; without a compass it stays None though velocity works."""
    fusion = NavFusion()
    origin = GeoPoint(59.0, 18.0)
    fusion.update_gps(origin, now=0.0, vel_n_mps=5.0, vel_e_mps=5.0)
    state = fusion.step(now=0.0)
    assert state.heading_deg is None
    assert state.crab_deg is None
    assert state.sog_mps is not None  # velocity still available


def test_dead_reckoning_engages_and_advances_position() -> None:
    """After dr_timeout with no GPS, position coasts along the velocity vector."""
    fusion = NavFusion(dr_timeout_s=2.0)
    origin = GeoPoint(59.0, 18.0)
    # Moving due east at 4 m/s (vel_n = 0, vel_e = 4).
    fusion.update_gps(origin, now=0.0, vel_n_mps=0.0, vel_e_mps=4.0)

    # Within the timeout: not dead reckoning yet, position is the last fix.
    live = fusion.step(now=1.5)
    assert live.dead_reckoning is False
    assert live.position == origin

    # Past the timeout: dead reckoning, position advanced by ~vel * elapsed.
    dr = fusion.step(now=10.0)
    assert dr.dead_reckoning is True
    assert dr.position is not None
    displacement = haversine_m(origin, dr.position)
    expected = 4.0 * 10.0  # 40 m east over 10 s
    assert math.isclose(displacement, expected, rel_tol=1e-3)
    # Displacement is essentially due east (longitude increased, latitude ~same).
    assert math.isclose(dr.position.lat, origin.lat, abs_tol=1e-6)
    assert dr.position.lon > origin.lon

    # A fresh fix clears dead reckoning.
    fusion.update_gps(dr.position, now=11.0, vel_n_mps=0.0, vel_e_mps=4.0)
    cleared = fusion.step(now=11.0)
    assert cleared.dead_reckoning is False
