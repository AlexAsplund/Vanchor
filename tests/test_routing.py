"""Fixture-based tests for the smart water router (no live network).

The fixture ``tests/data/water_sim.geojson`` is a real OpenStreetMap water
polygon for the simulator's lake (Visten, near Karlstad), assembled with the
relation-aware pipeline and simplified to a manageable vertex count. The boat's
configured sim start sits inside it.
"""

from pathlib import Path

from shapely.geometry import LineString, Point

from vanchor.core.config import SimConfig
from vanchor.nav import routing, water

FIXTURE = Path(__file__).parent / "data" / "water_sim.geojson"

# A destination, in water, whose straight line from the sim start crosses land,
# so the fastest route must bend around it (>= 3 waypoints).
DEST_AROUND_LAND = (59.66430488913581, 13.368675408442506)


def _load():
    return water.load_geojson(FIXTURE)


def test_sim_start_is_in_water():
    poly = _load()
    start = Point(SimConfig().start_lon, SimConfig().start_lat)
    assert poly.covers(start), "sim start must be inside the assembled water polygon"


def test_route_stays_in_the_boats_lake_not_the_largest():
    """Regression: the planner must restrict to the water body the boat is in,
    not the biggest lake in view (the bug where shoreline hugged a neighbour)."""
    import shapely.affinity as aff
    from shapely.geometry import MultiPolygon, Polygon

    boat_lake = Polygon([(-0.01, -0.01), (0.01, -0.01), (0.01, 0.01), (-0.01, 0.01)])
    bigger_neighbour = Polygon([(0.05, -0.03), (0.12, -0.03), (0.12, 0.03), (0.05, 0.03)])
    world = aff.translate(MultiPolygon([boat_lake, bigger_neighbour]), xoff=13.32, yoff=59.66)
    biggest = max(world.geoms, key=lambda g: g.area)

    body = routing._water_body_for(Point(13.32, 59.66), world)
    assert body.area < biggest.area  # picked the boat's (smaller) lake

    res = routing.plan_route(
        start_lat=59.66, start_lon=13.32, dest_lat=59.665, dest_lon=13.325,
        water_ll=world, mode="shoreline", shoreline_offset_m=20.0,
    )
    assert res.ok and res.waypoints
    assert not any(biggest.covers(Point(w["lon"], w["lat"])) for w in res.waypoints)


def test_fixture_is_multipolygon_with_islands():
    # The relation-aware assembly yields a polygon with island holes.
    poly = _load()
    assert poly.geom_type in ("Polygon", "MultiPolygon")
    interiors = sum(len(p.interiors) for p in getattr(poly, "geoms", [poly]))
    assert interiors >= 1, "expected island holes in the water polygon"


def _segments_in_water(waypoints, poly):
    """True iff every consecutive waypoint segment stays in water (1 m slack)."""
    proj = water.Projection.for_point(waypoints[0]["lon"], waypoints[0]["lat"])
    water_m = proj.to_metric(poly)
    nav = water_m.buffer(1.0)  # 1 m tolerance for floating-point boundary touch
    prev = None
    for wp in waypoints:
        cur = proj.point_to_metric(wp["lon"], wp["lat"])
        if prev is not None and not nav.covers(LineString([prev, cur])):
            return False
        prev = cur
    return True


def test_fastest_route_returns_waypoints_on_water():
    poly = _load()
    res = routing.plan_route(
        start_lat=SimConfig().start_lat,
        start_lon=SimConfig().start_lon,
        dest_lat=DEST_AROUND_LAND[0],
        dest_lon=DEST_AROUND_LAND[1],
        water_ll=poly,
        mode="fastest",
    )
    assert res.ok, res.message
    assert len(res.waypoints) >= 2
    assert res.waypoints[-1]["name"] == "DEST"
    assert _segments_in_water(res.waypoints, poly)


def test_fastest_route_bends_around_land():
    # Destination is deliberately not in direct line of sight, so the shortest
    # water path must add an intermediate bend (>= 3 waypoints).
    poly = _load()
    res = routing.plan_route(
        start_lat=SimConfig().start_lat,
        start_lon=SimConfig().start_lon,
        dest_lat=DEST_AROUND_LAND[0],
        dest_lon=DEST_AROUND_LAND[1],
        water_ll=poly,
        mode="fastest",
    )
    assert res.ok
    assert len(res.waypoints) >= 3


def test_destination_on_land_is_rejected():
    poly = _load()
    # Well outside the lake bbox -> snap distance exceeds the threshold.
    res = routing.plan_route(
        start_lat=SimConfig().start_lat,
        start_lon=SimConfig().start_lon,
        dest_lat=59.80,
        dest_lon=13.10,
        water_ll=poly,
        mode="fastest",
    )
    assert not res.ok
    assert "land" in res.message.lower() or "water" in res.message.lower()


def test_shoreline_mode_returns_route():
    poly = _load()
    res = routing.plan_route(
        start_lat=SimConfig().start_lat,
        start_lon=SimConfig().start_lon,
        dest_lat=DEST_AROUND_LAND[0],
        dest_lon=DEST_AROUND_LAND[1],
        water_ll=poly,
        mode="shoreline",
        shoreline_offset_m=30.0,
    )
    assert res.ok, res.message
    assert len(res.waypoints) >= 2
    assert _segments_in_water(res.waypoints, poly)


def test_assemble_water_relation_aware():
    # A minimal multipolygon-relation element (outer ring only) must assemble
    # into a polygon -- the step that makes lakes like Visten appear.
    elements = [
        {
            "type": "relation",
            "members": [
                {
                    "type": "way",
                    "role": "outer",
                    "geometry": [
                        {"lon": 0.0, "lat": 0.0},
                        {"lon": 0.01, "lat": 0.0},
                        {"lon": 0.01, "lat": 0.01},
                        {"lon": 0.0, "lat": 0.01},
                        {"lon": 0.0, "lat": 0.0},
                    ],
                }
            ],
        }
    ]
    poly = water.assemble_water(elements)
    assert not poly.is_empty
    assert poly.covers(Point(0.005, 0.005))
