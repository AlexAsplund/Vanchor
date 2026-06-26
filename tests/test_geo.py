import math

import pytest

from vanchor.core.geo import (
    angle_difference,
    cross_track,
    destination_point,
    haversine_m,
    initial_bearing,
    normalize_deg,
    offset_meters,
)
from vanchor.core.models import GeoPoint

ORIGIN = GeoPoint(59.0, 18.0)


def test_normalize_deg():
    assert normalize_deg(370) == 10
    assert normalize_deg(-10) == 350
    assert normalize_deg(0) == 0


@pytest.mark.parametrize(
    "a,b,expected",
    [(0, 10, 10), (10, 0, -10), (350, 10, 20), (10, 350, -20), (0, 180, 180)],
)
def test_angle_difference(a, b, expected):
    assert angle_difference(a, b) == pytest.approx(expected)


def test_haversine_known_distance():
    # 1 degree of latitude is ~111.2 km.
    d = haversine_m(GeoPoint(0, 0), GeoPoint(1, 0))
    assert d == pytest.approx(111195, rel=0.001)


def test_haversine_zero():
    assert haversine_m(ORIGIN, ORIGIN) == pytest.approx(0.0, abs=1e-6)


def test_initial_bearing_cardinals():
    assert initial_bearing(ORIGIN, GeoPoint(60.0, 18.0)) == pytest.approx(0, abs=0.1)
    assert initial_bearing(ORIGIN, GeoPoint(59.0, 19.0)) == pytest.approx(90, abs=0.5)


def test_destination_point_roundtrip():
    dest = destination_point(ORIGIN, 100.0, 45.0)
    assert haversine_m(ORIGIN, dest) == pytest.approx(100.0, rel=1e-3)
    assert initial_bearing(ORIGIN, dest) == pytest.approx(45.0, abs=0.1)


def test_offset_meters_matches_haversine():
    p = offset_meters(ORIGIN, 30.0, 40.0)  # east 30, north 40 => 50 m
    assert haversine_m(ORIGIN, p) == pytest.approx(50.0, rel=1e-3)


def test_cross_track_sign_and_magnitude():
    start = GeoPoint(0.0, 0.0)
    end = GeoPoint(0.0, 1.0)  # heading due east along the equator
    # A point north of the track is to the *left* of an eastbound leg.
    north = GeoPoint(0.001, 0.5)
    xt = cross_track(start, end, north)
    assert xt.distance_m < 0  # left of track => negative
    assert xt.steer_to == "R"
    assert abs(xt.distance_m) == pytest.approx(haversine_m(GeoPoint(0, 0.5), north), rel=0.02)

    south = GeoPoint(-0.001, 0.5)
    assert cross_track(start, end, south).steer_to == "L"


def test_cross_track_on_track_is_zero():
    start, end = GeoPoint(0.0, 0.0), GeoPoint(0.0, 1.0)
    on = GeoPoint(0.0, 0.5)
    assert cross_track(start, end, on).distance_m == pytest.approx(0.0, abs=0.5)
