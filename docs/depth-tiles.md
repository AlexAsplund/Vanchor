# Depth-chart raster tiles (design)

> Server-rendered raster **tiles** for the static depth overlays (bottom
> composition + contours), replacing the live-vector canvas rendering. Design
> only — not built yet. Tracked by the tiling epic (label `roadmap`). When it
> ships, fold the still-true parts into [`llms/frontend.md`](llms/frontend.md) +
> [`llms/backend.md`](llms/backend.md) and delete the rest of this doc.

## Why

The composition overlay draws thousands of vector polygons live: a region is
~5 MB / ~200k vertices, fetched + parsed + reprojected on every meaningful move
(~400 ms on desktop localhost, multiple seconds on a phone talking to a Pi). The
[interim fix](../CHANGELOG.md) (edgeless + blur + region-cached fetch) helped,
but the vector approach fundamentally can't scale to the full 288k-polygon chart
or a whole-lake zoom-out, and every client re-does the work.

The data is **static** (imported charts change only on re-import). Static + dense
+ map-shaped is exactly what raster tiles are for: render once, serve everywhere,
and the browser + Leaflet handle caching, the zoom pyramid, and only-visible
tiles. Rendering server-side also lets every client — including weak phones and
offline devices on the boat WiFi — reuse one cached, good-looking render.

## Scope — tile the static layers only

| Layer | Today | Tiled? | Why |
|---|---|---|---|
| **Bottom composition** | vector polygons | **yes** | static, dense, the reported pain |
| **Depth contours** (isobaths) | vector lines | **yes** | static, dense, pairs with composition |
| Live soundings / depth heatmap | live points | **no** | changes every tick → tiling = constant rewrite = SD churn |
| Basemaps | tiles already | n/a | |

Tiling the two static layers is what makes the SD-write budget bounded. Dynamic
layers stay vector.

## Architecture

```
GET /api/depth/tiles/<layer>/<z>/<x>/<y>.png?v=<chart_hash>
      layer ∈ { composition, contours }
```

Standard XYZ / slippy web-mercator tiles, 256 px (512 for retina — open
question). Two independent tile sets (composition = blurred fill, contours =
crisp lines) stacked as two `L.TileLayer`s, preserving the current independent
toggles + opacity and the crisp-contours-over-soft-fill look.

**Renderer: Pillow, not matplotlib.** Per tile: query the layer for the tile
bbox (padded a few px) via the existing `composition_in(bbox)` /
`contours_in(bbox)`, rasterize at 2× supersample, downscale to the tile size for
smooth antialiased edges, save PNG. Colours reuse cmapper's YlOrBr ramp
(`at5/render/colors.py` logic). Pillow is already a dependency, far faster and
lower-RAM than a matplotlib figure per tile. Peak RAM per render = one small
image + one bbox query — bounded and flat, never a whole-lake buffer.

**Empty tiles** (no data in bbox) return a transparent PNG (cached like any
other) so blank regions aren't re-rendered on every pan.

## Cache — SD-friendly by construction

Key: `<data_dir>/tiles/<layer>/<chart_hash>/<z>/<x>/<y>.png`.

- **Lazy render → persist once.** On request: RAM LRU → disk → else render, write
  to disk, populate LRU, return. Static data means each tile is written **at most
  once, ever** — write-once-read-many, the SD-safe pattern (wear comes from
  rewrites, not one-time writes).
- **RAM LRU in front** (small, ~a few MB / N tiles) serves hot tiles with zero
  disk reads and caps RAM.
- **`chart_hash`** derives from the chart content (e.g. the `depthchart.npz`
  content hash, or a version counter bumped on import). A new import → new hash →
  new directory → lazy re-render of only what's viewed; the old directory is
  GC'd. This is also the tile-URL `?v=` param, so client/SW caches roll
  automatically.

## Invalidation modes (the "regenerate vs static" setting)

- **auto** (default): tiles keyed by `chart_hash`; re-import changes the hash, so
  new tiles render lazily and old ones are collected.
- **pinned / static**: freeze the hash — never re-render even if data changes,
  until manually cleared. Zero CPU, zero writes in steady state.

## Client + GUI

- Client renders nothing new: it swaps the vector `CompositionLayer`/contour layer
  for `L.TileLayer(.../composition/{z}/{x}/{y}.png?v=<hash>)` when tile-mode is on.
- **Offline** reuses the existing SW / offline tile cache + pre-cache + clear UI
  in `map-core.js` — depth tiles are just more tile URLs.
- Settings → Depth chart:
  - **Rendering:** Server tiles (default) · Live vectors (the existing overlay)
  - **Store tiles:** on server (SD) · on this device (offline)
  - **Update:** auto-regenerate on chart change · static (pinned)
  - Actions: **Pre-generate tiles now** · **Clear server cache** · **Clear this
    device's cache**
- Server clear: `POST /api/depth/tiles/clear` (optionally per layer / hash).

"Live vectors" is the client-render path, so we do **not** build a second
JS raster renderer — that stays deferred (low ROI, high complexity).

## Decisions

**Locked (Alex):** Pillow renderer; lazy render → persist to SD, hash-keyed.

**Recommended (proceed unless told otherwise):** tile static layers only (keep
live soundings vector); server-primary with the existing vector overlay as the
"client" mode; defer a real client-side PNG generator.

## Phasing

1. **Keystone** — tile endpoint + Pillow **composition** renderer + lazy disk
   cache (write-once) + RAM LRU. Client `TileLayer` behind a setting (default off
   until validated on a Pi).
2. **Contours** tile layer (crisp lines, edge clipping).
3. **Invalidation + control** — `chart_hash` keying, auto/pinned, clear
   endpoints, GUI settings, stale-hash GC.
4. **Offline** — SW precache + clear-UI integration.
5. **Pre-generate** button (batch write at import time) + polish.

## Open questions

- `chart_hash` source: `.npz` content hash vs an import version counter.
- Tile size 256 vs 512 (retina sharpness vs 4× the tiles).
- Max render zoom given data density (avoid rendering deeper than the data warrants).
- Contour line clipping / antialias at tile seams.
- Whether a combined `composition+contours` tile is worth it for the common case
  (fewer requests) vs the flexibility of two independent layers.
