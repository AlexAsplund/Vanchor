# Smart "Take me here" routing — research & implementation plan

Status: research / design only (no implementation). Author target: vanchor-ng backend.

## 1. Goal

Tap the map to choose a destination. The backend computes a **water-only** route
(avoiding land/islands) and returns it as a list of waypoints that the UI loads
into the **existing route editor, unstarted** — so the skipper reviews / edits /
approves before pressing Go. Two modes:

1. **Fastest** — shortest navigable water path, current position → destination.
2. **Along shoreline** — go to nearest shore (ending `X` m offshore), hug the
   shore at ~`X` m, and as soon as there is clear open-water line-of-sight to the
   destination, cut straight in.

## 2. Feasibility verdict — GO

Everything load-bearing was verified on this box (aarch64, Python 3.12, project
`.venv`):

- **Libraries install from prebuilt aarch64 wheels, no compilation:** `shapely
  2.1.2`, `networkx 3.6.1`, `pyproj 3.7.2`, `requests` (already present). No
  `osmnx`/`rasterio`/`scikit-image` needed — keep the dependency surface tiny.
- **Data fetch works:** an Overpass query over the sim area (59.62–59.70 N,
  13.26–13.40 E) returned ~400 KB / 10 polygons / 503 vertices in <1 s.
- **Geometry pipeline works:** building polygons, projecting to UTM 33N, inward
  `buffer(-30 m)`, `LineString` line-of-sight test, and `simplify()` all run in
  single-digit milliseconds.
- **Critical data caveat found & solved (see §3.2):** the sim start point sits in
  the lake **Visten** (OSM relation 287548), which OSM stores as a `natural=water`
  **multipolygon relation**, not a simple closed way. The water *is* mapped (it
  shows blue on the chart); the catch is purely how it's encoded. A naive "closed
  ways only" extraction found **zero** ways containing the boat and so reported it
  as *not in water*. Assembling the relation's `outer` member ways into rings with
  `shapely.ops.polygonize` fixed it (boat correctly inside the water body). This
  relation-assembly step is mandatory, not optional.

Risk is low and bounded; the main residual risks are OSM data gaps and Overpass
availability (both mitigated by offline caching).

## 3. Water/land geometry data

### 3.1 Source: OpenStreetMap via Overpass API

For an arbitrary lake we need a **water polygon** (possibly multiple, with island
holes). Lakes are tagged `natural=water`; large lakes/seas may instead use
`natural=coastline` ways (water is *to the left* of the way direction). Inland
Swedish lakes (Vänern, Fryken chain) are `natural=water`.

Recommended query (bbox `S,W,N,E`), POSTed to an Overpass endpoint with a real
`User-Agent` header (the public endpoint returns **406** without one — verified):

```overpassql
[out:json][timeout:60];
(
  way["natural"="water"](S,W,N,E);
  relation["natural"="water"](S,W,N,E);
  way["natural"="coastline"](S,W,N,E);
);
out geom;
```

`out geom;` inlines coordinates into every way *and* into relation members, so a
single request is self-contained — no second pass to resolve node refs. Endpoints
to try in order (fall back on error/429): `overpass-api.de`, `overpass.kumi.systems`.

Bbox sizing: query a generous box around `min/max(boat, dest)` padded by a margin
(e.g. +2 km, or +1× the boat→dest distance capped at ~20 km) so the route has room
to go around islands/peninsulas. For very large areas, cap and warn.

Why not the alternatives:
- **osmnx** — overkill (pulls in geopandas/pandas), road-network oriented. Skip.
- **GSHHG / prebuilt OSM water polygons (shapefiles)** — global, coarse for a
  small boat near shore, and a heavy offline payload. Good as an *optional*
  offline fallback layer, not the primary source.
- **Local `.osm.pbf` extract + pyosmium** — best for fully-offline production on
  the boat, but adds a C++/pyosmium dependency and a build step. Defer to a later
  phase; Overpass-with-cache covers the MVP and the realistic "fetch at the dock,
  run offline on the water" workflow.

### 3.2 Assembling polygons (the important part)

```
polys = []
for el in elements:
    if el.type == "way" and is_closed(el): polys.append(Polygon(coords(el)))
    elif el.type == "relation":
        outers = linestrings of members with role == "outer"
        inners = linestrings of members with role == "inner"
        for ring in polygonize(unary_union(outers)): polys.append(ring)
        # subtract inner rings (islands) as holes — see note
water = unary_union(polys)      # MultiPolygon
```

- Multipolygon **relations must be stitched** from their member ways
  (`polygonize` over the merged outer lines). Verified: this is what makes lakes
  like Visten / the Fryken chain appear. **Without it the boat reads as on land**
  even though the water is fully mapped.
- **Islands = holes.** Represent them either as polygon interiors (subtract inner
  rings) or simply keep `way`-tagged islands out of the water union. For routing,
  what matters is `water = navigable area`; build it as
  `water_union.difference(islands_union)` so island holes are real obstacles.
- For `natural=coastline` (if ever needed): merge ways with `linemerge`, then
  `polygonize`; water is the side consistent with way direction — for inland use
  this is an edge case, document and defer.
- `make_valid()` / `buffer(0)` to repair self-intersections from messy OSM data.

### 3.3 Caching offline (a real boat may be offline)

- On a successful fetch, **persist the assembled `MultiPolygon`** keyed by a
  rounded bbox (e.g. to 0.01°) under `vanchor_data/water_cache/`. Store as WKB
  (`shapely.wkb.dumps`) plus a small JSON sidecar (bbox, fetch timestamp, source).
  WKB is compact and reloads instantly.
- Cache lookup: if a cached polygon **covers** the requested boat+dest (with
  margin), use it and never touch the network. This gives the "fetch at the dock,
  use on the lake" story for free.
- Provide a **prefetch** path (CLI or `POST /api/route/cache {bbox}`) so the user
  can warm the cache for their lake before leaving connectivity.
- Offline + no cache covering the area → return a clear error the UI surfaces
  ("No offline chart for this area; connect once to download it"), and optionally
  fall back to a straight-line single-waypoint goto (current behavior).

### 3.4 Accuracy note

OSM lake outlines are good but not survey-grade; near-shore detail varies. Keep a
safety margin (the shoreline offset `X`, default ~20–30 m) and treat planned
routes as **advisory** — which is exactly why the route loads *unstarted* for the
skipper to approve. Document that narrow channels < ~2× boat beam may be missing
or mis-mapped.

## 4. Coordinate handling

All routing/buffering/distance math happens in a **local metric CRS**, never in
degrees. Use `pyproj.Transformer`:

- Pick UTM zone from the destination longitude (sim ≈ 13 E → EPSG:32633, UTM 33N),
  or a custom transverse-mercator centered on the area. `to_m` / `to_ll` transforms
  applied with `shapely.ops.transform`.
- Buffer distances, A*/visibility costs, the `X` offset, and `simplify` tolerance
  are then all in **metres**. Convert final waypoints back to lat/lon for the UI.

## 5. Fastest-route algorithm

**Recommendation: visibility graph over the water polygon (shapely + networkx).**

Rationale for a lake-sized, low-vertex polygon (sim area: ~500 boundary
vertices):

- The shortest obstacle-avoiding path in a polygon-with-holes is **piecewise
  linear with bends only at obstacle (island/shore) vertices** → a visibility
  graph gives the *exact* shortest water path, with naturally few, meaningful
  waypoints (bends at headlands/island tips). This matches "a series of
  waypoints" perfectly — far better than a grid path that needs heavy smoothing.
- A grid + A* (rasterize water, 8-connected A*) is simpler to reason about but
  yields staircased paths needing string-pulling, picks a resolution tradeoff
  (memory vs. missing thin channels), and is slower to get clean waypoints for a
  km-scale lake. Keep it as a documented **fallback** for pathological,
  very-high-vertex coastlines where the visibility graph’s O(n²) edge build hurts.

### Algorithm

1. `water_m = project(water_to_metric)`; ensure boat & dest are inside (see §8
   snapping).
2. **Nodes** = `{boat, dest} ∪ all polygon boundary vertices` (exterior + island
   interiors).
3. **Edges**: for each pair of nodes, the segment is valid iff it stays in water:
   `water_m.covers(LineString(a, b))`. Weight = Euclidean length (metres). Build
   with a prepared geometry (`shapely.prepared.prep(water_m)`) for fast repeated
   `covers`. For n≈500 that is ~125k segment tests — sub-second; if it gets large,
   prune by only connecting mutually-visible *convex* vertices and skip pairs
   farther than the bbox diagonal.
4. **Shortest path**: `networkx.shortest_path(G, boat, dest, weight="length")`
   (Dijkstra/A* with straight-line heuristic).
5. Output the node sequence → §7 (simplify + to lat/lon).

Performance: build is the cost; with prepared geometry and bbox/visibility pruning
this is well within an interactive request budget for lake-sized data. Cache the
graph per (water polygon, ) if repeated planning is needed.

## 6. Along-shoreline algorithm

Coast-hugging route at ~`X` m offshore, cutting straight to dest on clear sight.

Definitions (all metric):
- `offset = water_m.buffer(-X)` — the navigable region whose **boundary** is the
  locus of points exactly `X` m off the nearest shore (this is the path to hug).
  `buffer(-X)` may split into several polygons / vanish in narrow water — handle
  (see §8). Verified to run in ~1 ms on the sim polygon.
- `offset_ring = offset.boundary` (a MultiLineString following every shore +
  island at `X` m).

Steps:
1. **Entry point** — nearest point on `offset_ring` to the boat:
   `entry = nearest_points(boat, offset_ring)[1]`. First leg = boat → entry
   (this realizes "head to the nearest shoreline, ending X m off it"). If the
   boat is already ≤ `X` from shore / outside `offset`, snap onto the ring.
2. **Target on ring** — nearest ring point to the destination:
   `dexit = nearest_points(dest, offset_ring)[1]`.
3. **Follow the shore** from `entry` to `dexit` *along the ring*. Implement as a
   graph/walk along ring vertices (densify the ring to ≤ `X`-spaced points so
   line-of-sight checks below are meaningful), choosing the **shorter of the two
   directions** around the relevant ring component. Stay on one connected ring
   component; if entry and dexit are on different components (different
   islands/basins), fall back to the fastest-route graph (§5) between them.
4. **Line-of-sight shortcut** — *the key behavior.* While walking the ring,
   after each step test sight to the destination:
   - **Clear sight is defined as:** `water_m.covers(LineString(p, dest))` **and**
     the straight segment stays at least `clearance` m off shore for its whole
     length, i.e. `LineString(p, dest).buffer(clearance)` is covered by `water_m`
     (or equivalently the segment does not come within `clearance` of land).
     Use `clearance = min(X, safety_margin)` so the cut-in isn’t a hull-scraping
     diagonal. Plain "doesn’t cross land" (`covers`) is the minimum; the buffered
     test is the recommended, safer definition.
   - At the **first** ring point with clear sight, stop following and append a
     straight leg `p → dest`. This yields: approach → hug → cut straight in.
5. If sight is never clear until `dexit`, the route is entry → (full shore walk)
   → dexit → dest.
6. Output the polyline boat → entry → [ring points…] → cut point → dest → §7.

This naturally handles islands (the ring wraps them) and "boat starts mid-lake"
(leg 1 brings it to the offset ring). `X` is user-settable (`shoreline_offset_m`).

## 7. Output as an editable route

- Concatenate legs into one metric `LineString`, `simplify(tol)` (Douglas–Peucker
  via shapely) with `tol ≈ X/2` or ~5–15 m to get a sensible waypoint count.
  Verified: `simplify` collapses redundant collinear points cleanly.
- Cap total waypoints (e.g. ≤ 50); if still over, increase tol or down-sample.
- Transform each vertex back to lat/lon (`to_ll`), name them `WP1..WPn` (keep the
  final one e.g. `DEST`).
- Return `{waypoints:[{name,lat,lon}], meta:{distance_m, mode, ...}}`.

## 8. Pitfalls & edge cases (and handling)

- **Boat/dest on land or in unmapped inlet** → snap to nearest water:
  `nearest_points(pt, water_m)[1]`; if the snap distance exceeds a threshold,
  return an error ("destination is on land / outside known water"). The UI can
  still show the snapped point for approval.
- **`buffer(-X)` empties or fragments** (narrow water) → reduce `X` adaptively
  (try X, X/2, X/4) or, for fastest-mode-style sections, route on `water_m`
  directly. Surface a warning when `X` had to shrink.
- **Multiple water polygons / different basins** → operate on the polygon
  containing the boat; if dest is in a different connected component with no water
  path, return "no water route" rather than a bogus straight line.
- **Very long paths** → cap bbox + node count; if exceeded, return a coarse route
  and a warning, or refuse.
- **Offline / no cache** → §3.3: clear error, optional straight-line fallback.
- **Invalid OSM geometry** → `make_valid()`/`buffer(0)` before use.
- **Degrees vs metres** → never compute distance/buffer in 4326 (§4).
- **Overpass 406 / 429** → real `User-Agent`, endpoint fallback, backoff; prefer
  cache.

## 9. Proposed backend API

New module `src/vanchor/nav/planner.py` (pure geometry, no event loop — unit-
testable like the rest of `nav/`). New REST endpoints in `app.py`:

```
POST /api/route/plan
  body: { dest_lat, dest_lon, mode: "fastest"|"shoreline",
          shoreline_offset_m?: number (default e.g. 25),
          start_lat?, start_lon? (default = current position) }
  200:  { waypoints: [ {name, lat, lon}, ... ],
          meta: { mode, distance_m, offset_m, simplified_from, source, cached } }
  4xx:  { error: "...", detail }   # on-land, no-water-route, offline-no-cache, area-too-big

POST /api/route/cache            # optional: prefetch/warm offline chart
  body: { south, west, north, east }
  200:  { cached: true, bytes, vertices }
```

`/api/route/plan` does **not** start navigation — it only returns waypoints. The
existing `goto` command (which *does* start `WaypointMode`) is untouched; the new
endpoint deliberately stops short of it so the route is reviewable.

The planner runs CPU-bound shapely work; call it via
`asyncio.get_event_loop().run_in_executor(None, plan, ...)` so it doesn’t block the
~5 Hz telemetry loop. First-time Overpass fetch may take a few seconds — fine for
a one-shot request; the UI shows a spinner.

## 10. UI flow

The integration target already exists. `VA.map.setPending(arr)` +
`renderWpList()` load waypoints into the editor **without starting** them
(`app.js` only sends the `goto` command from `startRoute()` on the explicit Go
button). So:

1. Add a "Take me here" arm button + a mode toggle (Fastest / Along shore) and an
   offset slider (`shoreline_offset_m`), near the existing go-to controls
   (`app.js` `gotoArm` / `gotoAction`, `map.js` `setGotoArmed`).
2. On armed map click, instead of `gotoTo()` (which sends `goto` immediately),
   `POST /api/route/plan {dest_lat, dest_lon, mode, shoreline_offset_m}`.
3. On success: `VA.map.setPending(resp.waypoints.map(w=>({name:w.name,lat:w.lat,lon:w.lon})))`
   then `renderWpList()`. The route draws as the dashed pending polyline
   (`map.js drawWaypoints` / `routeLine`) — reviewable/editable exactly like
   hand-placed waypoints. Optionally overlay the water polygon for context.
4. The skipper edits/deletes points, then presses the existing **Go** (`wp-go` →
   `startRoute()` → `goto`) to start `WaypointMode`. Nothing about start/approve
   needs new backend plumbing.
5. On error: show the message (on-land, offline, etc.); optionally offer
   "straight line anyway" (a single-waypoint pending).

No telemetry-contract change required for the core flow; optionally add the water
polygon to a REST endpoint (`GET /api/chart/water?bbox=...`) for the map overlay.

## 11. Phased plan / MVP

**Phase 0 — deps (done/verified).** Add `shapely`, `networkx`, `pyproj`,
`requests` to an optional extra, e.g. `[project.optional-dependencies] routing`.
All install from aarch64 wheels.

**Phase 1 — data layer.** `nav/water.py`: Overpass fetch (User-Agent + endpoint
fallback) → relation-aware polygon assembly (`polygonize`, islands as holes,
`make_valid`) → project to metric → WKB cache in `vanchor_data/water_cache/`.
Unit-test against a saved fixture of the sim-area response (no live network in
tests).

**Phase 2 — fastest route (MVP).** `nav/planner.py`: snap, visibility graph,
networkx shortest path, simplify, to lat/lon. `POST /api/route/plan` (fastest
only). Wire UI button → `setPending`. **This is the smallest end-to-end win in the
sim.**

**Phase 3 — along-shoreline.** Add `buffer(-X)` ring walk + line-of-sight cut-in
and the offset slider / mode toggle.

**Phase 4 — robustness & offline.** Prefetch endpoint, adaptive `X`, fragmented-
buffer handling, error UX, optional water overlay, optional `.osm.pbf` offline
source.

## 12. Effort estimate

- Phase 0: trivial (deps + extra).
- Phase 1 (data + assembly + cache + tests): ~1 day.
- Phase 2 (fastest + API + UI wire-up): ~1–1.5 days.
- Phase 3 (shoreline mode + UI controls): ~1–1.5 days.
- Phase 4 (offline/robustness polish): ~1 day, incremental.

≈ 4–5 focused days to a polished feature; a usable **fastest-route MVP in the sim
in ~2 days**.

## 13. Key risks

1. **OSM data gaps / accuracy near shore** — mitigated by the offset margin and
   the mandatory human approval step (route loads unstarted).
2. **Multipolygon assembly correctness** — the verified `polygonize` step is
   essential; cover it with the fixture test (boat-in-water assertion).
3. **Offline operation** — solved by WKB cache + prefetch; degrade to clear error
   / straight-line fallback when no chart is available.
4. **Visibility-graph scaling** on very detailed coastlines — bounded bbox,
   prepared-geometry `covers`, optional grid+A* fallback.
5. **Overpass availability/limits** — User-Agent, endpoint fallback, backoff,
   cache-first.

## Appendix — verified facts (this box)

- `uname -m` = aarch64; Python 3.12.3; project `.venv`.
- `pip install shapely networkx pyproj requests` → `shapely 2.1.2`,
  `networkx 3.6.1`, `pyproj 3.7.2` from manylinux aarch64 wheels, no build.
- Overpass POST needs a `User-Agent` (else **406**); with it, sim-area water query
  returned 200, ~400 KB, 10 elements, 503 vertices, <1 s.
- Naive closed-way assembly → **zero** ways contain boat at (59.66275, 13.32247),
  so it reads as **not in water**. Relation `outer`-member assembly via
  `polygonize` → **boat in water**, inside lake **Visten** (OSM relation 287548).
  The water is correctly mapped; the issue is relation encoding, not a data gap.
- `project→UTM33N`, `buffer(-30)`, `LineString` LOS `covers`, `simplify` all OK,
  ms-scale.
