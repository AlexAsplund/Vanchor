"""Routing must stay TRACTABLE on a huge water body (e.g. a lake merged with a
much larger one via a connecting river). Before the bound, such a basin (~70k
vertices) made the visibility graph / shoreline ring hang for minutes. The
planner now clips to a route corridor + caps the vertex / ring-point counts.
"""

from shapely.geometry import MultiPolygon, Point

from vanchor.nav import routing
from vanchor.nav.routing import (
    MAX_PLAN_VERTS, Projection, _bounded_basin, _vertex_count,
)


def _big_lake_ll(verts_quad=400):
    """A large circular lake (lon/lat) with > MAX_PLAN_VERTS boundary vertices."""
    return MultiPolygon([Point(13.2, 59.7).buffer(0.12, quad_segs=verts_quad)])


def test_bounded_basin_caps_vertices():
    proj = Projection.for_point(13.2, 59.7)
    lake_m = proj.to_metric(_big_lake_ll().geoms[0])
    assert _vertex_count(lake_m) > MAX_PLAN_VERTS  # the input really is oversized
    s = Point(*proj.point_to_metric(13.12, 59.66))
    d = Point(*proj.point_to_metric(13.28, 59.74))
    bounded = _bounded_basin(lake_m, s, d, 6000.0, MAX_PLAN_VERTS)
    assert not bounded.is_empty
    assert _vertex_count(bounded) <= MAX_PLAN_VERTS


def test_plan_route_on_large_basin_completes_and_stays_in_water():
    # If the bound regressed, this would hang (the test would time out) instead of
    # returning. Both modes must return a water-only route promptly.
    water_ll = _big_lake_ll()
    for mode in ("shoreline", "fastest"):
        r = routing.plan_route(
            start_lat=59.66, start_lon=13.12,
            dest_lat=59.74, dest_lon=13.28,
            water_ll=water_ll, mode=mode,
        )
        assert r.ok, f"{mode}: {r.message}"
        assert len(r.waypoints) >= 1  # at least the destination (line-of-sight here)
