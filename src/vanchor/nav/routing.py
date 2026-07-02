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
# Bound the planning geometry on very large water bodies (e.g. a lake merged with
# a far larger one via a connecting river -> a 70k-vertex basin). Both planners
# are O(n^2)/O(perimeter), so without a bound they hang for minutes. We clip to a
# corridor around the route and cap the vertex / ring-point counts.
MAX_PLAN_VERTS = 800       # boundary-vertex cap fed to the planners
MAX_RING_PTS = 1500        # cap on shoreline-walk ring points
MIN_CORRIDOR_M = 2500.0    # route-corridor half-width bounds (excludes far water)
MAX_CORRIDOR_M = 12000.0


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


def _vertex_count(geom: BaseGeometry) -> int:
    """Total boundary vertices (exterior + interiors) of a (Multi)Polygon."""
    polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
    return sum(
        len(p.exterior.coords) + sum(len(r.coords) for r in p.interiors) for p in polys
    )


def _bounded_basin(
    basin: BaseGeometry, start_m: Point, dest_m: Point,
    corridor_m: float | None, vert_cap: int,
) -> BaseGeometry:
    """Bound the planning geometry on a huge water body so the visibility graph
    and shoreline ring stay tractable. Optionally clip to a corridor (a buffer
    around the straight start->dest line, which keeps the relevant water and drops
    far-off reaches of a merged system), then Douglas-Peucker simplify until the
    boundary is <= ``vert_cap`` vertices. Returns the boat's water sub-polygon of
    the result (empty geometry if the clip removed it)."""
    body = basin
    if corridor_m is not None:
        corr = LineString([(start_m.x, start_m.y), (dest_m.x, dest_m.y)]).buffer(corridor_m)
        clipped = basin.intersection(corr)
        if clipped.is_empty:
            return clipped
        body = _water_body_for(start_m, clipped)
    tol = ROUTE_SIMPLIFY_M
    while _vertex_count(body) > vert_cap and tol < 2000.0:
        tol *= 1.7
        simple = body.simplify(tol)
        if simple.is_empty or not simple.is_valid:
            break
        body = _largest_polygon(simple) if simple.geom_type == "MultiPolygon" else simple
    return body


def _subtract_shallow(
    basin: BaseGeometry,
    shallow_ll: BaseGeometry,
    proj: Projection,
    boat_pt: Point,
) -> BaseGeometry | None:
    """Subtract shallow no-go areas (lon/lat geometry) from the boat's ``basin``.

    Returns the boat's navigable water body with the shallow areas removed and
    re-generalized (bounded vertex count), or ``None`` if the subtraction empties
    the basin -- in which case the caller keeps the full basin. Keeping only the
    boat's component means a shoal that splits the lake leaves us with the reach
    the boat is in; if the destination lands in a severed reach, planning simply
    finds no path and the caller falls back gracefully.
    """
    shallow_m = proj.to_metric(shallow_ll)
    if not shallow_m.is_valid:
        shallow_m = shallow_m.buffer(0)
    if shallow_m.is_empty:
        return None
    cut = basin.difference(shallow_m)
    if not cut.is_valid:
        cut = cut.buffer(0)
    if cut.is_empty:
        return None
    body = _water_body_for(boat_pt, cut)
    simple = body.simplify(ROUTE_SIMPLIFY_M)
    if not simple.is_empty and simple.is_valid:
        body = _largest_polygon(simple) if simple.geom_type == "MultiPolygon" else simple
    return None if body.is_empty else body


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
    *,
    soft_m: BaseGeometry | None = None,
    soft_penalty: float = 1.0,
) -> LineString | None:
    """Shortest water-only polyline from ``start_m`` to ``dest_m`` (metric).

    Visibility graph over shore vertices + A* search. Speed-ups: the shore is
    already generalized by the caller (ROUTE_SIMPLIFY_M); here we additionally
    prune vertices that cannot lie on a sensible path -- those outside an ellipse
    with foci start/dest -- so routing across a small part of a big lake ignores
    far-off shore. If that prunes a needed detour (no path), we retry on the full
    vertex set.

    ``soft_m`` (metric geometry, e.g. a near-shallow penalty band) softly steers
    the search away from those areas: any visibility edge crossing it has its A*
    weight multiplied by ``soft_penalty`` (>= 1). The heuristic stays the raw
    straight-line distance -- a lower bound on the penalised cost, so A* remains
    admissible. Shallow water can still be crossed if it is the only way through.
    """
    prepared = prep(water_m)
    soft_prep = prep(soft_m) if (soft_m is not None and not soft_m.is_empty
                                 and soft_penalty > 1.0) else None
    if soft_prep is None and prepared.covers(LineString([start_m, dest_m])):
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
                    w = seg.length
                    if soft_prep is not None and soft_prep.intersects(seg):
                        w *= soft_penalty
                    graph.add_edge(i, j, length=w)

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
    start_m: Point, dest_m: Point, water_m: BaseGeometry, offset_m: float,
    cancelled: Callable[[], bool] | None = None,
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
    ring_geom = ring if ring.geom_type == "LineString" else max(ring.geoms, key=lambda g: g.length)
    # Cap the ring-point count (= perimeter / step): a huge basin's ring would
    # otherwise be tens of thousands of points. The corridor clip usually keeps the
    # perimeter small; this bounds the rest so the walk can't blow up.
    step = max(5.0, used_off, ring_geom.length / MAX_RING_PTS)
    ring_pts = _densify(ring_geom, step)
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
            _check_cancel(cancelled)   # the walk can be long -> stay cancellable
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
    avoid_shallow_ll: BaseGeometry | None = None,
    penalize_shallow_ll: BaseGeometry | None = None,
    shallow_penalty: float = 4.0,
) -> RouteResult:
    """Plan a water-only route over an already-assembled water polygon.

    ``water_ll`` is a lon/lat polygon (as produced by :mod:`.water`). This is
    pure CPU work (shapely + networkx); run it in an executor.

    ``cancelled`` is an optional predicate polled periodically during the heavy
    visibility-graph build; if it returns True the plan aborts and returns a
    cancelled result (#54).

    **Depth-awareness (optional, default off -- existing callers unchanged).**

    * ``avoid_shallow_ll`` -- shallow AREAS (lon/lat shapely geometry, e.g. from
      :meth:`DepthMap.shallow_polygons`) treated as HARD no-go: subtracted from
      navigable water so routes go *around* shoals. If the subtraction would trap
      the start/destination or leave no path, the planner falls back to routing
      on the full water (a note is logged) so it never returns worse than before.
    * ``penalize_shallow_ll`` -- a SOFT penalty band (lon/lat geometry): edges
      crossing it are weighted ``shallow_penalty``x in the fastest search, so
      A* prefers deeper water but can still cross if unavoidable.
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
            avoid_shallow_ll=avoid_shallow_ll,
            penalize_shallow_ll=penalize_shallow_ll,
            shallow_penalty=shallow_penalty,
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
    avoid_shallow_ll: BaseGeometry | None = None,
    penalize_shallow_ll: BaseGeometry | None = None,
    shallow_penalty: float = 4.0,
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

    req_mode = mode  # the requested mode (the retry loop must not flip it)

    # Soft penalty band (metric): edges crossing near-shallow water are weighted
    # up so A* prefers deeper routes but can still cross if it must.
    soft_m: BaseGeometry | None = None
    if penalize_shallow_ll is not None and not penalize_shallow_ll.is_empty:
        soft_m = proj.to_metric(penalize_shallow_ll)
        if not soft_m.is_valid:
            soft_m = soft_m.buffer(0)

    def _plan(b: BaseGeometry, s_m: Point, d_m: Point) -> tuple[LineString | None, str, str]:
        """Plan on geometry ``b``; returns (line, result_mode, message)."""
        if req_mode == "shoreline":
            ln, reason = _plan_shoreline_metric(s_m, d_m, b, shoreline_offset_m, cancelled)
            if ln is not None:
                return ln, "shoreline", ""
            # Fall back to fastest rather than blocking the request.
            fb = _plan_fastest_metric(s_m, d_m, b, cancelled,
                                      soft_m=soft_m, soft_penalty=shallow_penalty)
            msg = f"Shoreline mode unavailable ({reason}); returned fastest route instead." if fb is not None else ""
            return fb, "fastest", msg
        return _plan_fastest_metric(s_m, d_m, b, cancelled,
                                    soft_m=soft_m, soft_penalty=shallow_penalty), "fastest", ""

    def _run(nav: BaseGeometry) -> tuple[LineString | None, str, str, BaseGeometry]:
        """Plan on ``nav`` (the full basin, or a shallow-subtracted variant),
        returning (line, result_mode, message, geometry-used). Bounds a huge
        basin exactly as before. Returns ``line=None`` if the start/dest can't be
        reached on ``nav`` (so the caller can fall back to a different geometry).
        """
        s_m, ss = _snap_into_water(Point(sx, sy), nav)
        d_m, sd = _snap_into_water(Point(dx, dy), nav)
        if ss > MAX_SNAP_M or sd > MAX_SNAP_M:
            return None, req_mode, "", nav
        if _vertex_count(nav) <= MAX_PLAN_VERTS:
            ln, m, msg = _plan(nav, s_m, d_m)
            return ln, m, msg, nav
        # Huge water body (e.g. a lake merged with a far larger one via a river):
        # the O(n^2) visibility graph and the shoreline ring are intractable at
        # full detail and would hang. Bound the planning geometry -- clip to a
        # corridor around the route + cap the vertex count -- widening the corridor
        # (and finally dropping it, but STILL capped) if no route is found, so a
        # route is still produced. Every attempt is bounded; it can never hang.
        direct = s_m.distance(d_m)
        base = min(MAX_CORRIDOR_M, max(MIN_CORRIDOR_M, direct * 0.15))
        for corridor_m in (base, base * 3.0, None):
            pb = _bounded_basin(nav, s_m, d_m, corridor_m, MAX_PLAN_VERTS)
            if pb.is_empty:
                continue
            ps_m, pss = _snap_into_water(s_m, pb)
            pd_m, psd = _snap_into_water(d_m, pb)
            if pss > MAX_SNAP_M or psd > MAX_SNAP_M:
                continue
            ln, m, msg = _plan(pb, ps_m, pd_m)
            if ln is not None:
                return ln, m, msg, pb
        return None, req_mode, "", nav

    # Depth-aware HARD avoidance: subtract shallow areas from the boat's basin and
    # try that first; fall back to the full basin if it isolates the start/dest or
    # yields no path (so depth-awareness never returns worse than plain routing).
    attempts: list[tuple[BaseGeometry, bool]] = []
    if avoid_shallow_ll is not None and not avoid_shallow_ll.is_empty:
        nav_basin = _subtract_shallow(basin, avoid_shallow_ll, proj, Point(sx, sy))
        if nav_basin is not None:
            attempts.append((nav_basin, True))
    attempts.append((basin, False))

    line_m = None
    message = ""
    for nav, is_depth in attempts:
        line_m, mode, message, basin = _run(nav)
        if line_m is not None:
            if not is_depth and len(attempts) > 1:
                logger.info("depth-aware routing fell back to full water "
                            "(shallow subtraction left no route)")
            break

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
        # Require essentially full water coverage (≥ 99.99%) so we never return
        # the unclipped ring when any meaningful portion lies outside water.
        # The 0.01% tolerance absorbs floating-point rounding in the intersection;
        # anything more than that must be rejected and a smaller offset tried.
        if length >= ring.length * 0.9999:
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
