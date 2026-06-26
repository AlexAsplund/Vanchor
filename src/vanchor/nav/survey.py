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

from shapely.geometry import LineString, MultiLineString, Polygon
from shapely.geometry.base import BaseGeometry

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

    sweep_ang = _longest_axis_angle(poly_m) if angle_deg is None else float(angle_deg)

    lines = _sweep_lines(poly_m, float(spacing_m), sweep_ang)
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
