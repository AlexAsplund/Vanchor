# Backend (Python) developer guide

> Part of the `docs/llms/` developer guide. 🔁 **If you add/change a control
> mode, command, boat parameter, nav feature, or endpoint, update this file**
> (and `api.md` for endpoints/commands).

Covers everything Python except the physics simulator (that's
[simulation.md](simulation.md)). Read [architecture.md](architecture.md) first.

## The runtime (`app.py`)

`Runtime` constructs the sim-or-hardware devices, the `Navigator`, the
`Controller`, and the periodic `asyncio` loops; `main()` parses CLI/YAML and
starts uvicorn with the FastAPI app from `ui/server.py`. Key methods:

- `handle_command(cmd)` — runtime/sim commands (teleport, environment, battery,
  routes, trips); delegates steering commands to the controller.
- `telemetry()` — returns `state.to_dict()` (with replay support).
- `depth_grid()`, `boat_profile()`, `_apply_boat_specs()`,
  `_build_boat_params()` — boat/depth surfaces called by endpoints.
- `_apply_boat_specs(specs)` is where a `BoatConfig` change becomes live: it
  rebuilds the sim physics **and** re-derives helm tuning (steer_sign,
  thrust-yaw feed-forward, hull-character authority/smoothing).

## Control modes (`controller/modes.py`)

A **mode** is one steering behaviour. The base `ControlMode` has:

- `activate(state)` — called when the mode becomes active (snapshot leg start,
  reset flags).
- `update(state, dt) -> Setpoint` — called every control tick; returns a
  `GuidedSetpoint(target_heading, thrust)` (the helm steers to the heading) or a
  `ManualSetpoint(thrust, steering)` (raw passthrough).

Modes are registered in the `Controller` constructor (a `ControlModeName` →
instance dict) and selected by `handle_command`. Existing modes include:
`ANCHOR_HOLD`, `WAYPOINT` (route following), `HEADING_HOLD`, `MANUAL`, `CRUISE`,
`DRIFT`, `ORBIT`, `TROLLING`, `CONTOUR_FOLLOW`, `FOLLOW_APB`, plus survey/jog.

**Guidance note (waypoint following):** `WaypointMode` uses
`heading = bearing_to_target − clamp(xte_gain · cross_track, ±max)`. It is a
proportional cross-track law. It tracks cleanly on a *steady* GPS fix — if you
see weaving, suspect noisy input (see the GPS-noise lesson in
[simulation.md](simulation.md)) before re-tuning the law. `AnchorMode` filters
position (`_filtered_position`) + uses hysteresis to avoid GPS-noise
overcorrection — a good pattern to copy if a mode hunts.

**Route end-behaviour (loop vs patrol).** Reaching an end of the route is
resolved by `WaypointMode._wrap_or_bounce`, driven by two `state` flags (both in
telemetry + route snapshots, set from a `goto`/`load_route` flag):

- `route_loop` — wrap the active index back to `0` and keep circling (closed
  ring, e.g. "around island").
- `route_patrol` — **bounce**: flip the internal `_step` (±1) and run the route
  back the other way, a continuous there-and-back (`0→1→2→1→0…`). Needs ≥2
  waypoints. Off-the-end correction is `active_waypoint += 2*_step` (lands on the
  adjacent in-range mark). Distinct from `route_loop`; a plain route (neither
  flag) completes and idles.

**Work Area mode (`WorkAreaMode`).** Visit each spot, HOLD position there, then
advance. The spots are `state.waypoints`; `active_waypoint` is the current spot.
A two-phase machine: TRAVEL reuses the waypoint leg (cross-track + fwd/reverse);
on arrival within `arrival_radius_m` it switches to HOLD, delegating to a private
`AnchorHoldMode` (spot-lock). It advances when the `next_spot` button arrives
(`state.work_next_requested`) and/or, in `advance="timed"`, after `dwell_s`
(accumulated from `dt`, harness-friendly); `route_loop`/`route_patrol` cycle the
spots via the same `_wrap_or_bounce`, else it holds the final spot. Each
`Waypoint` may carry an optional `heading`: once on station the boat orients to
it with a gentle `orient_thrust` (best-effort — a single bow thruster can't hold
heading AND position, so the anchor's position recovery wins on drift-out).
Telemetry: `work_holding` / `work_dwell_remaining_s` / `work_spot_count`. The
mode shares the WaypointMode leg config (boat-spec tuning applies to both). The
draw-an-area → grid spots generator is `survey.plan_work_spots` (→
`Runtime.plan_work_spots`, water-clipped; endpoint `POST /api/route/work_area`).

**Forward vs reverse manoeuvring:** `modes.maneuver_to_bearing(...)` decides
whether to drive **forward** (bow at the mark) or **reverse** (stern at the mark)
by lower estimated *time-to-arrive* = `turn_time + travel_time`. Reversing trades
a smaller heading change for slower travel (a prop is weaker astern), so it wins
when the mark is **behind AND near** ("turn a little and reverse" rather than a
180° spin); for a far mark, turning around and running forward is quicker. The
crossover is ≈ `432 / turn_rate_dps` metres, so sluggish (keelboat) hulls reverse
at longer distances. `WaypointMode` applies it (config `allow_reverse`, default
on; with hysteresis to avoid chatter); the helm already flips steering authority
under negative thrust. `AnchorMode` has its own simpler angle-based reverse.

### Recipe: add a control mode

1. Add a `ControlModeName` enum value (`core/state.py`) and a `Mode(ControlMode)`
   in `modes.py` implementing `activate`/`update`.
2. Register it in the `Controller` constructor dict.
3. Add a `handle_command` branch (`elif ctype == "your_mode": ...`) that sets the
   mode + any config.
4. Add a test in `tests/` using the harness (drive it, assert convergence).
5. If the UI triggers it, wire a JS module (`VA.send`) — [frontend.md](frontend.md).
6. Document the command in [api.md](api.md).

## The helm (`controller/controller.py`)

`Helm` converts a mode's heading-intent into a steering command. It owns the
project's hardest-won invariants:

- `steer_sign` (+1 bow / −1 stern from `thruster_x_m()` sign) multiplies **all**
  steering. Set live in `_apply_boat_specs`.
- `thrust_yaw_ff` — a thrust-proportional steering bias that cancels the yaw of a
  laterally-offset motor (geometry-derived, calibration-trimmed).
- `autopilot_steer_scale` (authority) and `steer_tau` (command smoothing) — both
  biased by `hull_tracking` so a boat starts sensibly tuned (see
  [simulation.md](simulation.md); a no-op at `hull_tracking=1.0`).
- Slew/limit enforcement lives in `controller/safety.py`.

## Calibration (`controller/calibration.py`)

Drives the real/sim boat through phases (straight / coast / turn / **reverse**)
and measures max speed, turn rate, steering sign, the thrust-yaw trim, and the
**reverse speed → `reverse_efficiency`** (+ reverse turn rate), then writes them
back to the active `BoatConfig` via `update_boat`. It is the *reliable* way to
fit a real hull; the boat parameters are a prior it refines. The reverse profile
is what makes the forward/reverse decision use real data, not the 0.6 default.

## Navigation (`nav/`)

- `navigator.py` — `handle_sentence(nmea)` parses GPS/compass/depth, spike-guards
  (`guard.py`), and updates the perceived `NavigationState`.
- `routing.py` + `water.py` — smart "take me here" routing over OSM water
  geometry (shapely/networkx); `water.py` caches water polygons in
  `vanchor_data/`. `routes.py` is the in-memory route model; `survey.py` builds
  lawnmower routes; island loops + RTL also live here.
- `depth.py` — `DepthMap`: see the **depth chart** section below.
- `track.py`, `trip.py` — breadcrumb track + trip log/GPX.
- `nmea.py`, `nmea_net.py` — NMEA parse + TCP/UDP NMEA bridge.

## Depth chart (`nav/depth.py`)

`DepthMap` holds the live **soundings** the recorder accumulates *plus* three
parallel **imported chart layers**:

- `points` — `(lat, lon, depth_m)` soundings (recorded live as the boat moves, or
  imported). The only layer the recorder grows.
- `hardness` — `(lat, lon, index)`, bottom-hardness raw `0..127`. Empty for live
  sonar (it has no hardness); imported only. Same `(lat, lon, value)` shape as
  soundings, so it grids/windows identically (just a different `source`).
- `contours` — list of `{"d": depth_m, "pts": [[lat, lon], ...]}` isobath
  polylines. A **vector** overlay served windowed.
- `composition` — list of `{"pct": 0..100, "ring": [[lat, lon], ...]}` polygons.
  A **vector polygon** overlay rendered FILLED — never rasterised/interpolated
  (that destroys the boundaries).

**SPLIT persistence (two files, all writes ATOMIC).** Soundings and the static
chart are stored separately because the recorder calls `save()` often:

- `save(path)` writes **only** `points` → `vanchor_data/depthmap.json` (small;
  the recorder's periodic save stays tiny).
- `save_chart(path)` writes the static chart (`hardness`/`contours`/`composition`)
  → `vanchor_data/depthchart.json`, written **once on import**, not per sounding.
- `load(path, chart_path)` reads both back.

Every write is `_atomic_write` (temp file + `os.replace`) — a kill/power-loss
mid-write can't truncate the file. This fixed a mid-write truncation that
corrupted the (large, slow-to-rewrite) chart.

**`as_grid(cell_m, max_cells, interpolate, radiate, bbox=, source=)`** bins
`(lat, lon, value)` points into ~`cell_m` square cells (local metres-per-degree
frame; single O(n) pass) and averages per cell, then fills the gaps in two
confidence-ordered passes: **interpolate** (enclosed-hole IDW → `kind:"interp"`,
`est:true`) and **radiate** (nearest-neighbour/Voronoi out to a bounded radius →
`kind:"radiated"`, `est:false`); measured cells are `kind:"measured"`. Cell size
auto-grows (doubling) until the count is `≤ max_cells`, and the size used is
returned. `bbox` (west, south, east, north) windows the input first. `source`
picks the layer to grid — defaults to `points`; pass `self.hardness` etc.

> **INVARIANT — do NOT reintroduce a bounding-box scan.** Both fill passes
> (`_interpolate_holes`, `_radiate`) iterate the **measured** cells'
> neighbourhoods → O(measured · radius²), independent of how widely the soundings
> are spread. The old bounding-box scan was O(bbox_area · radius²) and pegged the
> CPU / froze the event loop on sparse-but-wide charts (a few thousand soundings
> over a whole lake).

`contours_in(bbox)` / `composition_in(bbox)` window the two vector layers (kept
if any vertex falls in the bbox, capped by `limit`).

**Parsing imported files.** `parse_depth_features(filename, data) -> {soundings,
hardness, contours, composition}` handles CSV/XYZ (`.xyz` = `lon,lat,z`; CSV =
`lat,lon,depth`, header auto-detected) and GeoJSON, routing GeoJSON features by
geometry: **Point/MultiPoint** → soundings (depth from a `depth`-ish property or
Z); a **`hardness`** property → hardness; **LineString** → contours (depth from a
property); **Polygon** with `composition_pct` → composition. `parse_depth_soundings`
is a back-compat wrapper returning just `soundings`.

### `Runtime` depth methods (`app.py`)

- `import_depth_map(filename, data, replace=False)` — parse → `replace` swaps the
  whole chart else merge all four layers (caps `points`/`hardness` at
  `max_points`) → `save()` soundings + `save_chart()` the static chart. Returns
  per-layer counts.
- `depth_grid(cell_m, bbox, field="depth")` — `cell_m` clamped 2..200;
  `field="hardness"` grids the hardness layer (passes `source=self.hardness`),
  else soundings. Returns `{ok, field, cell_m, min_depth, max_depth, count, cells}`.
  The chart changes slowly, so the UI polls this, not the 5 Hz telemetry.
- `depth_contours(bbox)` / `depth_composition(bbox)` — windowed vector layers →
  `{ok, count, contours}` / `{ok, count, polygons}`.
- `water_polygon(bbox)` — OSM water MultiPolygon coords for the bbox, used to
  **clip** the overlays to water (don't paint composition over land). Uses
  `nav/water.py` `WaterCache` + Overpass (the same offline cache as routing), so
  offline it needs the area pre-downloaded. Endpoints: see [api.md](api.md).

## Config + boat profiles (`core/config.py`, `core/boat_profiles.py`)

- `config.py` — nested dataclasses loaded from YAML (`vanchor.example.yaml`).
  `BoatConfig` holds the physical params; `SensorConfig` holds sim noise;
  `ControlConfig` holds gains/`steer_tau`.
- `boat_profiles.py` — `BoatProfileStore` persists named profiles to
  `vanchor_data/boats.json` and **seeds starter presets on first run** (jon
  boat, bow/stern trolling, off-centre, 15 HP outboard).

### Recipe: add a boat parameter

1. Add the field to `BoatConfig` (+ `DEFAULT_CONFIG_YAML`) with a **no-op
   default**.
2. Use it where it belongs (sim physics in `_build_boat_params`/`fossen.py`,
   and/or helm tuning in `_apply_boat_specs`).
3. Expose it in `boat_profile()` telemetry; `POST /api/boat` already accepts any
   field generically (verify).
4. Test the no-op invariant + the effect at non-default values.
5. UI slider (optional) → [frontend.md](frontend.md). Document in
   [simulation.md](simulation.md) and `docs/nav-control-api.md`.

## Backup / restore (`core/backup.py`)

A versioned, self-describing backup of all persistent state, built + restored
purely in memory.

- `create_backup(data_dir, client=None, app_version=None, *, created_at=...)`
  → a ZIP (bytes) containing the worth-keeping `data_dir` files (`boats.json`,
  `depthmap.json`, `devices.json`, every `trips/*.json`), a `client.json` (the
  UI's `localStorage` dict, or `{}`), and a `manifest.json`. **Regenerable
  caches (`water_cache/`, `debug/`) are excluded.** `created_at` is an ISO8601
  string **passed in by the caller** — the module never calls `datetime.now()`
  (so backups are reproducible/testable).
- `restore_backup(data_dir, zip_bytes)` → validates `manifest.format ==
  "vanchor-backup"` (else `ValueError` → 400), extracts the known files
  (overwriting; creating `trips/`), and returns
  `{ok, schema_version, app_version, created_at, restored, client, warnings}`.
  Defensive against zip-slip (absolute / `..` paths ignored) and bad zips
  (`ValueError`).

**Manifest + `SCHEMA_VERSION`.** `manifest.json` =
`{format, schema_version, app_version, created_at, contents}`. `SCHEMA_VERSION`
(currently `1`) is the on-disk layout version. A backup whose `schema_version`
is *newer* than this build restores best-effort with a warning; an *older* one
is run through `_migrate(manifest, zf)`.

**Migration extension point.** `_migrate` is the single, explicit hook for
"convert old backups". Today it's a no-op pass-through. When you change the file
set / names / shapes, **bump `SCHEMA_VERSION`** and add a step keyed on the
*source* version inside `_migrate` (chain `v1→v2→…`), each returning the upgraded
manifest. The wiring lives in `Runtime.create_backup` / `Runtime.restore_backup`
(`app.py`): restore extracts, then reloads what it can **live** (boat profiles +
depth map from disk), setting `restart_required` for whatever it can't refresh —
notably restored **device config**, which (like editing it) only applies on the
next restart (`reload_devices()` is not auto-invoked). Endpoints: `POST /api/backup` (zip download) and
`POST /api/restore` (multipart upload) — see [api.md](api.md).

## Hardware (`hardware/`)

`interfaces.py` defines the device/motor protocols (`MotorController.apply` +
`flush`, sensor `start`/`stop`); `serial_devices.py` / `serial_link.py` implement
real serial GPS/compass + an Arduino motor with optional steering feedback. They
mirror `sim/devices.py` so nothing above the device layer changes between sim and
hardware.

**Adding a new hardware driver** (e.g. an AHRS compass like the HWT901B) does NOT
touch `app.py`: `registry.py` + the `drivers/` package are a self-registering
plugin system — drop a module that calls `register_driver(kind, source, build)`
and it becomes a selectable `*_source`. The runtime builds/validates/lists from
the registry, and a driver may expose a `device_menu()` (settings + actions the
UI renders). Full how-to: **[device-drivers.md](device-drivers.md)**.

**Simulation is one source *per device*, not a global mode.** `Runtime.__init__`
asks `HardwareConfig.source(device)` for each of `gps`/`compass`/`depth`/`motor`
and builds them independently, so any **mix** works:

- Sensors: `"sim"` | `"serial"` | `"nmea"`. `"nmea"` builds **no internal
  device** — the navigator is fed by external NMEA over the TCP bridge
  (`--nmea-tcp`) or the `inject_nmea` command (a phone/chart-plotter GPS). So
  "GPS from NMEA" is never blocked by the sim/serial choice.
- Motor: `"sim"` | `"serial"` | `"both"`. `"both"` builds a `_TeeMotor` that
  drives the **simulated boat AND** a real serial servo at once — i.e.
  **bench-test a steering servo against a realistic autopilot** (`motor_source:
  both`, everything else sim).

The simulated boat is built whenever *any* device is `"sim"`/`"both"` (the sim
sensors read its truth; the sim motor drives it). `start()`/`stop()` guard a
`None` (external-NMEA) sensor. Defaults: `enabled=false` → all sim (unchanged);
`enabled=true` → all serial. Above the device layer, code never checks which.

**Device config is persistable + API-editable** (the only config that is — the
YAML is load-only). `Runtime.device_config()` / `set_device_config(payload)` back
`GET`/`POST /api/config/devices`; a POST validates (sensor sources `sim|serial|
nmea`, motor `sim|serial|both`, int ports/baudrate), saves `<data_dir>/devices.
json` (`{"hardware":{...}, "nmea_tcp":{...}}`), and updates the in-memory config.
On startup `main()` calls `apply_device_overrides(config)` (in `config.py`, after
`load()`) to merge `devices.json` over the base config **before** `Runtime` builds
devices — so a saved setup survives restarts. Edits are **persist + apply on
restart**: the POST validates and persists to `devices.json` and returns
`restart_required: true`; the new device set takes effect on the next process
start. Edits are **not** hot-swapped live — a live reload was prototyped and
reverted as unreliable (it can trip the fix-loss failsafe mid-operation).
`Runtime.reload_devices()` exists (it uses `_construct_devices()` to build + start
a new device set and only swap in on success) but is **not auto-invoked** today.
Device construction lives in `_construct_devices()` (returns the set without
mutating `self`) and is shared by `__init__` and that (currently unused) reload.

## Analysis & tuning (`analysis/`)

A headless scenario runner (`scenarios.py`, `runner.py`), metrics, report
generation, and an auto-tuner (`tune.py`/`tuning.py`). Run via
`python -m vanchor.analysis`. Use it for systematic before/after control
comparisons. See `docs/analysis.md`.
