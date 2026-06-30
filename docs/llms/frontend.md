# Front-end (web UI) developer guide

> Part of the `docs/llms/` developer guide. 🔁 **If you add/rename a JS module,
> a `VA.*` API, a map overlay, a Settings card, or a dialog — update this file**
> (and add new scripts to BOTH `index.html` and `sw.js`, see Gotchas).

## Shape of the front end

- **No build step, no framework.** Plain ES (modern Safari/Chrome target),
  Leaflet for the map, uPlot for charts — all vendored under `static/vendor/`.
  Your only compile gate is `node --check <file>.js`.
- **One module per feature.** Each `static/*.js` is an IIFE that attaches to the
  global `VA` namespace and wires its own DOM by id. `<script>` tags at the end
  of `index.html` load them in order.
- **Hubs:** `core.js` (the `VA` namespace + WebSocket), `map.js` (Leaflet + the
  `VA.map` API, the single biggest file), `app.js` (top-bar/HUD/route-editor glue
  + `VA.routeEditor`). Treat these three as shared — don't let parallel agents
  edit them at once.

## The `VA` contract (`core.js`)

| API | Purpose |
|-----|---------|
| `VA.last` | latest telemetry object (the `/ws` frame). Read fields from here. |
| `VA.simEnabled` | `true` in simulation (gate sim-only UI on this). |
| `VA.onTelemetry(fn)` | subscribe; `fn(t)` runs every frame (~5 Hz). |
| `VA.send(cmd)` | send a command `{type:"...",...}` (→ `/api/command`). |
| `VA.getJSON(url)` / `VA.postJSON(url, body)` | REST helpers. |
| `VA.onConnState(fn)` / `VA.connect()` | connection state / (re)connect the socket. |
| `VA.setText(id,v)`, `VA.fmt`, `VA.num`, `VA.fin`, `VA.continuousAngle` | small DOM/format helpers. |
| `VA.logLine`, `VA.clearLog` | the NMEA/console log. |

Always guard telemetry reads (`VA.fin(...)`, `VA.last && ...`) — fields may be
absent early or on hardware.

## The `VA.map` API (`map.js`)

| API | Purpose |
|-----|---------|
| `VA.map.leaflet` | the raw `L.Map` (use `.distance`, `.on`, panes, etc.). |
| `VA.map.addOverlay(name, layer, {onToggle, persistKey})` | register a layer into the **single** layers-control panel (toggleable + persisted). This is how *all* overlays (sea marks, depth, contours, catches, no-go, reference grid) appear. |
| `VA.map.addClickConsumer(fn)` | `fn(lat,lon)` runs on map click; **return `true` to consume** the click (so you don't fight go-to / markers). Consume only while *your* mode is armed. |
| `VA.map.setOnMapClick(fn)` | the fallback click handler (`fn(lat,lon,armed)`). |
| `VA.map.setGotoArmed/isGotoArmed`, `setGotoMarker/clearGotoMarker` | go-to ("tap map") state. |
| `VA.map.addPending(lat,lon)`, `pending()`, `setPending(arr)`, `redrawWaypoints()`, `onWaypointChange(fn)`, `onRouteEdit(fn)` | the pending-route-waypoint editor. |
| `VA.map.setDepthShow`, `setContourShow`, `redrawDepth` | depth overlays. |
| `VA.routeEditor` (in `app.js`) | `setLoop/clearLoop/refresh` — refresh the route list after editing pending waypoints. |

## Patterns (copy these)

- **Armed map interaction** (go-to, markers, island pick, measure): register a
  click consumer that returns `true` *only while armed*; disarm on use; never
  consume otherwise. See `markers.js`, `island.js`, `measure.js`.
- **A new overlay**: build an `L.Layer`/`L.GridLayer`, register it with
  `VA.map.addOverlay(name, layer, {onToggle, persistKey})`; it lands in the
  layers panel and its on/off is persisted (`vanchor-map-layers` localStorage).
  Default-on layers: add to the map when there's no saved pref.
- **A dialog**: use a native `<dialog>` + themed CSS (see `selectboat.js`,
  `alerts.js`).
- **An on-map label/menu**: a Leaflet `divIcon` marker + an `L.popup` keep it
  anchored in map space (see `measure.js`) — don't pin readouts to `#map`
  corners where the top bar can occlude them.
- **Pointer, not mouse**, for drawing/drag gestures, so touch works
  (`measure.js` uses `pointerdown/move/up` on `map.getContainer()`).

## Layout & styling

- `index.html` — the top bar (status chips + alert bell + menu), the map, the
  mode rail / contextual panels (right), the bottom HUD + steering gauge, and the
  **Settings drawer** (`#settings`) whose cards are grouped into labelled
  categories (`.drawer-section`).
- `style.css` — theme via CSS vars: `--accent`/`--accent-rgb`, `--muted`,
  `--line`, `--text`, `--faint`, `--glass`/`--glass-solid`, `--r`, `--shadow`,
  `--font-display`. `body.light` = light theme; `body.mobile` = phone layout
  (`mobile.js`, full-bleed map + bottom sheet). Reuse the vars — never hard-code
  colours.
- HUD/instrument opacity is driven by the `--hud-opacity` var off the
  `#hud-opacity` slider (`app.js`).

## The PWA service worker (`sw.js`)

- Strategy is **network-first** for the same-origin shell (fresh from the Pi
  every load; cache only as offline fallback). Don't revert to cache-first — that
  was the "my changes don't show up" bug.
- `const VERSION` is the cache name. **Bump it when you change shell assets.**
- The `SHELL` precache list enumerates every JS file. **When you add a
  `static/*.js`, add it to `SHELL` *and* to `index.html`'s script tags.**
- `/api`, `/ws`, and tile hosts are bypassed (never cached by the SW).

## Recipes

- **Add a feature module**: create `static/feature.js` (IIFE, boot-retry until
  `VA`/`VA.map` ready), add its `<script>` to `index.html`, add it to `sw.js`
  `SHELL`, add any markup to `index.html` + themed CSS, wire via `VA.*`.
  `node --check` it; verify ids are unique.
- **Add a Settings card**: add a `<details class="card" id="...">` under the
  right `.drawer-section` in `index.html`; lazy-load its data on `toggle`.
- **Surface a sim-only control**: gate visibility on `VA.simEnabled` /
  `VA.last.sim_enabled`.

## Devices card (`devices.js`)

Settings → **Devices** section → "Devices & hardware" (`#devices-card`). Lets the
user choose the master **Mode** (Simulation vs Hardware), a **source** per device
(GPS / Compass / Depth → Sim · Serial · NMEA; Motor → Sim · Serial · Both — `""`
in the select = Auto/null, "follows mode"), the **serial ports + baud** (shown
only when any source is `serial`), and the **NMEA TCP** bridge. Lazy-loads
`GET /api/config/devices` on first `toggle`; **Save** POSTs `{hardware, nmea_tcp}`
to `POST /api/config/devices` and shows "Saved — restart the app to apply"
(changes apply on **restart**, not hot-swapped); "Reset to current" re-fetches.
**Needs the matching `/api/config/devices` backend** — if that GET 404s (older
backend) the card degrades to an "unavailable" notice. Empty source select →
`null`; empty port text → `null`; baudrate/port coerced to numbers.

## Depth overlays (`map-depth.js`)

Three depth overlays, each registered via `VA.map.addOverlay(...)` so they land in
the single layers panel (toggleable + persisted). All are canvas overlays sharing
`CanvasOverlayMixin` and all clip to the OSM water polygon. Loads after
`map-core.js` and registers the **last** built-in overlays, then calls
`ctx.restoreLayers()` — so it must load before any later overlay-registering
module (Catches, heatmap, no-go).

- **Depth map** — the `GridLayer` heatmap: gridded soundings from
  `GET /api/depth/grid` painted into one canvas (low-res offscreen, bilinear
  upscale) as a continuous bathymetric field. Selectable palettes (Angler /
  Nautical, `vanchor-depth-palette`), relief/hillshade (a render *mode* of the
  grid — NW-light Lambert shade baked into the same offscreen pass, not a separate
  canvas; default on, own `vanchor-depth-relief` pref), and a depth-range
  highlight band. A **"Colour by: Depth / Bottom hardness"** selector recolours
  the *same* grid by switching the fetched `field` (and the ramp + legend);
  relief/highlight are depth-only and disable under hardness.
- **Depth contours** — the `ContourLayer`: explicit imported isobaths from
  `GET /api/depth/contours` are **preferred** (`setExplicit`, batched by depth
  level + sparse haloed labels). When none are returned for the window it falls
  back to deriving contours from the same grid via marching squares
  (`setData` → chain segments → Chaikin smooth → stroke).
- **Bottom composition** — the `CompositionLayer`: filled translucent YlOrBr
  polygons from `GET /api/depth/composition`, drawn in its OWN map pane
  (`composition`, z-index 350) so it sits **below** the contour lines.

### `CanvasOverlayMixin` — two load-bearing invariants (do not regress)

The shared canvas-overlay plumbing all three layers mix in (see also Performance &
memory). Two things a future LLM must keep:

- **Pan guard uses LAYER coords.** `_scheduleReset`'s "did the view move?" check
  compares the canvas top-left (`containerPointToLayerPoint([0,0])`) — that point
  shifts with every pan. Do **not** use the map centre in container coords: it's
  always ~size/2 and never changes on pan, so the guard would always "skip", and
  the overlay would freeze offset until a zoom forced a redraw.
- **`_animateZoom` (bound to `zoomanim`)** scales + translates the canvas to track
  the zoom animation. Without it the overlay shows wrong-scale content until the
  `zoomend` redraw.

### Water clip, alignment, windowed fetch

- **Water clip.** `clipToWaterMask` clips **all** depth overlays to the OSM water
  polygon (fetched from `GET /api/depth/water`, held in `waterMask`) so nothing
  paints over land/islands. Apply between `ctx.save()`/`ctx.restore()` in each
  `_draw`. No water available → draws unclipped (graceful).
- **Alignment Adjust.** `VA.depthOffset()` returns a `{lat,lon}` nudge **added to
  every rendered depth coordinate** (grid + contours + composition), for an
  imported chart whose georeferencing sits slightly off the basemap. Set via a
  two-click Adjust tool in the depth Settings card (click a point on the overlay,
  then where it should sit; the delta is added so it converges), persisted in
  `vanchor-depth-offset`.
- **Tier-1 windowed fetch.** Overlays fetch only the padded viewport
  (`map.getBounds().pad(0.3)`), not the whole chart. Pan-refetch is debounced and
  **gated to >25% view moves** (`_viewMovedEnough`) — boat-follow micro-drift is
  covered by the periodic 4 s grid poll, not per-tick refetches (refetching every
  tick crashed a tab).

`VA.map` is extended with `redrawDepth`, `setDepthShow`, `setContourShow`,
`setReliefShow`, `setDepthPalette`/`getDepthPalette`, `setHighlight`/`getHighlight`
(plus `VA.setCompositionShow`). Each overlay is fronted by a proxy
`L.layerGroup()` so the layers-panel checkbox can drive the real start/stop while a
`*Syncing` guard prevents re-entrancy.

## Backup & restore card (`backup.js`)

Settings → **Data & diagnostics** section → "Backup & restore" (`#backup-card`).
**Download** gathers every `vanchor-`-prefixed `localStorage` key into a
`{client}` map and `fetch`-POSTs it to `POST /api/backup` (reads the response as
a **blob** — `VA.postJSON` can't, so we use raw `fetch`), then triggers a browser
download via an object URL (filename from the `Content-Disposition` header, else
`vanchor-backup-<date>.zip`). **Restore** uploads a `.zip` as
`multipart/form-data` (field `file`) to `POST /api/restore`, writes the returned
`client` map back into `localStorage`, surfaces the backup's
`app_version`/`schema_version`/`created_at` + any `warnings` (+ a restart note if
`restart_required`), and **reloads** the page after a short delay so the restored
data takes effect. Restore is confirmed first (it overwrites current data).
ids: `backup-download`, `backup-dl-status`, `backup-file`, `backup-restore`,
`backup-rs-status`. **Needs the matching `/api/backup` + `/api/restore`
backend.**

## Gotchas

- New JS not loading? You forgot the `sw.js` `SHELL` entry or the `index.html`
  script tag — or the user is on a stale worker (tell them to reload twice).
- Duplicate `id`s silently break `getElementById` wiring — keep ids unique;
  prefix module-local ids.
- Verify visually with headless Playwright (see
  [testing-and-workflow.md](testing-and-workflow.md)) — `node --check` only
  catches syntax, not behaviour or layout.

## Performance & memory

The hot path is the **5 Hz telemetry frame** (`VA.onTelemetry`) and the
**boat-follow pan**: while `followBoat` is on, `renderMap` calls
`map.panTo(...)` *every frame*, which fires `moveend` ~5×/s. Anything bound to
`moveend`/`zoomend`/`resize` therefore runs 5×/s even when the user is idle.
Pitfalls fixed (don't regress these):

- **Canvas overlays must NOT repaint per telemetry frame.** The depth grid
  (`GridLayer`), depth contours (`ContourLayer`) and catch heatmap (`HeatLayer`
  in `analytics.js`) used to fully repaint on every follow-pan `moveend` — each
  repaint iterates thousands of cells/segments and is a 50–110 ms *long task*.
  They now share a **`CanvasOverlayMixin`** (in `map.js`; `analytics.js` has its
  own copy) that binds `moveend/zoomend/resize` to **`_scheduleReset`**, which
  (a) **coalesces** bursts into one redraw per `requestAnimationFrame`, and
  (b) **skips the redraw** when the map view hasn't shifted by ≥ `VIEW_EPS_PX`
  (6 px) — so the sub-pixel micro-pans of boat-follow no longer trigger
  repaints. New data forces a repaint via `this._forceDraw = true` before
  `_scheduleReset()`. If you add a canvas overlay, copy this pattern — bind
  `_scheduleReset`, never `_reset`, and set `_forceDraw` in `setData`.
- **Cull before projecting; project once.** In the per-cell loops, hoist the
  `bounds.getSouth()`/… getters out of the loop, cull off-screen cells *before*
  calling `latLngToContainerPoint` (the dominant cost), and project each point
  once. The grid derives a cell's pixel box from metres-per-pixel at the view
  centre and projects only the cell **centre** (1 call/cell, not 2 corners).
  The contour `_draw` scans the lattice **once** (cells outer, levels inner,
  with a min/max-corner early-out) and **memoizes** corner projections, instead
  of rescanning + reprojecting the whole lattice for each of the ~24 isobath
  levels. Segments are bucketed per level and stroked in one batched path.
- **Result:** with Depth map + contours on, a realistic exercise (idle + pan +
  zoom) went from ~40 long tasks / 2.6 s of jank to **0 long tasks**; idle-while-
  depth-on went from 67 long tasks (4.1 s) to ~5 (the 4 s grid re-fetches).
- **Buffers are capped — keep them so.** trail (`trailPts`, 600), NMEA log
  (`MAX_LOG` 40), charts (`CHART_CAP` 600, and only sampled while the charts
  card is `open`), rolling SOG window (time-bounded). When you add a per-frame
  buffer, cap/trim it and prefer gating work on a panel being open.
- **Profiling:** drive headless Chromium with `--enable-precise-memory-info`
  (+`--js-flags=--expose-gc`), watch `JSHeapUsedSize`/CDP `Performance.getMetrics`
  (`JSEventListeners`, `Nodes`) for growth, and a `PerformanceObserver`
  `longtask` for jank. A monotonically rising heap *while idle* = a leak; a flat
  GC-sawtooth is fine.
