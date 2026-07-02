"""Depth-aware routing (#30): cost the water router with the depth grid /
imported contours so routes proactively go AROUND shallow water instead of
relying only on the reactive shallow-stop.

Covers the depth.py shallow-mask helper and the routing.py hard-avoidance /
graceful-fallback behaviour end-to-end.
"""

import time

from shapely.geometry import LineString, MultiPolygon, Point, Polygon

from vanchor.nav import routing
from vanchor.nav.depth import DepthMap

# A simple rectangular lake (lon/lat). ~2.2 km N-S x ~2.3 km E-W near 59N/13E.
LAT_S, LAT_N = 59.00, 59.02
LON_W, LON_E = 13.00, 13.04
BBOX = (LON_W - 0.005, LAT_S - 0.005, LON_E + 0.005, LAT_N + 0.005)  # w,s,e,n


def _lake() -> MultiPolygon:
    return MultiPolygon([
        Polygon([(LON_W, LAT_S), (LON_E, LAT_S), (LON_E, LAT_N), (LON_W, LAT_N)])
    ])


def _seed_bar(dm: DepthMap, *, lon=13.020, lat_lo=59.008, lat_hi=59.020,
              depth=0.5, step_deg=0.00006) -> None:
    """A vertical wall of shallow soundings from the NORTH shore down to
    ``lat_lo`` -- leaving a navigable gap along the SOUTH shore."""
    la = lat_lo
    while la <= lat_hi + 1e-9:
        dm.points.append((round(la, 6), lon, depth))
        la += step_deg


def _route_line(start_lat, start_lon, waypoints) -> LineString:
    coords = [(start_lon, start_lat)] + [(w["lon"], w["lat"]) for w in waypoints]
    return LineString(coords)


# --------------------------------------------------------------------------- #
# depth.py: shallow-mask helper
# --------------------------------------------------------------------------- #
def test_shallow_polygons_none_without_data():
    """No soundings and no contours -> no mask (never a false obstacle)."""
    dm = DepthMap()
    assert dm.shallow_polygons(BBOX, min_depth_m=2.0) is None


def test_shallow_polygons_ignores_composition():
    """Composition is bottom HARDNESS, not depth -- it must never seed the mask."""
    dm = DepthMap()
    dm.composition = [
        {"pct": 90.0, "ring": [[59.01, 13.01], [59.01, 13.02], [59.015, 13.02]]}
    ]
    assert dm.shallow_polygons(BBOX, min_depth_m=2.0) is None


def test_shallow_polygons_from_soundings_covers_the_bar():
    dm = DepthMap()
    _seed_bar(dm)
    mask = dm.shallow_polygons(BBOX, min_depth_m=1.5, margin_m=1.0)
    assert mask is not None and not mask.is_empty
    # A point on the bar is inside the mask; deep water off the bar is not.
    assert mask.buffer(1e-9).intersects(Point(13.0201, 59.014))
    assert not mask.intersects(Point(13.006, 59.010))  # open water, far from bar
    # Deep soundings never make the mask.
    deep = DepthMap()
    deep.points = [(59.01, 13.01, 12.0), (59.011, 13.011, 9.0)]
    assert deep.shallow_polygons(BBOX, min_depth_m=1.5) is None


def test_shallow_polygons_from_contours_closed_ring_filled():
    """A CLOSED shallow isobath fills to a shoal polygon; a deep one is ignored."""
    dm = DepthMap()
    dm.contours = [
        {"d": 1.0, "pts": [[59.010, 13.010], [59.010, 13.020],
                           [59.015, 13.020], [59.015, 13.010], [59.010, 13.010]]},
        {"d": 10.0, "pts": [[59.000, 13.030], [59.000, 13.035],
                            [59.005, 13.035], [59.005, 13.030], [59.000, 13.030]]},
    ]
    mask = dm.shallow_polygons(BBOX, min_depth_m=2.0, margin_m=1.0)
    assert mask is not None
    assert mask.intersects(Point(13.015, 59.0125))   # inside the shallow shoal
    assert not mask.intersects(Point(13.0325, 59.0025))  # inside the DEEP contour


# --------------------------------------------------------------------------- #
# routing.py: hard avoidance + fallback
# --------------------------------------------------------------------------- #
def test_depth_aware_route_goes_around_the_bar():
    dm = DepthMap()
    _seed_bar(dm)
    mask = dm.shallow_polygons(BBOX, min_depth_m=1.5, margin_m=1.0)
    assert mask is not None

    common = dict(start_lat=59.010, start_lon=13.002,
                  dest_lat=59.010, dest_lon=13.038, water_ll=_lake())

    agnostic = routing.plan_route(**common)
    aware = routing.plan_route(**common, avoid_shallow_ll=mask)
    assert agnostic.ok and aware.ok

    line_agn = _route_line(59.010, 13.002, agnostic.waypoints)
    line_awa = _route_line(59.010, 13.002, aware.waypoints)

    # The depth-agnostic route drives straight across -> crosses the shallow bar.
    assert line_agn.intersects(mask)
    # The depth-aware route stays out of the bar's core and detours SOUTH through
    # the gap (its min latitude dips below the bar's southern end at 59.008).
    assert not line_awa.intersects(mask.buffer(-2e-5))
    awa_min_lat = min(y for _, y in line_awa.coords)
    assert awa_min_lat < 59.008, awa_min_lat
    agn_min_lat = min(y for _, y in line_agn.coords)
    assert agn_min_lat >= 59.009, agn_min_lat  # agnostic did NOT go around


def test_no_depth_data_is_identical_to_plain_route():
    """Regression: passing an empty/None mask yields the same route as today."""
    common = dict(start_lat=59.010, start_lon=13.002,
                  dest_lat=59.010, dest_lon=13.038, water_ll=_lake())
    base = routing.plan_route(**common)
    with_none = routing.plan_route(**common, avoid_shallow_ll=None)
    dm = DepthMap()  # no data -> None mask
    with_empty = routing.plan_route(
        **common, avoid_shallow_ll=dm.shallow_polygons(BBOX, min_depth_m=2.0))
    assert base.ok and with_none.ok and with_empty.ok
    assert base.waypoints == with_none.waypoints == with_empty.waypoints


def test_start_in_shallow_falls_back_and_still_returns_a_path():
    """A shoal covering the START must not trap the boat -- routing falls back
    gracefully and still returns a usable path."""
    dm = DepthMap()
    # A shallow blob right on the start position.
    la = 59.009
    while la <= 59.011 + 1e-9:
        lo = 13.001
        while lo <= 13.004 + 1e-9:
            dm.points.append((round(la, 6), round(lo, 6), 0.4))
            lo += 0.0001
        la += 0.0001
    mask = dm.shallow_polygons(BBOX, min_depth_m=1.5, margin_m=1.0)
    assert mask is not None and mask.intersects(Point(13.002, 59.010))

    r = routing.plan_route(
        start_lat=59.010, start_lon=13.002,
        dest_lat=59.010, dest_lon=13.038, water_ll=_lake(),
        avoid_shallow_ll=mask,
    )
    assert r.ok and len(r.waypoints) >= 1


def test_soft_penalty_still_returns_a_route():
    """A soft penalty band nudges the search but never blocks a route."""
    dm = DepthMap()
    _seed_bar(dm)
    band = dm.shallow_polygons(BBOX, min_depth_m=1.5, margin_m=1.0)
    r = routing.plan_route(
        start_lat=59.010, start_lon=13.002,
        dest_lat=59.010, dest_lon=13.038, water_ll=_lake(),
        penalize_shallow_ll=band, shallow_penalty=6.0,
    )
    assert r.ok and len(r.waypoints) >= 1


def test_depth_aware_large_basin_stays_bounded():
    """Performance smoke: depth-awareness on a big basin returns promptly."""
    water_ll = MultiPolygon([Point(13.2, 59.7).buffer(0.12, quad_segs=400)])
    dm = DepthMap()
    # A small shoal near the middle of the route.
    la = 59.699
    while la <= 59.701 + 1e-9:
        dm.points.append((round(la, 6), 13.20, 0.5))
        la += 0.0001
    mask = dm.shallow_polygons((13.0, 59.5, 13.4, 59.9), min_depth_m=1.5)
    t0 = time.monotonic()
    r = routing.plan_route(
        start_lat=59.66, start_lon=13.12,
        dest_lat=59.74, dest_lon=13.28,
        water_ll=water_ll, avoid_shallow_ll=mask,
    )
    dt = time.monotonic() - t0
    assert r.ok, r.message
    assert len(r.waypoints) >= 1
    assert dt < 30.0, f"depth-aware large-basin plan took {dt:.1f}s"
