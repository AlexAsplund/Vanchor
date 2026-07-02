"""Roadmap item 33 -- visibility-graph speedup (lazy A*) for Pi-class planning.

Two things are asserted here:

1. **Route equivalence.** The new lazy-A* core (``_lazy_shortest_path``) must
   return the *same optimal path length* as an independent, eager visibility
   graph + Dijkstra oracle built directly in this test (no shared code with the
   implementation). Proven across several representative scenarios: open water,
   around one island, into a bay, and the large circular basin.

2. **Speedup.** On a water polygon near the ``MAX_PLAN_VERTS`` cap, a plan must
   complete quickly AND perform *far fewer* visibility (``covers``) tests than
   the eager O(n^2) build would -- we count them by wrapping the single choke
   point ``routing._segment_ok``. The assertion is on the RATIO/count (robust,
   not flaky), with only a generous wall-clock sanity bound.
"""

from __future__ import annotations

import math
import time

import pytest
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.prepared import prep

from vanchor.nav import routing
from vanchor.nav.routing import Projection, _boundary_vertices, _plan_fastest_metric


# --------------------------------------------------------------------------- #
# Independent eager oracle (no shared code with the implementation)
# --------------------------------------------------------------------------- #
def _eager_shortest_length(
    water_m, start_m: Point, dest_m: Point, soft_m=None, soft_penalty: float = 1.0
) -> float | None:
    """Brute-force shortest water-only path length via a *full* visibility graph.

    Builds every start/dest/vertex pair's edge eagerly (O(n^2) covers tests) and
    runs Dijkstra. This is the reference the lazy A* must match. Node set mirrors
    ``_plan_fastest_metric``'s unbudgeted construction (start, dest, then all
    boundary vertices, deduped to mm) so the two search the *same* graph.
    """
    prepared = prep(water_m)
    soft_prep = prep(soft_m) if (soft_m is not None and not soft_m.is_empty
                                 and soft_penalty > 1.0) else None
    verts = _boundary_vertices(water_m)
    nodes = [(start_m.x, start_m.y), (dest_m.x, dest_m.y), *verts]
    seen: dict[tuple[float, float], int] = {}
    unique: list[tuple[float, float]] = []
    for nd in nodes:
        key = (round(nd[0], 3), round(nd[1], 3))
        if key not in seen:
            seen[key] = len(unique)
            unique.append(nd)

    n = len(unique)
    adj: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    for i in range(n):
        ax, ay = unique[i]
        for j in range(i + 1, n):
            seg = LineString([(ax, ay), unique[j]])
            if prepared.covers(seg):
                w = seg.length
                if soft_prep is not None and soft_prep.intersects(seg):
                    w *= soft_penalty
                adj[i].append((j, w))
                adj[j].append((i, w))

    import heapq

    dist = [math.inf] * n
    dist[0] = 0.0
    pq = [(0.0, 0)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist[u]:
            continue
        if u == 1:
            return d
        for v, w in adj[u]:
            nd = d + w
            if nd < dist[v]:
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist[1] if math.isfinite(dist[1]) else None


def _line_len(line: LineString | None) -> float | None:
    return None if line is None else line.length


# --------------------------------------------------------------------------- #
# Scenario geometries (metric, built directly in a local UTM projection)
# --------------------------------------------------------------------------- #
LAT0, LON0 = 59.0, 13.0
_PROJ = Projection.for_point(LON0, LAT0)


def _rect(cx, cy, hx, hy) -> Polygon:
    return Polygon([(cx - hx, cy - hy), (cx + hx, cy - hy),
                    (cx + hx, cy + hy), (cx - hx, cy + hy)])


def _open_water():
    """A plain rectangular lake -- start sees dest directly."""
    x0, y0 = _PROJ.point_to_metric(LON0, LAT0)
    water = _rect(x0, y0, 600.0, 400.0)
    return water, Point(x0 - 500, y0), Point(x0 + 500, y0)


def _around_island():
    """A lake with a big central island squarely between start and dest."""
    x0, y0 = _PROJ.point_to_metric(LON0, LAT0)
    outer = _rect(x0, y0, 600.0, 400.0)
    island = _rect(x0, y0, 150.0, 250.0)  # blocks the straight line
    water = Polygon(outer.exterior.coords, [island.exterior.coords])
    return water, Point(x0 - 500, y0), Point(x0 + 500, y0)


def _into_a_bay():
    """An L-shaped basin: start in one arm, dest in the other. The straight line
    crosses the missing (land) corner, so the path must round the single reflex
    inner corner -- exercising both the reflex filter and the graph search."""
    x0, y0 = _PROJ.point_to_metric(LON0, LAT0)
    water = Polygon([
        (x0 - 600, y0 - 600),   # bottom-left
        (x0 + 400, y0 - 600),   # bottom-right
        (x0 + 400, y0 - 200),   # up the right side of the bottom arm
        (x0 - 200, y0 - 200),   # REFLEX inner corner (the headland to round)
        (x0 - 200, y0 + 400),   # up the left arm
        (x0 - 600, y0 + 400),   # top-left
    ])
    start = Point(x0 + 300, y0 - 400)   # in the bottom arm
    dest = Point(x0 - 400, y0 + 200)    # in the left arm (not line-of-sight)
    return water, start, dest


def _big_basin():
    """A large near-circular lake (~ the cap) with a blocking island."""
    x0, y0 = _PROJ.point_to_metric(LON0, LAT0)
    outer = Point(x0, y0).buffer(1500.0, quad_segs=64)
    island = Point(x0, y0).buffer(300.0, quad_segs=32)
    water = Polygon(outer.exterior.coords, [island.exterior.coords])
    return water, Point(x0 - 1300, y0), Point(x0 + 1300, y0)


SCENARIOS = {
    "open_water": _open_water,
    "around_island": _around_island,
    "into_a_bay": _into_a_bay,
    "big_basin": _big_basin,
}


# --------------------------------------------------------------------------- #
# 1. Route equivalence: lazy A* == eager oracle (length)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_lazy_matches_eager_oracle_length(name):
    water, start, dest = SCENARIOS[name]()
    lazy = _line_len(_plan_fastest_metric(start, dest, water))
    oracle = _eager_shortest_length(water, start, dest)
    assert lazy is not None, f"{name}: lazy planner found no route"
    assert oracle is not None, f"{name}: oracle found no route"
    # Same graph, same optimum -> identical length (float tolerance for tie paths).
    assert lazy == pytest.approx(oracle, rel=1e-9, abs=1e-6), (
        f"{name}: lazy {lazy:.4f} != oracle {oracle:.4f}"
    )


def test_lazy_matches_eager_with_soft_penalty():
    """The soft near-shallow penalty band must not change the optimum either."""
    water, start, dest = _around_island()
    x0, y0 = _PROJ.point_to_metric(LON0, LAT0)
    soft = _rect(x0, y0 - 330, 250.0, 60.0)  # a band the lower detour crosses
    lazy = _line_len(_plan_fastest_metric(
        start, dest, water, soft_m=soft, soft_penalty=4.0))
    oracle = _eager_shortest_length(water, start, dest, soft_m=soft, soft_penalty=4.0)
    assert lazy is not None and oracle is not None
    assert lazy == pytest.approx(oracle, rel=1e-9, abs=1e-6)


def test_around_island_actually_bends():
    """Sanity: the island scenario is a real detour (not line-of-sight), so the
    equivalence test above is exercising the graph search, not the shortcut."""
    water, start, dest = _around_island()
    line = _plan_fastest_metric(start, dest, water)
    assert line is not None
    assert line.length > start.distance(dest) + 1.0  # genuinely longer than direct


# --------------------------------------------------------------------------- #
# 2. Speedup: covers() count is a small fraction of eager n^2
# --------------------------------------------------------------------------- #
def _big_capped_water():
    """A many-vertex water polygon near MAX_PLAN_VERTS, with a blocking island so
    the search must run the graph (not take the line-of-sight shortcut)."""
    x0, y0 = _PROJ.point_to_metric(LON0, LAT0)
    outer = Point(x0, y0).buffer(1500.0, quad_segs=100)   # ~400 verts
    island = Point(x0, y0).buffer(250.0, quad_segs=64)    # ~256 verts
    water = Polygon(outer.exterior.coords, [island.exterior.coords])
    return water, Point(x0 - 1300, y0), Point(x0 + 1300, y0)


def test_lazy_astar_does_far_fewer_covers_than_eager(monkeypatch):
    water, start, dest = _big_capped_water()
    n = len(_boundary_vertices(water)) + 2  # + start + dest nodes
    eager_pairs = n * (n - 1) // 2          # what the old build would test

    calls = {"n": 0}
    real = routing._segment_ok

    def counting(prepared, seg):
        calls["n"] += 1
        return real(prepared, seg)

    monkeypatch.setattr(routing, "_segment_ok", counting)

    t0 = time.perf_counter()
    line = _plan_fastest_metric(start, dest, water)
    elapsed = time.perf_counter() - t0

    assert line is not None, "planner found no route on the big basin"
    # Deterministic ratio assertion (the real point of the test): lazy A* tests a
    # small fraction of the eager pair count. Eager would be ~n^2/2; we demand at
    # least a 5x reduction (in practice far more) and a hard sub-n^2 bound.
    assert calls["n"] < eager_pairs / 5.0, (
        f"expected << {eager_pairs} covers, did {calls['n']}"
    )
    # Generous wall-clock sanity bound (NOT the primary assertion): even a slow Pi
    # is far under this; here it should be a fraction of a second.
    assert elapsed < 10.0, f"plan took {elapsed:.2f}s (regressed?)"


def test_covers_results_are_cached_within_a_search(monkeypatch):
    """A single lazy-A* search never visibility-tests the same vertex pair twice
    (the per-plan cache holds)."""
    water, start, dest = _around_island()
    prepared = prep(water)
    verts = _boundary_vertices(water)
    nodes = [(start.x, start.y), (dest.x, dest.y), *verts]
    seen: dict = {}
    unique: list = []
    for nd in nodes:
        key = (round(nd[0], 3), round(nd[1], 3))
        if key not in seen:
            seen[key] = len(unique)
            unique.append(nd)

    seen_pairs: set[tuple] = set()
    dupes = {"n": 0}
    real = routing._segment_ok

    def tracking(prep_geom, seg):
        key = tuple(sorted(seg.coords))
        if key in seen_pairs:
            dupes["n"] += 1
        seen_pairs.add(key)
        return real(prep_geom, seg)

    monkeypatch.setattr(routing, "_segment_ok", tracking)
    line = routing._lazy_shortest_path(unique, prepared, None, 1.0, None)
    assert line is not None
    assert dupes["n"] == 0, f"{dupes['n']} redundant covers() calls -- cache leaked"
