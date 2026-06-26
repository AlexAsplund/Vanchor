"""Smart "Take me here" water routing.

Given a start and a destination, plan a route that stays on **water only**
(never crossing land or islands) and return it as a short list of waypoints for
the UI's route editor to load *unstarted* (the skipper reviews before pressing
Go). Two modes:

- ``fastest``  -- the shortest navigable water path. Computed as the exact
  shortest obstacle-avoiding path with a **visibility graph** over the water
  polygon (bends occur only at shore/island vertices), via shapely + networkx.
- ``shoreline`` -- head to the nearest shore (ending ``shoreline_offset_m`` m
  off it), hug that offset ring toward the destination, then cut straight in as
  soon as there is clear open-water line-of-sight.

All geometry maths happens in a metric UTM CRS (see :mod:`.water`); only the
final waypoints are converted back to lat/lon.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field

import networkx as nx
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import nearest_points
from shapely.prepared import prep

from .water import Projection

logger = logging.getLogger("vanchor.nav.routing")

# Snap a start/dest this far (m) onto water before giving up as "on land".
MAX_SNAP_M = 150.0
# Generalize the shore to this tolerance (m) before routing. A boat doesn't need
# sub-10 m shore detail, and the visibility graph is O(n^2) in vertex count, so
# this is the single biggest speed-up for dense OSM polygons.
ROUTE_SIMPLIFY_M = 8.0
# Douglas-Peucker tolerance (m) and waypoint cap for the returned route.
DEFAULT_SIMPLIFY_M = 10.0
MAX_WAYPOINTS = 50


@dataclass
class RouteResult:
    ok: bool
    waypoints: list[dict] = field(default_factory=list)  # {name, lat, lon}
    message: str = ""
    distance_m: float = 0.0
    mode: str = "fastest"
    # A closed loop route (last waypoint returns toward the first) -- set by the
    # "around island" planner so the UI/controller can flag it for loop-following.
    loop: bool = False


class RoutePlanCancelled(Exception):
    """Raised internally when a caller cancels an in-progress plan (#54)."""


def _check_cancel(cancelled: Callable[[], bool] | None) -> None:
    """Raise :class:`RoutePlanCancelled` if the caller asked to abort."""
    if cancelled is not None and cancelled():
        raise RoutePlanCancelled


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def _largest_polygon(water_m: BaseGeometry) -> BaseGeometry:
    """The water sub-polygon with the greatest area (the main basin)."""
    if water_m.geom_type == "MultiPolygon":
        return max(water_m.geoms, key=lambda g: g.area)
    return water_m


def _water_body_for(pt: Point, water_m: BaseGeometry) -> BaseGeometry:
    """The water polygon the boat is actually in (or nearest to).

    The fetched area usually contains SEVERAL lakes; routing must stay in the
    one the boat occupies, not the largest one in view. Returns the component
    that covers ``pt``; if the boat sits just off the mapped polygon (GPS /
    coordinate slop), the nearest component; otherwise the whole geometry.
    """
    if water_m.geom_type != "MultiPolygon":
        return water_m
    for g in water_m.geoms:
        if g.covers(pt):
            return g
    return min(water_m.geoms, key=lambda g: g.distance(pt))


def _snap_into_water(pt: Point, water_m: BaseGeometry) -> tuple[Point, float]:
    """Return ``pt`` if already in water, else the nearest water point + dist."""
    if water_m.covers(pt):
        return pt, 0.0
    nearest = nearest_points(pt, water_m)[1]
    return nearest, pt.distance(nearest)


def _boundary_vertices(water_m: BaseGeometry) -> list[tuple[float, float]]:
    """All boundary vertices (exterior + island interiors) of the water polygon."""
    verts: list[tuple[float, float]] = []
    polys = water_m.geoms if water_m.geom_type == "MultiPolygon" else [water_m]
    for poly in polys:
        verts.extend(poly.exterior.coords[:-1])
        for ring in poly.interiors:
            verts.extend(ring.coords[:-1])
    return verts


def _simplify_to_waypoints(
    line_m: LineString,
    proj: Projection,
    *,
    tol_m: float = DEFAULT_SIMPLIFY_M,
    water_m: BaseGeometry | None = None,
) -> list[dict]:
    """Simplify a metric polyline and convert to named lat/lon waypoints.

    Douglas-Peucker can shave a genuine bend and pull the line across a
    headland, so when a water polygon is given we back the tolerance off until
    the simplified line still stays in water (never below the unsimplified
    path, which is in water by construction).
    """
    tol = tol_m
    simplified = line_m.simplify(tol)
    if water_m is not None:
        nav = water_m.buffer(0.5)
        while tol > 0.5 and not nav.covers(simplified):
            tol /= 2.0
            simplified = line_m.simplify(tol)
        if not nav.covers(simplified):
            simplified = line_m  # fall back to the exact path
    coords = list(simplified.coords)
    # Cap the waypoint count by progressively increasing the tolerance -- but
    # never simplify so far that the route leaves the water (it's better to
    # return a few extra waypoints than a leg that cuts across land).
    nav = water_m.buffer(0.5) if water_m is not None else None
    while len(coords) > MAX_WAYPOINTS:
        tol *= 2
        cand = line_m.simplify(tol)
        if nav is not None and not nav.covers(cand):
            break
        coords = list(cand.coords)
    waypoints: list[dict] = []
    n = len(coords)
    for i, (x, y) in enumerate(coords):
        lon, lat = proj.point_to_lonlat(x, y)
        name = "DEST" if i == n - 1 else f"WP{i + 1}"
        waypoints.append({"name": name, "lat": lat, "lon": lon})
    return waypoints


# --------------------------------------------------------------------------- #
# Fastest route: visibility graph
# --------------------------------------------------------------------------- #
def _plan_fastest_metric(
    start_m: Point,
    dest_m: Point,
    water_m: BaseGeometry,
    cancelled: Callable[[], bool] | None = None,
) -> LineString | None:
    """Shortest water-only polyline from ``start_m`` to ``dest_m`` (metric).

    Visibility graph over shore vertices + A* search. Speed-ups: the shore is
    already generalized by the caller (ROUTE_SIMPLIFY_M); here we additionally
    prune vertices that cannot lie on a sensible path -- those outside an ellipse
    with foci start/dest -- so routing across a small part of a big lake ignores
    far-off shore. If that prunes a needed detour (no path), we retry on the full
    vertex set.
    """
    prepared = prep(water_m)
    if prepared.covers(LineString([start_m, dest_m])):
        return LineString([start_m, dest_m])  # direct line of sight

    verts = _boundary_vertices(water_m)
    sx, sy, dx, dy = start_m.x, start_m.y, dest_m.x, dest_m.y
    direct = start_m.distance(dest_m)

    def _search(budget: float) -> LineString | None:
        if math.isinf(budget):
            kept = verts
        else:
            kept = [
                v for v in verts
                if math.hypot(v[0] - sx, v[1] - sy) + math.hypot(v[0] - dx, v[1] - dy) <= budget
            ]
        nodes = [(sx, sy), (dx, dy), *kept]
        seen: dict[tuple[float, float], int] = {}
        unique: list[tuple[float, float]] = []
        for node in nodes:
            key = (round(node[0], 3), round(node[1], 3))
            if key not in seen:
                seen[key] = len(unique)
                unique.append(node)

        graph = nx.Graph()
        graph.add_nodes_from(range(len(unique)))
        for i in range(len(unique)):
            # The visibility-graph build is the O(n^2) hot loop; poll the cancel
            # flag once per source vertex so a long plan aborts promptly.
            _check_cancel(cancelled)
            ax, ay = unique[i]
            for j in range(i + 1, len(unique)):
                seg = LineString([(ax, ay), unique[j]])
                if prepared.covers(seg):
                    graph.add_edge(i, j, length=seg.length)

        def heuristic(a: int, b: int) -> float:
            return math.hypot(unique[a][0] - unique[b][0], unique[a][1] - unique[b][1])

        try:
            path = nx.astar_path(graph, 0, 1, heuristic=heuristic, weight="length")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None
        return LineString([unique[i] for i in path])

    line = _search(max(direct * 2.5, direct + 400.0))
    if line is None and verts:
        line = _search(math.inf)  # pruning may have removed a needed detour
    return line


# --------------------------------------------------------------------------- #
# Shoreline route: hug an offset ring, cut in on clear line-of-sight
# --------------------------------------------------------------------------- #
def _densify(line: LineString, step_m: float) -> list[Point]:
    """Sample points along ``line`` no more than ``step_m`` apart."""
    if line.length == 0:
        return [Point(line.coords[0])]
    n = max(2, int(line.length // step_m) + 1)
    return [line.interpolate(i / (n - 1), normalized=True) for i in range(n)]


def _plan_shoreline_metric(
    start_m: Point, dest_m: Point, water_m: BaseGeometry, offset_m: float
) -> tuple[LineString | None, str]:
    """Coast-hugging metric polyline, or (None, reason) if not tractable.

    Computed as the shortest path *inside a near-shore corridor* -- the band of
    water roughly ``offset_m`` off the shore (the deep interior is excluded). By
    construction this hugs the coast and traces into bays, and never shortcuts
    across open water or crosses land (every graph edge is covered by the
    corridor). The final route adds the short hops from the start/destination to
    the corridor.
    """
    basin = _largest_polygon(water_m)

    # Adaptively shrink the offset if buffer(-X) empties / fragments badly.
    offset = None
    used_off = offset_m
    for factor in (1.0, 0.5, 0.25):
        cand = basin.buffer(-offset_m * factor)
        if not cand.is_empty:
            offset, used_off = cand, offset_m * factor
            break
    if offset is None or offset.is_empty:
        return None, "water too narrow for an offset ring"

    prepared = prep(basin)
    clearance = min(used_off, DEFAULT_SIMPLIFY_M)
    # The deep open-water interior: a straight cut to the mark is only taken when
    # it does NOT cross this, so the route traces into a bay rather than
    # shortcutting across its mouth (narrow inlets buffer to empty -> a tiny cut
    # is still allowed there).
    deep = basin.buffer(-used_off * 2.5)

    ring = offset.boundary
    entry = nearest_points(start_m, ring)[1]
    ring_pts = _densify(
        ring if ring.geom_type == "LineString" else max(ring.geoms, key=lambda g: g.length),
        max(5.0, used_off),
    )
    if not ring_pts:
        return None, "empty offset ring"

    def _nearest_idx(target: Point) -> int:
        return min(range(len(ring_pts)), key=lambda k: ring_pts[k].distance(target))

    i_entry = _nearest_idx(entry)
    i_exit = _nearest_idx(nearest_points(dest_m, ring)[1])
    n = len(ring_pts)

    def _walk(direction: int) -> list[Point]:
        seq = [entry]
        k = i_entry
        for _ in range(n):
            p = ring_pts[k]
            seq.append(p)
            cut = LineString([p, dest_m])
            near_shore = deep.is_empty or not cut.intersects(deep)
            if near_shore and prepared.covers(cut) and prepared.covers(cut.buffer(clearance)):
                return seq  # alongside the mark, clear near-shore hop -> cut in
            if k == i_exit:
                break
            k = (k + direction) % n
        return seq

    hug = min((_walk(1), _walk(-1)), key=lambda s: LineString([(p.x, p.y) for p in s]).length)

    coords = [(start_m.x, start_m.y)] + [(p.x, p.y) for p in hug] + [(dest_m.x, dest_m.y)]
    deduped = [coords[0]]
    for c in coords[1:]:
        if c != deduped[-1]:
            deduped.append(c)
    if len(deduped) < 2:
        return None, "degenerate shoreline route"
    line = LineString(deduped)
    # A densified-ring chord can clip a sharp concavity; if the hug leaves the
    # water (>1 m), let the caller fall back to the fastest (in-water) route.
    if not basin.buffer(1.0).covers(line):
        return None, "shoreline hug would leave the water"
    return line, ""


# --------------------------------------------------------------------------- #
# Public entry point (CPU/IO heavy -- call from an executor)
# --------------------------------------------------------------------------- #
def plan_route(
    *,
    start_lat: float,
    start_lon: float,
    dest_lat: float,
    dest_lon: float,
    water_ll: MultiPolygon,
    mode: str = "fastest",
    shoreline_offset_m: float = 25.0,
    cancelled: Callable[[], bool] | None = None,
) -> RouteResult:
    """Plan a water-only route over an already-assembled water polygon.

    ``water_ll`` is a lon/lat polygon (as produced by :mod:`.water`). This is
    pure CPU work (shapely + networkx); run it in an executor.

    ``cancelled`` is an optional predicate polled periodically during the heavy
    visibility-graph build; if it returns True the plan aborts and returns a
    cancelled result (#54).
    """
    try:
        return _plan_route_inner(
            start_lat=start_lat,
            start_lon=start_lon,
            dest_lat=dest_lat,
            dest_lon=dest_lon,
            water_ll=water_ll,
            mode=mode,
            shoreline_offset_m=shoreline_offset_m,
            cancelled=cancelled,
        )
    except RoutePlanCancelled:
        return RouteResult(False, message="Route planning cancelled.", mode=mode)


def _plan_route_inner(
    *,
    start_lat: float,
    start_lon: float,
    dest_lat: float,
    dest_lon: float,
    water_ll: MultiPolygon,
    mode: str,
    shoreline_offset_m: float,
    cancelled: Callable[[], bool] | None,
) -> RouteResult:
    _check_cancel(cancelled)
    proj = Projection.for_point(dest_lon, dest_lat)
    water_m = proj.to_metric(water_ll)
    if not water_m.is_valid:
        water_m = water_m.buffer(0)

    sx, sy = proj.point_to_metric(start_lon, start_lat)
    dx, dy = proj.point_to_metric(dest_lon, dest_lat)

    # Restrict routing to the water body the BOAT is in (not the largest lake in
    # view) so the route -- and especially the shoreline hug -- never wanders
    # onto a neighbouring lake's shore.
    basin = _water_body_for(Point(sx, sy), water_m)
    # Generalize the shore -- the visibility graph is O(n^2) in vertex count.
    simple = basin.simplify(ROUTE_SIMPLIFY_M)
    if not simple.is_empty and simple.is_valid:
        basin = _largest_polygon(simple) if simple.geom_type == "MultiPolygon" else simple
    start_m, snap_s = _snap_into_water(Point(sx, sy), basin)
    dest_m, snap_d = _snap_into_water(Point(dx, dy), basin)

    if snap_s > MAX_SNAP_M:
        return RouteResult(False, message="Start position is on land or outside known water.", mode=mode)
    if snap_d > MAX_SNAP_M:
        return RouteResult(
            False,
            message="Destination is on land or in a different water body than the boat.",
            mode=mode,
        )

    message = ""
    if mode == "shoreline":
        line_m, reason = _plan_shoreline_metric(start_m, dest_m, basin, shoreline_offset_m)
        if line_m is None:
            # Fall back to fastest rather than blocking the request.
            line_m = _plan_fastest_metric(start_m, dest_m, basin, cancelled)
            message = f"Shoreline mode unavailable ({reason}); returned fastest route instead."
            mode = "fastest"
    else:
        line_m = _plan_fastest_metric(start_m, dest_m, basin, cancelled)

    if line_m is None:
        return RouteResult(False, message="No water route to the destination.", mode=mode)

    waypoints = _simplify_to_waypoints(line_m, proj, water_m=basin)
    # The boat is already at the start, so a leading waypoint sitting on it is
    # redundant -- drop it so WP1 is the first place the boat actually steers to.
    if len(waypoints) >= 2:
        w0 = proj.point_to_metric(waypoints[0]["lon"], waypoints[0]["lat"])
        if Point(w0).distance(Point(sx, sy)) < 5.0:
            waypoints = waypoints[1:]
    if waypoints:
        waypoints[0]["name"] = "WP1"
        waypoints[-1]["name"] = "DEST"
    if not message:
        message = f"Planned {mode} route with {len(waypoints)} waypoints."
    return RouteResult(
        ok=True,
        waypoints=waypoints,
        message=message,
        distance_m=line_m.length,
        mode=mode,
    )


# --------------------------------------------------------------------------- #
# "Around island" loop route (#77)
# --------------------------------------------------------------------------- #
# A loop has a closed run of waypoints; cap it a touch higher than a one-way
# route so a big island still gets enough points to trace its shape.
MAX_LOOP_WAYPOINTS = 60


def _island_holes(basin: BaseGeometry) -> list[Polygon]:
    """Every island (interior ring of the basin) as a filled polygon.

    The basin's interior rings are land surrounded by routable water, i.e. the
    islands. Each is returned as a solid :class:`Polygon` so we can test
    containment and buffer it outward.
    """
    polys: list[Polygon] = []
    parts = basin.geoms if basin.geom_type == "MultiPolygon" else [basin]
    for part in parts:
        for ring in part.interiors:
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if not poly.is_empty and poly.geom_type == "Polygon":
                polys.append(poly)
    return polys


def _loop_waypoints(
    ring: LineString,
    proj: Projection,
    *,
    tol_m: float = DEFAULT_SIMPLIFY_M,
    water_m: BaseGeometry | None = None,
) -> list[dict]:
    """Simplify a closed metric ring into ordered, closed lat/lon waypoints.

    Mirrors :func:`_simplify_to_waypoints` but keeps the loop closed (the last
    waypoint equals the first so the boat circles back) and never simplifies so
    far that the loop leaves the water.
    """
    nav = water_m.buffer(0.5) if water_m is not None else None
    tol = tol_m
    simplified = ring.simplify(tol)
    if nav is not None:
        while tol > 0.5 and not nav.covers(simplified):
            tol /= 2.0
            simplified = ring.simplify(tol)
        if not nav.covers(simplified):
            simplified = ring  # fall back to the exact ring
    coords = list(simplified.coords)
    while len(coords) > MAX_LOOP_WAYPOINTS:
        tol *= 2
        cand = ring.simplify(tol)
        if nav is not None and not nav.covers(cand):
            break
        coords = list(cand.coords)
    # A closed ring repeats its first vertex at the end; drop that duplicate and
    # re-append the first point's name so the route reads as a clean loop.
    if len(coords) >= 2 and coords[0] == coords[-1]:
        coords = coords[:-1]
    waypoints: list[dict] = []
    for i, (x, y) in enumerate(coords):
        lon, lat = proj.point_to_lonlat(x, y)
        waypoints.append({"name": f"WP{i + 1}", "lat": lat, "lon": lon})
    # Close the loop: return toward the first waypoint at the end.
    if waypoints:
        first = waypoints[0]
        waypoints.append({"name": "LOOP", "lat": first["lat"], "lon": first["lon"]})
    return waypoints


def plan_island_loop(
    click_lat: float,
    click_lon: float,
    water_ll: MultiPolygon,
    *,
    boat_lat: float,
    boat_lon: float,
    offset_m: float = 20.0,
) -> RouteResult:
    """Plan a closed loop track that encircles the island under the click.

    The boat's basin is the water body it occupies; an **island** is one of that
    basin's interior rings (land ringed by routable water). We find the island
    whose polygon contains the click, buffer it outward by ``offset_m``, take
    that offset ring and clip it to the navigable water (the basin minus its
    other islands) so the whole loop stays on the water. If the offset ring
    can't stay in water all the way around, the offset is shrunk; if even a small
    offset won't fit, the request is rejected.

    Pure CPU work (shapely); run it from an executor.
    """
    proj = Projection.for_point(click_lon, click_lat)
    water_m = proj.to_metric(water_ll)
    if not water_m.is_valid:
        water_m = water_m.buffer(0)

    bx, by = proj.point_to_metric(boat_lon, boat_lat)
    basin = _water_body_for(Point(bx, by), water_m)

    cx, cy = proj.point_to_metric(click_lon, click_lat)
    click = Point(cx, cy)

    islands = _island_holes(basin)
    target = next((isl for isl in islands if isl.contains(click)), None)
    if target is None:
        return RouteResult(
            False,
            message=(
                "That's not an island in the boat's water body -- click on a "
                "patch of land fully surrounded by the lake to circle it."
            ),
            mode="island",
            loop=True,
        )

    # Navigable water around the island: the basin's filled outline minus EVERY
    # island (including the target), so the loop never crosses land.
    basin_filled = Polygon(
        _largest_polygon(basin).exterior
        if basin.geom_type != "MultiPolygon"
        else max(basin.geoms, key=lambda g: g.area).exterior
    )
    navigable = basin_filled
    for isl in islands:
        navigable = navigable.difference(isl)
    if not navigable.is_valid:
        navigable = navigable.buffer(0)

    # Buffer the island outward and clip the offset ring to navigable water.
    # Shrink the offset if the full ring won't stay in water all the way around.
    used_off = offset_m
    loop_ring: LineString | None = None
    shrunk = False
    for factor in (1.0, 0.75, 0.5, 0.35, 0.25):
        off = offset_m * factor
        grown = target.buffer(off)
        ring = grown.exterior
        clipped = ring.intersection(navigable.buffer(0.5))
        # The whole offset ring must stay in water: the clipped geometry should
        # still be one continuous loop roughly as long as the offset ring.
        if clipped.is_empty:
            continue
        length = clipped.length if hasattr(clipped, "length") else 0.0
        if length >= ring.length * 0.98:
            used_off = off
            loop_ring = LineString(ring.coords)
            shrunk = factor < 1.0
            break

    if loop_ring is None:
        return RouteResult(
            False,
            message=(
                "The island is too close to shore or another island to circle "
                "with a navigable offset on all sides."
            ),
            mode="island",
            loop=True,
        )

    waypoints = _loop_waypoints(loop_ring, proj, water_m=navigable)
    if len(waypoints) < 4:
        return RouteResult(
            False,
            message="Could not build a sensible loop around that island.",
            mode="island",
            loop=True,
        )

    message = f"Planned a loop around the island with {len(waypoints)} waypoints"
    if shrunk:
        message += f" (offset shrunk to {used_off:.0f} m to stay on water)"
    message += "."
    return RouteResult(
        ok=True,
        waypoints=waypoints,
        message=message,
        distance_m=loop_ring.length,
        mode="island",
        loop=True,
    )
