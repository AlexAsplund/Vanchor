"""Along-contour route: click a depth contour on the chart -> a track that follows
that isobath.

Imported contours arrive as many short ``{d, pts}`` polyline pieces all sharing a
handful of discrete depths. Tracing the single clicked piece would give a stub, so
the smart bit here is to **chain together the same-depth pieces** whose endpoints
coincide into one continuous line through the click, then return it as an ordered,
simplified route (closed isobaths come back as a loop). The caller windows the
contours around the click so this stays fast even with tens of thousands loaded.

Pure CPU (shapely); run it in an executor.
"""

from __future__ import annotations

import math

from shapely.geometry import LineString, Point

from .water import Projection


def _chain(polylines: list[list[tuple]], tol: float) -> list[list[tuple]]:
    """Connect polylines sharing near-coincident endpoints (<= ``tol`` m) into
    longer chains. O(n^2) in the windowed piece count (bounded by the caller)."""
    rem = [list(p) for p in polylines]
    used = [False] * len(rem)
    chains: list[list[tuple]] = []
    close = lambda a, b: math.dist(a, b) <= tol  # noqa: E731
    for i in range(len(rem)):
        if used[i]:
            continue
        chain = rem[i][:]
        used[i] = True
        extended = True
        while extended:
            extended = False
            for j in range(len(rem)):
                if used[j]:
                    continue
                pj = rem[j]
                if close(chain[-1], pj[0]):
                    chain += pj[1:]
                elif close(chain[-1], pj[-1]):
                    chain += pj[-2::-1]
                elif close(chain[0], pj[-1]):
                    chain = pj[:-1] + chain
                elif close(chain[0], pj[0]):
                    chain = pj[:0:-1] + chain
                else:
                    continue
                used[j] = True
                extended = True
        chains.append(chain)
    return chains


def contour_route_near(
    lat: float, lon: float, contours: list[dict], *,
    max_snap_m: float = 120.0, max_waypoints: int = 80,
    simplify_m: float = 10.0, join_tol_m: float = 12.0,
) -> dict:
    """Nearest imported contour to (lat, lon), chained with its same-depth pieces
    into a continuous track and returned as an ordered route. ``contours`` are the
    windowed ``[{d, pts:[[lat,lon],...]}]``. Returns
    ``{ok, waypoints:[{name,lat,lon}], depth_m, loop, message}``."""
    proj = Projection.for_point(lon, lat)
    click = proj.point_to_metric(lon, lat)
    cpt = Point(click)

    polys: list[tuple[float, list[tuple]]] = []
    for c in contours:
        pts = c.get("pts") or []
        if len(pts) < 2:
            continue
        polys.append((c.get("d"), [proj.point_to_metric(p[1], p[0]) for p in pts]))
    if not polys:
        return {"ok": False, "waypoints": [], "message": "No depth contours here."}

    # The contour piece nearest the click decides which isobath (depth) to trace.
    nearest = min(polys, key=lambda dp: LineString(dp[1]).distance(cpt))
    if LineString(nearest[1]).distance(cpt) > max_snap_m:
        return {"ok": False, "waypoints": [],
                "message": "No contour line near there — click closer to one."}
    depth = nearest[0]

    same = [m for (d, m) in polys if d == depth]
    chains = _chain(same, join_tol_m)
    chain = min(chains, key=lambda ch: LineString(ch).distance(cpt))
    if len(chain) < 2:
        return {"ok": False, "waypoints": [], "message": "Contour too short to follow."}

    closed = len(chain) > 3 and math.dist(chain[0], chain[-1]) <= join_tol_m
    if closed:
        # Rotate the ring to start at the point nearest the click, and re-close it.
        i0 = min(range(len(chain)), key=lambda i: math.dist(chain[i], click))
        chain = chain[i0:] + chain[: i0 + 1]
    elif math.dist(chain[-1], click) < math.dist(chain[0], click):
        chain = chain[::-1]  # start from the end nearest the click

    line = LineString(chain)
    coords = list(line.simplify(simplify_m).coords)
    tol = simplify_m
    while len(coords) > max_waypoints and tol < 5000.0:
        tol *= 1.6
        coords = list(line.simplify(tol).coords)

    waypoints = [
        {"name": f"C{i + 1}", "lat": la, "lon": lo}
        for i, (x, y) in enumerate(coords)
        for lo, la in [proj.point_to_lonlat(x, y)]
    ]
    if len(waypoints) < 2:
        return {"ok": False, "waypoints": [], "message": "Contour too short to follow."}
    length_m = round(line.length)
    return {
        "ok": True, "waypoints": waypoints, "depth_m": depth, "loop": closed,
        "message": (f"Following the {depth:g} m contour "
                    f"({'loop, ' if closed else ''}{length_m} m, {len(waypoints)} waypoints)."),
    }
