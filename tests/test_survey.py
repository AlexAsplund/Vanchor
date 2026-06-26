"""Tests for the area-survey "map mode" boustrophedon coverage planner (#47)."""

import math

from shapely.geometry import Point, Polygon

from vanchor.nav import survey
from vanchor.nav.water import Projection


def _rect_latlon(lat0, lon0, width_m, height_m):
    """A metric rectangle (width east, height north) as [lat,lon] corners."""
    proj = Projection.for_point(lon0, lat0)
    x0, y0 = proj.point_to_metric(lon0, lat0)
    corners_m = [
        (x0, y0),
        (x0 + width_m, y0),
        (x0 + width_m, y0 + height_m),
        (x0, y0 + height_m),
    ]
    out = []
    for x, y in corners_m:
        lon, lat = proj.point_to_lonlat(x, y)
        out.append([lat, lon])
    return out, proj


def test_rectangle_pass_count_and_inside():
    # 100 m (E) x 50 m (N) rectangle, 10 m spacing. Longest axis is east-west,
    # so passes stack along the 50 m (north) dimension: ceil(50/10) = 5 passes
    # inset half a spacing from the edges (at 5,15,25,35,45 m).
    poly_latlon, proj = _rect_latlon(59.66, 13.32, 100.0, 50.0)
    res = survey.plan_survey(poly_latlon, spacing_m=10.0)
    assert res.ok, res.message
    assert res.passes == 5
    assert len(res.waypoints) == 10  # two endpoints per pass

    # Every waypoint must lie inside (or on) the polygon.
    poly_m = Polygon([proj.point_to_metric(lon, lat) for lat, lon in poly_latlon])
    nav = poly_m.buffer(0.5)
    for wp in res.waypoints:
        x, y = proj.point_to_metric(wp["lon"], wp["lat"])
        assert nav.covers(Point(x, y)), f"{wp} outside polygon"

    assert res.waypoints[0]["name"] == "WP1"
    assert res.waypoints[-1]["name"] == "DEST"


def test_passes_alternate_direction():
    # Force the sweep east-west so passes run along x; check boustrophedon flip.
    poly_latlon, proj = _rect_latlon(59.66, 13.32, 120.0, 60.0)
    res = survey.plan_survey(poly_latlon, spacing_m=15.0, angle_deg=0.0)
    assert res.ok, res.message

    # Convert waypoints back to metric and look at the x-direction of each pass
    # (pairs of consecutive waypoints form a pass). Adjacent passes must point
    # in opposite x-directions.
    xs = [proj.point_to_metric(wp["lon"], wp["lat"]) for wp in res.waypoints]
    pass_dirs = []
    for i in range(0, len(xs) - 1, 2):
        dx = xs[i + 1][0] - xs[i][0]
        pass_dirs.append(math.copysign(1.0, dx))
    assert len(pass_dirs) >= 3
    for a, b in zip(pass_dirs, pass_dirs[1:]):
        assert a != b, "consecutive passes should alternate direction"


def test_default_sweep_is_longest_axis():
    # A long thin rectangle (200 m E x 20 m N). The default sweep follows the
    # long axis, so passes stack along the short (20 m) axis: few passes.
    poly_latlon, _ = _rect_latlon(59.66, 13.32, 200.0, 20.0)
    res = survey.plan_survey(poly_latlon, spacing_m=10.0)
    assert res.ok, res.message
    # 20 m / 10 m -> ~2-3 passes. If it had swept the short way (200 m) it would
    # have produced ~20 passes, so a small count proves the default longest-axis
    # sweep direction.
    assert res.passes <= 3


def test_large_survey_warns_but_is_not_rejected():
    # Mapping a big area at tight spacing legitimately makes many passes; it must
    # still plan (just warn), not get rejected at a low hard cap.
    poly_latlon, _ = _rect_latlon(59.66, 13.32, 1000.0, 1000.0)
    res = survey.plan_survey(poly_latlon, spacing_m=1.0)
    assert res.ok, res.message
    assert len(res.waypoints) > survey.WARN_WAYPOINTS
    assert "heads up" in res.message.lower() or "a lot" in res.message.lower()


def test_pathological_survey_is_capped_with_message():
    # Tiny spacing on a big area exceeds the absolute guard -> rejected cleanly.
    poly_latlon, _ = _rect_latlon(59.66, 13.32, 2000.0, 2000.0)
    res = survey.plan_survey(poly_latlon, spacing_m=0.5)
    assert not res.ok
    assert "too many" in res.message.lower()
    assert res.waypoints == []


def test_bad_spacing_rejected():
    poly_latlon, _ = _rect_latlon(59.66, 13.32, 100.0, 100.0)
    assert not survey.plan_survey(poly_latlon, spacing_m=0.0).ok
    assert not survey.plan_survey(poly_latlon, spacing_m=-5.0).ok


def test_too_few_points_rejected():
    res = survey.plan_survey([[59.66, 13.32], [59.661, 13.321]], spacing_m=10.0)
    assert not res.ok
    assert "3" in res.message


def test_l_shaped_polygon_waypoints_inside():
    # A non-convex (L-shaped) area: passes must be clipped to the polygon so no
    # waypoint lands in the notch.
    proj = Projection.for_point(13.32, 59.66)
    x0, y0 = proj.point_to_metric(13.32, 59.66)
    l_m = [
        (x0, y0),
        (x0 + 100, y0),
        (x0 + 100, y0 + 40),
        (x0 + 40, y0 + 40),
        (x0 + 40, y0 + 100),
        (x0, y0 + 100),
    ]
    poly_latlon = []
    for x, y in l_m:
        lon, lat = proj.point_to_lonlat(x, y)
        poly_latlon.append([lat, lon])

    res = survey.plan_survey(poly_latlon, spacing_m=10.0)
    assert res.ok, res.message
    poly_m = Polygon(l_m)
    nav = poly_m.buffer(0.5)
    for wp in res.waypoints:
        x, y = proj.point_to_metric(wp["lon"], wp["lat"])
        assert nav.covers(Point(x, y)), f"{wp} outside L-polygon notch"
