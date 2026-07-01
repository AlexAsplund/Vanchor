"""Tests for the "around island" loop route (#77).

Builds a synthetic basin -- a square lake with a square island *hole* -- and
exercises the loop planner plus the loop-following behaviour of WaypointMode.
"""

import math

from shapely.geometry import LineString, MultiPolygon, Point, Polygon

from vanchor.controller.modes import WaypointMode
from vanchor.core.geo import destination_point
from vanchor.core.models import GeoPoint, GpsFix, Waypoint
from vanchor.core.state import NavigationState
from vanchor.nav import routing, water

LAT0, LON0 = 59.0, 13.0


def _square(lat: float, lon: float, half_lat: float, half_lon: float):
    return [
        (lon - half_lon, lat - half_lat),
        (lon + half_lon, lat - half_lat),
        (lon + half_lon, lat + half_lat),
        (lon - half_lon, lat + half_lat),
    ]


def _m_to_deg(lat: float, dist_m: float):
    dlat = dist_m / 111_320.0
    dlon = dist_m / (111_320.0 * math.cos(math.radians(lat)))
    return dlat, dlon


def _basin_with_island(basin_half_m=500.0, island_half_m=40.0):
    """A square lake centred on (LAT0, LON0) with a centred square island hole."""
    bdlat, bdlon = _m_to_deg(LAT0, basin_half_m)
    idlat, idlon = _m_to_deg(LAT0, island_half_m)
    ext = _square(LAT0, LON0, bdlat, bdlon)
    hole = _square(LAT0, LON0, idlat, idlon)
    return MultiPolygon([Polygon(ext, [hole])]), hole


def _boat_pos():
    # A point well inside the lake but off the island.
    dlat, _ = _m_to_deg(LAT0, 250.0)
    return LAT0 - dlat, LON0


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #
def test_island_click_yields_closed_loop_in_water():
    water_ll, hole = _basin_with_island()
    boat_lat, boat_lon = _boat_pos()
    res = routing.plan_island_loop(
        LAT0, LON0, water_ll, boat_lat=boat_lat, boat_lon=boat_lon, offset_m=20.0
    )
    assert res.ok, res.message
    assert res.loop is True
    assert len(res.waypoints) >= 4

    proj = water.Projection.for_point(LON0, LAT0)
    nav = proj.to_metric(water_ll).buffer(1.0)
    pts = [proj.point_to_metric(w["lon"], w["lat"]) for w in res.waypoints]

    # Closed: the last waypoint returns to the first.
    assert Point(pts[0]).distance(Point(pts[-1])) < 1.0

    # Every waypoint -- and every leg between them -- stays on the water.
    assert all(nav.covers(Point(p)) for p in pts)
    prev = None
    for p in pts:
        if prev is not None:
            assert nav.covers(LineString([prev, p]))
        prev = p

    # The loop encircles the island: the island centroid is inside the polygon
    # traced by the waypoints.
    island_m = proj.to_metric(Polygon(hole))
    assert Polygon(pts).contains(island_m.centroid)


def test_open_water_click_is_rejected():
    water_ll, _ = _basin_with_island()
    boat_lat, boat_lon = _boat_pos()
    # Click on the boat's own (open water) position, not the island.
    res = routing.plan_island_loop(
        boat_lat, boat_lon, water_ll, boat_lat=boat_lat, boat_lon=boat_lon
    )
    assert not res.ok
    assert "island" in res.message.lower()


def test_click_outside_water_body_is_rejected():
    water_ll, _ = _basin_with_island()
    boat_lat, boat_lon = _boat_pos()
    # Far away on the mainland / outside the basin entirely.
    res = routing.plan_island_loop(
        60.0, 14.0, water_ll, boat_lat=boat_lat, boat_lon=boat_lon
    )
    assert not res.ok


def test_island_loop_no_off_water_waypoints_when_ring_clips():
    """20 m offset ring clips ~1.4% at the north basin wall — all waypoints must stay on water.

    Geometry (metric, built via UTM projection so distances are exact):
      - Basin: 500 m half-square.
      - Island: circle, radius 200 m, centred 280.22 m north of the lake centre.
      - 20 m offset ring radius 220 m → north edge at basin_north + 0.22 m (~1.4% outside).
      - 15 m offset (factor 0.75) ring radius 215 m → north edge at basin_north − 4.78 m (100% inside).

    With the OLD threshold (0.98), factor=1.0 was accepted and the UNCLIPPED ring
    (with its north vertex 0.22 m outside the basin) was passed downstream.
    With the FIX (threshold 0.9999), factor=1.0 is rejected and factor=0.75 is
    used — every returned waypoint (and leg midpoint) must lie inside navigable water.
    """
    proj = water.Projection.for_point(LON0, LAT0)
    x0, y0 = proj.point_to_metric(LON0, LAT0)

    bh = 500.0  # basin half-side (metres)
    basin_m = Polygon([
        (x0 - bh, y0 - bh), (x0 + bh, y0 - bh),
        (x0 + bh, y0 + bh), (x0 - bh, y0 + bh),
    ])
    # Circle of radius 200 m; 20 m ring → north edge = y0 + 280.22 + 220 = y0 + 500.22
    island_cy = y0 + 280.22
    island_m = Point(x0, island_cy).buffer(200.0, quad_segs=64)

    def _to_ll(poly):
        return [proj.point_to_lonlat(px, py) for (px, py) in poly.exterior.coords]

    water_ll = MultiPolygon([Polygon(_to_ll(basin_m), [_to_ll(island_m)])])
    click_lon, click_lat = proj.point_to_lonlat(x0, island_cy)
    boat_lon, boat_lat = proj.point_to_lonlat(x0, y0 - 200.0)

    res = routing.plan_island_loop(
        click_lat, click_lon, water_ll,
        boat_lat=boat_lat, boat_lon=boat_lon, offset_m=20.0,
    )
    assert res.ok, res.message
    assert res.loop is True

    # Every waypoint and every leg midpoint must be inside navigable water.
    nav = proj.to_metric(water_ll).buffer(1.0)
    pts = [proj.point_to_metric(w["lon"], w["lat"]) for w in res.waypoints]
    assert all(nav.covers(Point(p)) for p in pts), (
        "Some waypoints are outside navigable water — the unclipped ring was returned"
    )
    for a, b in zip(pts, pts[1:]):
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        assert nav.covers(Point(mid)), "A leg segment crosses non-water area"


def test_offset_shrinks_when_island_is_tight():
    # Island nearly fills a small basin: a 20 m offset won't fit, so the planner
    # shrinks it and says so (or rejects if even that won't fit).
    water_ll, _ = _basin_with_island(basin_half_m=60.0, island_half_m=48.0)
    boat_lat, boat_lon = _boat_pos()
    res = routing.plan_island_loop(
        LAT0, LON0, water_ll, boat_lat=boat_lat, boat_lon=boat_lon, offset_m=20.0
    )
    if res.ok:
        assert "shrunk" in res.message.lower()
        assert res.loop
    else:
        assert "close" in res.message.lower() or "navigable" in res.message.lower()


# --------------------------------------------------------------------------- #
# Loop following
# --------------------------------------------------------------------------- #
HERE = GeoPoint(59.3293, 18.0686)


def _state_at(point, heading=0.0):
    s = NavigationState()
    s.fix = GpsFix(point=point)
    s.heading_deg = heading
    return s


def test_loop_following_wraps_back_to_start():
    wp0 = destination_point(HERE, 3.0, 90.0)
    wp1 = destination_point(HERE, 6.0, 90.0)
    state = _state_at(HERE)
    state.waypoints = [Waypoint("WP0", wp0), Waypoint("WP1", wp1)]
    state.route_loop = True
    mode = WaypointMode()
    mode.activate(state)

    # Arrive at WP0 -> advance to WP1.
    state.fix = GpsFix(point=wp0)
    mode.update(state, 0.2)
    assert state.active_waypoint == 1

    # Arrive at WP1 (the last) with loop set -> wrap back to 0, NOT complete.
    state.fix = GpsFix(point=wp1)
    sp = mode.update(state, 0.2)
    assert state.active_waypoint == 0
    assert not state.route_complete
    assert sp.thrust > 0  # still driving, not idling


def test_non_loop_route_still_completes():
    wp0 = destination_point(HERE, 3.0, 90.0)
    state = _state_at(HERE)
    state.waypoints = [Waypoint("WP0", wp0)]
    state.route_loop = False
    mode = WaypointMode()
    mode.activate(state)
    state.fix = GpsFix(point=wp0)
    sp = mode.update(state, 0.2)
    assert state.active_waypoint == 1
    assert state.route_complete
    assert sp.thrust == 0.0
