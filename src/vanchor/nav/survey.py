"""Area survey route planning -- "map mode" boustrophedon coverage (#47).

Given a closed area polygon (lat/lon), compute a back-and-forth *lawnmower*
coverage route that surveys the whole area with a settable spacing between
parallel passes. The classic agricultural / robotics pattern for this is the
**boustrophedon decomposition**: sweep a set of parallel lines across the area,
clip each line to the polygon, then walk them in alternating direction so the
end of one pass connects to the start of the next (a continuous "ox-plough"
path -- Greek *boustrophedon*, "as the ox turns").

All geometry maths happens in a metric UTM CRS (see :mod:`.water`); only the
final waypoints are converted back to lat/lon.

The default sweep direction is the polygon's **longest axis** (so passes run the
long way and there are fewer expensive turns), unless an explicit ``angle_deg``
is given. The number of passes is capped so a tiny spacing on a big area can't
produce thousands of waypoints; when the cap is hit a clear message is returned.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import substring

from .water import Projection

logger = logging.getLogger("vanchor.nav.survey")

# Cap the number of generated waypoints. A coverage route with more legs than
# this is almost always a mis-set (too-small) spacing; we refuse rather than
# return an unusably huge route the UI then has to load and the boat to follow.
# Warn (do NOT reject) above this many waypoints -- mapping a big area at tight
# spacing legitimately produces a lot of passes. A much higher absolute guard
# only stops a pathological tiny-spacing run from making an unusable route.
WARN_WAYPOINTS = 900
MAX_WAYPOINTS = 5000
# Minimum spacing (m); guards against zero/negative/absurd spacing producing an
# unbounded number of passes.
MIN_SPACING_M = 0.5
# Maximum intermediate boundary waypoints inserted per connecting leg when the
# direct leg exits the constraint polygon.  Keeps the repaired route tractable.
MAX_BOUNDARY_PTS = 20
# Safety inset (m) applied to the routing constraint when inserting boundary
# waypoints along a leg detour.  Keeps detour waypoints ~2 m inside the edge
# (e.g. off the shoreline) so the boat does not hug dry land.
INSET_M = 2.0


@dataclass
class SurveyResult:
    ok: bool
    waypoints: list[dict] = field(default_factory=list)  # {name, lat, lon}
    message: str = ""
    passes: int = 0
    spacing_m: float = 0.0
    angle_deg: float = 0.0


def _longest_axis_angle(poly: Polygon) -> float:
    """Bearing (deg, math convention) of the polygon's longest edge of its
    minimum rotated rectangle -- a good default sweep direction.

    Sweeping *along* the longer side means each pass is as long as possible and
    there are as few turns as possible.
    """
    rect = poly.minimum_rotated_rectangle
    if rect.geom_type != "Polygon":
        return 0.0
    coords = list(rect.exterior.coords)
    # The min rotated rectangle has 5 coords (closed); inspect its two adjacent
    # edge vectors and take the longer one's direction.
    best_len = -1.0
    best_ang = 0.0
    for i in range(len(coords) - 1):
        (x0, y0), (x1, y1) = coords[i], coords[i + 1]
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length > best_len:
            best_len = length
            best_ang = math.degrees(math.atan2(dy, dx))
    return best_ang


def _ordered_lines_for_geom(geom: BaseGeometry) -> list[LineString]:
    """Flatten a clip result (LineString / MultiLineString / empty) to a list of
    LineStrings, dropping degenerate (zero-length) pieces."""
    if geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom] if geom.length > 0 else []
    if geom.geom_type == "MultiLineString":
        return [g for g in geom.geoms if g.length > 0]
    if geom.geom_type == "GeometryCollection":
        out: list[LineString] = []
        for g in geom.geoms:
            out.extend(_ordered_lines_for_geom(g))
        return out
    return []


def _containing_polygon(geom: BaseGeometry, pt: tuple[float, float]) -> Polygon | None:
    """Return the Polygon component of *geom* that covers *pt*.

    Falls back to the nearest component when no polygon strictly covers the
    point (handles float-precision edge cases where a waypoint sits exactly on
    the boundary).
    """
    p = Point(pt)
    if geom.geom_type == "Polygon":
        return geom  # type: ignore[return-value]
    if geom.geom_type == "MultiPolygon":
        for g in geom.geoms:
            if g.covers(p):
                return g
        return min(geom.geoms, key=lambda g: g.distance(p))
    return None


def _inset_for_routing(poly: Polygon, a: tuple[float, float]) -> Polygon:
    """Return an inward-buffered (INSET_M) version of *poly* for routing boundary
    waypoints so they sit inside the edge rather than on it.

    Robustness:
    - If the negative buffer collapses the polygon (narrow throat < 2×INSET_M)
      the original *poly* is returned as a fallback.
    - If the inset splits into a MultiPolygon the component that covers (or is
      nearest to) point *a* is returned; if none qualifies, falls back to *poly*.
    """
    inset = poly.buffer(-INSET_M)
    if inset.is_empty:
        return poly
    if inset.geom_type == "Polygon":
        return inset  # type: ignore[return-value]
    if inset.geom_type == "MultiPolygon":
        component = _containing_polygon(inset, a)
        return component if component is not None else poly
    return poly


def _route_along_boundary(
    poly: Polygon,
    a: tuple[float, float],
    b: tuple[float, float],
    max_pts: int = MAX_BOUNDARY_PTS,
) -> list[tuple[float, float]]:
    """Intermediate waypoints for the shorter arc of *poly*'s exterior ring, a→b.

    Both points are projected onto the exterior ring; the shorter of the two arcs
    between the projections is extracted and its interior vertices returned (the
    projected endpoints of a and b are excluded -- the caller already has those).

    For a concave polygon the exterior ring traces the concavity edges, so
    following it always stays inside (or on the boundary of) the polygon.
    """
    ring_ls = LineString(poly.exterior.coords)
    L = ring_ls.length
    if L < 1e-9:
        return []

    da = ring_ls.project(Point(a))
    db = ring_ls.project(Point(b))
    if abs(da - db) < 1e-9:
        return []

    # Forward arc: da → db increasing along the ring (wraps at L→0 if da > db).
    fwd_len = (db - da) % L
    bwd_len = L - fwd_len

    def _arc_coords(start: float, end: float) -> list[tuple[float, float]]:
        """Coords from *start* to *end* along ring_ls; handles wrap-around."""
        if start <= end:
            seg = substring(ring_ls, start, end)
            return list(seg.coords) if seg and not seg.is_empty else []
        # Wrapping: start → L, then 0 → end.
        s1 = substring(ring_ls, start, L)
        s2 = substring(ring_ls, 0.0, end)
        c1 = list(s1.coords) if s1 and not s1.is_empty else []
        c2 = list(s2.coords) if s2 and not s2.is_empty else []
        # The ring's last coord equals its first; drop the duplicate at the seam.
        if c1 and c2 and c1[-1] == c2[0]:
            c2 = c2[1:]
        return c1 + c2

    if fwd_len <= bwd_len:
        coords = _arc_coords(da, db)
    else:
        # Backward arc a→b = reverse of forward arc b→a.
        coords = _arc_coords(db, da)[::-1]

    # Strip the projected endpoints of a and b (interior vertices only).
    interior = coords[1:-1]
    if not interior:
        return []
    if len(interior) > max_pts:
        step = max(1, len(interior) // max_pts)
        interior = interior[::step][:max_pts]
    return interior


def _repair_legs(
    ordered_pts: list[tuple[float, float]],
    constraint: BaseGeometry,
    max_insert: int = MAX_BOUNDARY_PTS,
) -> list[tuple[float, float]]:
    """Repair connecting legs that exit *constraint*, inserting boundary waypoints.

    Each consecutive pair (a, b) in *ordered_pts* is checked against *constraint*
    (buffered 0.5 m for floating-point tolerance).  When a leg exits, intermediate
    waypoints are inserted along the exterior ring of the polygon component that
    contains *a*, routing around the concavity without leaving the polygon.

    Chord interiors are never problematic (they are clipped to the constraint by
    ``_sweep_lines``), so the check is fast in the common case.
    """
    if len(ordered_pts) < 2:
        return list(ordered_pts)

    # Validation uses the original constraint (with 0.5 m float tolerance) so
    # chord endpoints that legitimately sit near the edge are never rejected.
    nav = constraint.buffer(0.5)
    result: list[tuple[float, float]] = [ordered_pts[0]]

    for i in range(1, len(ordered_pts)):
        a = result[-1]
        b = ordered_pts[i]
        if nav.covers(LineString([a, b])):
            result.append(b)
        else:
            poly = _containing_polygon(constraint, a)
            if poly is not None:
                # Route along an inset polygon so inserted waypoints sit
                # ~INSET_M inside the edge rather than on the shoreline.
                route_poly = _inset_for_routing(poly, a)
                intermediates = _route_along_boundary(route_poly, a, b, max_pts=max_insert)
                result.extend(intermediates)
            result.append(b)

    return result


def _sweep_lines(poly_m: Polygon, spacing_m: float, angle_deg: float) -> list[LineString]:
    """Parallel lines ``spacing_m`` apart at ``angle_deg`` clipped to the polygon.

    Generated in a rotated frame (so the sweep direction is axis-aligned), where
    parallel lines are simply horizontal lines at increasing y; each is clipped
    to the polygon and the clipped chords ordered along the sweep so the route
    reads bottom-to-top.
    """
    import shapely.affinity as aff

    # Rotate the polygon so the desired sweep direction becomes the x-axis; then
    # parallel passes are horizontal lines and clipping is trivial. Rotate the
    # results back at the end.
    rotated = aff.rotate(poly_m, -angle_deg, origin="centroid", use_radians=False)
    minx, miny, maxx, maxy = rotated.bounds
    # Each pass "covers" a strip +/- spacing/2 around it, so we inset the first
    # and last pass half a spacing from the edges and centre the stack. The pass
    # count is ceil(height/spacing): a 50 m band at 10 m spacing -> 5 passes at
    # 5,15,25,35,45 m (each covering its 10 m strip). This is the standard
    # lawnmower convention and is robust to sub-metre jitter in the bounds.
    height = maxy - miny
    n_passes = max(1, int(math.ceil(height / spacing_m - 1e-9)))
    used = (n_passes - 1) * spacing_m
    y0 = miny + (height - used) / 2.0
    pad = (maxx - minx) * 0.05 + 1.0  # extend lines past the bbox before clipping

    lines: list[LineString] = []
    for i in range(n_passes):
        y = y0 + i * spacing_m
        scan = LineString([(minx - pad, y), (maxx + pad, y)])
        clipped = rotated.intersection(scan)
        for seg in _ordered_lines_for_geom(clipped):
            # Order each chord left-to-right; boustrophedon flipping happens later.
            xs = sorted(seg.coords, key=lambda c: c[0])
            lines.append(LineString([xs[0], xs[-1]]))

    # Rotate the chords back into the metric frame.
    return [aff.rotate(ln, angle_deg, origin=poly_m.centroid, use_radians=False) for ln in lines]


def plan_survey(
    polygon_latlon: list,
    spacing_m: float,
    angle_deg: float | None = None,
    *,
    water: BaseGeometry | None = None,
) -> SurveyResult:
    """Plan a boustrophedon coverage route over a closed area polygon.

    Parameters
    ----------
    polygon_latlon:
        The survey area as a list of ``[lat, lon]`` vertices (a closed ring; the
        ring need not repeat its first point).
    spacing_m:
        Distance between parallel passes, in metres.
    angle_deg:
        Sweep direction in **compass-ish math degrees in the metric frame**; if
        omitted the polygon's longest axis is used (fewest, longest passes).
    water:
        Optional navigable-water polygon (lon/lat shapely geometry, as produced
        by :mod:`.water`).  When provided the survey polygon is clipped to the
        water boundary first so chords never land on shore; connecting legs that
        would exit the clipped area are rerouted along the polygon boundary.
        When *None* the same leg-repair is applied using the survey polygon
        itself as the constraint, so concave polygons never produce legs that
        cross their own notch.

    Returns ordered waypoints ``[{name, lat, lon}]`` (``WP1``.. / ``DEST``).
    Pure CPU work (shapely); run it in an executor.
    """
    pts = [(float(lat), float(lon)) for lat, lon in polygon_latlon]
    if len(pts) < 3:
        return SurveyResult(False, message="A survey area needs at least 3 points.")
    if spacing_m is None or spacing_m < MIN_SPACING_M:
        return SurveyResult(
            False, message=f"Spacing must be at least {MIN_SPACING_M:g} m."
        )

    # Project to a metric frame centred on the polygon (use the first vertex).
    proj = Projection.for_point(pts[0][1], pts[0][0])
    ring_m = [proj.point_to_metric(lon, lat) for lat, lon in pts]
    poly_m = Polygon(ring_m)
    if not poly_m.is_valid:
        poly_m = poly_m.buffer(0)
    if poly_m.is_empty or poly_m.geom_type != "Polygon":
        # buffer(0) of a self-intersecting ring may yield a MultiPolygon; take
        # the largest part.
        if poly_m.geom_type == "MultiPolygon" and not poly_m.is_empty:
            poly_m = max(poly_m.geoms, key=lambda g: g.area)
        else:
            return SurveyResult(False, message="Survey area polygon is degenerate.")

    # Clip to water when provided so chords stay on water, and set the
    # constraint used for leg validation.  Without water the survey polygon
    # itself is the constraint (keeps legs inside even for concave shapes).
    if water is not None:
        water_m = proj.to_metric(water)
        if not water_m.is_valid:
            water_m = water_m.buffer(0)
        constraint_m: BaseGeometry = poly_m.intersection(water_m)
        if constraint_m.is_empty:
            return SurveyResult(
                False, message="Survey polygon does not overlap with navigable water."
            )
        if not constraint_m.is_valid:
            constraint_m = constraint_m.buffer(0)
        if constraint_m.geom_type not in ("Polygon", "MultiPolygon"):
            return SurveyResult(
                False, message="Survey polygon clips to a degenerate water shape."
            )
    else:
        constraint_m = poly_m

    # For pass-count geometry (angle, bounds) use a single Polygon reference --
    # take the largest component when the constraint is a MultiPolygon.
    ref_poly: Polygon = (
        max(constraint_m.geoms, key=lambda g: g.area)
        if constraint_m.geom_type == "MultiPolygon"
        else constraint_m  # type: ignore[assignment]
    )

    sweep_ang = _longest_axis_angle(ref_poly) if angle_deg is None else float(angle_deg)

    # _sweep_lines clips every scan line to the constraint, so chords are
    # guaranteed to stay inside (handles both convex and concave shapes, and
    # multi-part water clipping).
    lines = _sweep_lines(constraint_m, float(spacing_m), sweep_ang)
    if not lines:
        return SurveyResult(
            False,
            message="Spacing too large for this area -- no passes fit.",
            spacing_m=float(spacing_m),
            angle_deg=sweep_ang,
        )

    # Boustrophedon ordering: walk the passes in sweep order, flipping every
    # other pass so the end of one connects to the start of the next.
    ordered_pts: list[tuple[float, float]] = []
    prev_end: tuple[float, float] | None = None
    for ln in lines:
        a, b = ln.coords[0], ln.coords[-1]
        if prev_end is not None:
            # Connect to whichever end of this pass is nearer the previous end,
            # so the connecting leg is the short turn (true boustrophedon).
            d_a = math.hypot(a[0] - prev_end[0], a[1] - prev_end[1])
            d_b = math.hypot(b[0] - prev_end[0], b[1] - prev_end[1])
            if d_b < d_a:
                a, b = b, a
        ordered_pts.append(a)
        ordered_pts.append(b)
        prev_end = b

    if len(ordered_pts) > MAX_WAYPOINTS:
        return SurveyResult(
            False,
            message=(
                f"{len(ordered_pts)} waypoints is too many to plan ({len(lines)} passes); "
                f"increase the spacing."
            ),
            passes=len(lines),
            spacing_m=float(spacing_m),
            angle_deg=sweep_ang,
        )

    # Repair connecting legs that exit the constraint (e.g. across a concave
    # notch or over a dry bank at the polygon edge).  Chord interiors are never
    # checked here -- they are already clipped to the constraint by _sweep_lines.
    ordered_pts = _repair_legs(ordered_pts, constraint_m)

    waypoints: list[dict] = []
    n = len(ordered_pts)
    for i, (x, y) in enumerate(ordered_pts):
        lon, lat = proj.point_to_lonlat(x, y)
        name = "DEST" if i == n - 1 else f"WP{i + 1}"
        waypoints.append({"name": name, "lat": lat, "lon": lon})

    warn = (
        f" Heads up: {len(waypoints)} waypoints is a lot to run -- wider spacing is easier."
        if len(waypoints) > WARN_WAYPOINTS else ""
    )
    return SurveyResult(
        ok=True,
        waypoints=waypoints,
        message=(
            f"Survey route: {len(lines)} passes, {len(waypoints)} waypoints, "
            f"{spacing_m:g} m spacing.{warn}"
        ),
        passes=len(lines),
        spacing_m=float(spacing_m),
        angle_deg=sweep_ang,
    )


# --------------------------------------------------------------------------- #
# Work Area spots: an even grid of discrete spots over a drawn area, visited in
# a serpentine order (the "smart pattern" for Work Area mode).
# --------------------------------------------------------------------------- #
MAX_WORK_SPOTS = 250


def plan_work_spots(polygon_latlon: list, spacing_m: float) -> SurveyResult:
    """Even grid of work spots inside an area polygon (``[[lat, lon], ...]`` ring),
    spaced ~``spacing_m`` apart and ordered serpentine (lawnmower) so the boat
    works them in a tidy sweep. Returns ``SurveyResult`` with waypoints
    ``[{name, lat, lon}]``. Pure CPU (shapely); run in an executor. Water-clipping
    is applied by the caller (``Runtime.plan_work_spots``)."""
    pts = [(float(lat), float(lon)) for lat, lon in polygon_latlon]
    if len(pts) < 3:
        return SurveyResult(False, message="A work area needs at least 3 points.")
    if spacing_m is None or spacing_m < MIN_SPACING_M:
        return SurveyResult(False, message=f"Spacing must be at least {MIN_SPACING_M:g} m.")

    proj = Projection.for_point(pts[0][1], pts[0][0])
    ring_m = [proj.point_to_metric(lon, lat) for lat, lon in pts]
    poly_m = Polygon(ring_m)
    if not poly_m.is_valid:
        poly_m = poly_m.buffer(0)
    if poly_m.is_empty or poly_m.area <= 0.0:
        return SurveyResult(False, message="Degenerate work area.")

    minx, miny, maxx, maxy = poly_m.bounds
    # Pre-guard: refuse a too-small spacing before generating the grid.
    if int((maxx - minx) / spacing_m + 1) * int((maxy - miny) / spacing_m + 1) > 50000:
        return SurveyResult(False, message="Spacing too small for this area.")

    rows: list[list[tuple[float, float]]] = []
    y = miny + spacing_m / 2.0
    ri = 0
    while y <= maxy:
        row = []
        x = minx + spacing_m / 2.0
        while x <= maxx:
            if poly_m.contains(Point(x, y)):
                row.append((x, y))
            x += spacing_m
        if ri % 2 == 1:
            row.reverse()          # serpentine: alternate row direction
        rows.append(row)
        ri += 1
        y += spacing_m

    flat = [p for row in rows for p in row]
    if not flat:
        return SurveyResult(False, message="No spots fit inside the area at this spacing.")
    if len(flat) > MAX_WORK_SPOTS:
        return SurveyResult(
            False,
            message=f"{len(flat)} spots is too many; increase spacing (max {MAX_WORK_SPOTS}).",
        )

    waypoints = [
        {"name": f"Spot {i + 1}", "lat": lat, "lon": lon}
        for i, (x, y) in enumerate(flat)
        for lon, lat in [proj.point_to_lonlat(x, y)]
    ]
    return SurveyResult(
        True, waypoints=waypoints, spacing_m=spacing_m,
        message=f"{len(waypoints)} spots on a {spacing_m:g} m grid.",
    )
