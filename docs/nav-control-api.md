# Nav-control API

The control & navigation backend contract that the web UI builds against. All
commands go over the WebSocket (`/ws`) or `POST /api/command`; telemetry fields
appear in the `/ws` stream and `GET /api/state`. These extend the contract in
[`ui-contract.md`](ui-contract.md).

## 1. GPS offset calibration (#45)

Corrects a known-wrong GPS receiver against a surveyed truth position. The
offset is a constant (Δlat, Δlon) added to **every subsequent fix** in the
navigator before it becomes `state.fix` / `state.position`, so the corrected
position flows to the controller and telemetry. Nothing is persisted.

Commands:

- `{"type":"set_gps_offset","true_lat":<f>,"true_lon":<f>}` — set the offset so
  the boat's **current** reported position lands exactly on `(true_lat,
  true_lon)`. (If an offset is already active it is adjusted, not replaced, so
  the currently displayed position is what snaps to the truth.) The sensor
  spike-filter's reference is shifted by the same delta so the correction is not
  mistaken for a GPS glitch.
- `{"type":"clear_gps_offset"}` — zero the offset.

Telemetry:

```json
"gps_offset": { "dlat": <deg>, "dlon": <deg>, "active": <bool> }
```

## 2. Throttle % override for guided modes (#49 backend half)

An independent **engine-power** path for guided/autopilot modes (heading_hold,
waypoint, follow_apb, drift, anchor recovery). When set, a guided mode's
non-zero thrust **magnitude** is replaced by the override percent; the mode
keeps control of direction (sign — e.g. an anchor recovery backing up, drift
braking in reverse) and of whether it wants thrust at all (a zero stays zero).
Manual mode and the validated heading/anchor gains are untouched. This is
separate from the speed-in-KNOTS `cruise {knots}` hold, which still owns the
throttle of cruising modes while active; the % override does not fight it.

Commands:

- `{"type":"set_throttle","percent":<0..100>}` — set the override (clamped to
  0..100).
- `{"type":"set_throttle","percent":null}` (or `0`) — clear it; modes revert to
  their built-in default throttle.

Telemetry:

```json
"throttle_override": { "active": <bool>, "percent": <0..100> }
```

## 3. Pause / Resume / Stop navigation (#50 backend half)

Commands:

- `{"type":"pause_nav"}` — remember the active guided mode **and** its
  parameters (waypoints + active index, route on-arrival, target heading, anchor
  + radius + hold-heading, drift target, cruise knots, throttle override), then
  hold position by engaging anchor-hold at the current position. No-op if not
  navigating (already manual).
- `{"type":"resume_nav"}` — restore the previously suspended mode + all its
  parameters and continue (a route resumes from where it left off). No-op if
  nothing is suspended.
- `{"type":"stop"}` (pre-existing) — now **also** clears any suspended state,
  in addition to switching to manual/idle.

Telemetry:

```json
"nav": { "paused": <bool>, "suspended_mode": <str|null> }
```

`suspended_mode` is the mode value that was active when paused (e.g.
`"waypoint"`), or `null` when nothing is suspended.

## 4. Abort route planning (#54 backend half)

`Runtime.plan_route` is cancellable. The planner
(`src/vanchor/nav/routing.plan_route`) accepts an optional `cancelled`
predicate that it polls periodically during the O(n²) visibility-graph build /
search; if it returns true the plan aborts and returns:

```json
{ "ok": false, "waypoints": [], "message": "Route planning cancelled." }
```

`Runtime.plan_route` passes a predicate reading an internal flag. A normal plan
**resets the flag at its start**, so a stale cancel never blocks the next plan.

Endpoint:

- `POST /api/route/plan/cancel` → `{ "cancelled": true }` — sets the flag so an
  in-progress `POST /api/route/plan` aborts ASAP.

`Runtime.cancel_route_plan()` is the programmatic equivalent.

## 5. Area survey "map mode" route (#47 backend half)

Given a closed area polygon, compute a **boustrophedon / lawnmower coverage
route** (back-and-forth parallel passes) to survey the area, with a settable
spacing between passes. The planner lives in
[`src/vanchor/nav/survey.py`](../src/vanchor/nav/survey.py):

```python
plan_survey(polygon_latlon, spacing_m, angle_deg=None) -> SurveyResult
```

- `polygon_latlon` is a list of `[lat, lon]` vertices (closed ring; the first
  point need not be repeated).
- Projects to a metric UTM frame (reuses `water.Projection`), generates parallel
  sweep lines `spacing_m` apart, clips each to the polygon, and orders them
  boustrophedon (alternate direction each pass, connecting the nearer ends so the
  turn between passes is short).
- The default sweep direction is the polygon's **longest axis** (fewest, longest
  passes); pass `angle_deg` to override.
- Passes are inset half a spacing from the edges and centred (standard lawnmower
  convention): a 50 m band at 10 m spacing → 5 passes at 5,15,25,35,45 m.
- The result is capped at `survey.MAX_WAYPOINTS` (300). A too-small spacing on a
  big area returns `ok: false` with a clear message rather than a huge route.

Endpoint (does **not** start nav — the UI loads the waypoints into its route
editor; the shapely work runs in an executor):

- `POST /api/route/survey` body
  `{ "polygon": [[lat,lon],...], "spacing_m": <f>, "angle_deg": <f|null> }`
  → `{ "ok": <bool>, "waypoints": [{ "name", "lat", "lon" }], "message": <str> }`

Waypoints are named `WP1`, `WP2`, … with the last named `DEST`.
`Runtime.plan_survey(polygon, spacing_m, angle_deg=None)` is the programmatic
equivalent.

## 5b. "Around island" loop route (#77)

Click a patch of land that the lake fully surrounds (an **island**) and get a
closed loop route that circles it. The planner lives in
[`src/vanchor/nav/routing.py`](../src/vanchor/nav/routing.py):

```python
plan_island_loop(click_lat, click_lon, water_ll, *, boat_lat, boat_lon, offset_m=20.0)
    -> RouteResult        # RouteResult now also carries `loop: bool`
```

- The boat's **basin** is the water body it occupies (`_water_body_for`). An
  **island** is one of that basin's interior rings (a hole = land ringed by
  routable water). The planner picks the island whose filled polygon **contains
  the click**. A click in open water, on the mainland shore, or outside the
  basin returns `ok: false` with a clear message (it is *not* an island).
- It buffers the island outward by `offset_m`, takes that offset ring, and
  **clips it to the navigable water** (the basin's filled outline minus *every*
  island) so the whole loop stays on the water. If the full ring can't stay in
  water all the way around (island too close to shore/another island), the
  offset is shrunk (and the message says so); if even a small offset won't fit,
  it returns `ok: false`.
- The result is a **closed**, ordered waypoint list (`WP1…WPn` then a final
  `LOOP` point back at `WP1`), `loop: true`, capped at `MAX_LOOP_WAYPOINTS` (60).

Endpoint (offline water chart/cache shared with `/api/route/plan`; same on-land /
no-chart handling; runs in an executor; does **not** start nav):

- `POST /api/route/island` body `{ "lat": <f>, "lon": <f>, "offset_m": <f?> }`
  → `{ "ok": <bool>, "waypoints": [{ "name", "lat", "lon" }], "loop": <bool>, "message": <str> }`

`Runtime.plan_island_loop(click_lat, click_lon, offset_m=20.0)` is the
programmatic equivalent.

### Loop following

`NavigationState.route_loop` (default `false`) makes a `WaypointMode` route
**circle continuously**: when the boat reaches the last waypoint with
`route_loop` set, `active_waypoint` wraps back to `0` (the leg is reset) instead
of setting `route_complete`. Non-loop routes are unchanged.

Set it via the **`"loop": true`** flag on the route-start commands:

```json
{"type":"goto","waypoints":[...],"loop":true}        // start a looping route
{"type":"load_route","loop":true}                     // ditto for state-placed waypoints
```

(`route_loop` is also preserved across pause/resume.)

## 6. Sonar cone → depth-map footprint (#47, cone part)

The depth-map dot size should reflect the sonar **cone footprint**, not a fixed
size. A new `boat.sonar_cone_deg` field (default `20.0`) holds the transducer
beam angle. NMEA `DPT`/`DBT` sentences carry only a depth, never a beam angle, so
a configurable default is the correct source. The footprint **diameter** at depth
`d` is:

```
footprint_diameter_m = 2 * d * tan(sonar_cone_deg / 2)
```

so the UI sizes each depth dot from its `depth_m` plus this config value.

- Config: `boat.sonar_cone_deg` in `AppConfig.boat` (`config.py`,
  `DEFAULT_CONFIG_YAML`, `vanchor.example.yaml`).
- Telemetry / profile: appears in `boat_profile()`, i.e. the `boat` block of
  `GET /api/state`, the `/ws` stream, and `GET /api/boat`.
- Update: `POST /api/boat` accepts `{ "sonar_cone_deg": <f> }` (generic
  field-update path) and returns the updated profile.

## 7. Offline chart prefetch + management (#52 backend half)

So a boat can route offline, the user can pre-download the water/routing chart
for an area at the dock. Charts are stored in the same `WaterCache`
(`<data_dir>/water_cache/`) used by `POST /api/route/plan`, so a prefetched area
makes subsequent route planning offline-capable.

- `POST /api/route/prefetch` body `{ "bbox": [south, west, north, east] }`
  → `{ "ok": <bool>, "cached": <bool>, "vertices": <int>, "message": <str> }`.
  Fetches the water polygon via Overpass (`water.fetch_overpass` + `assemble_water`)
  and caches it. Runs in an executor; network failure is handled gracefully with a
  clear `ok: false` message. If the area is already covered by a cached chart it
  returns `cached: true` without re-fetching.
- `GET /api/route/charts` → `{ "charts": [{ "bbox": [s,w,n,e], "vertices": <int>,
  "size_bytes": <int> }] }` — lists cached charts for the UI to show/manage.
- `POST /api/route/charts/clear` →
  `{ "ok": true, "removed": <int>, "message": <str> }` — deletes all cached charts.

Programmatic equivalents: `Runtime.prefetch_chart(bbox)`,
`Runtime.list_charts()`, `Runtime.clear_charts()`.

## 8. Guided pattern modes — Contour-follow, Orbit, Trolling (#57/#58/#59)

Three new guided control modes, each a `ControlModeName` + `ControlMode`
subclass + config dataclass, dispatched from `Controller.handle_command` and
wired into the existing helm / safety / cruise / throttle-override pipeline.

All three accept an optional `speed_knots`: when given it is held via the
existing **Cruise Control** (speed-over-ground) loop — the mode is registered in
`_CRUISING_MODES`, so cruise owns the throttle and the boat holds that SOG. When
`speed_knots` is `null` the mode falls back to its own sensible default thrust
(`ContourConfig.throttle` 0.5, `OrbitConfig.throttle` 0.5, `TrollingConfig.throttle`
0.4). The throttle-% override (#49) still scales a non-zero default thrust when
no knots cruise is active.

### Contour-follow (#57)

`ControlModeName.CONTOUR_FOLLOW` / `ContourFollowMode(ContourConfig)`. Holds a
depth contour (isobath): drives forward while steering to keep `state.depth_m`
at `target_depth_m`. It weaves a heading offset off the along-contour **base
heading** captured when engaged (proportional to the depth error, capped at
`max_offset_deg` = 30° so turns stay gentle) — too deep aims toward the shallow
side, too shallow toward the deep side. `side` (`"deep"`/`"shallow"`) picks which
side of the boat the operator wants the deep water on. The depth **trend** along
the track (depth now vs. a few metres back) nudges the base heading to follow a
curving isobath. If the depth is unknown (`depth_m <= 0`, no sounding) it simply
holds heading.

Command:

```json
{"type":"contour_follow","target_depth_m":<f>,"side":"deep"|"shallow","speed_knots":<f|null>}
```

Telemetry:

```json
"contour": { "target_depth_m": <f>, "depth_m": <f>, "error_m": <f> }
```

(`error_m` = `depth_m - target_depth_m`; positive = too deep.)

### Circle / orbit (#58)

`ControlModeName.ORBIT` / `OrbitMode(OrbitConfig)`. Orbits `center_lat/lon` at
`radius_m`. Each tick it heads along the **tangent** to the ring at the boat's
bearing-from-centre (on the `direction` side), biased by a radial-error
correction (`radial_gain_deg_per_m`, capped at `max_radial_correction_deg` = 60°)
so the boat converges to the ring from inside or outside and then holds it.
`direction` is `"cw"` or `"ccw"`.

Command:

```json
{"type":"orbit","center_lat":<f>,"center_lon":<f>,"radius_m":<f>,"direction":"cw"|"ccw","speed_knots":<f|null>}
```

Telemetry:

```json
"orbit": { "center_lat": <f|null>, "center_lon": <f|null>, "radius_m": <f>, "direction": "cw"|"ccw", "range_m": <f> }
```

(`range_m` = current distance from the boat to the centre. The mode also mirrors
this into `state.distance_to_anchor_m` for the existing HUD range field.)

### Trolling pattern (#59)

`ControlModeName.TROLLING` / `TrollingMode(TrollingConfig)`. Weaves a sinusoidal
heading offset `amplitude_deg * sin(2π t / period_s)` around `base_heading`
while driving forward — the lazy-S troll. `base_heading` defaults to the boat's
heading at the moment of engaging when `null`.

Command:

```json
{"type":"trolling","base_heading":<f|null>,"amplitude_deg":<f>,"period_s":<f>,"speed_knots":<f|null>}
```

Telemetry:

```json
"trolling": { "base_heading": <f>, "amplitude_deg": <f>, "period_s": <f>, "phase": <f> }
```

(`phase` is the current sine phase in radians, `0..2π`.)

## Safety & power features (#60–#64)

Five backend features for power monitoring and safety/failsafe behaviour. New
config lives under `safety:` and a new `battery:` section (see
`core/config.py` / `DEFAULT_CONFIG_YAML`).

### 1. Battery monitor (#60)

A simulated battery (`sim/battery.py`, `Battery`/`BatteryConfig`) wired into the
simulator step. It draws current = `idle_a` + `load_a·|thrust|`, integrates the
state-of-charge down over time, and estimates remaining range/time-to-empty
from a recent-average draw and the boat's speed-over-ground. On real hardware
the live SOC/voltage/current come from a battery monitor over the HAL; the
telemetry shape and estimates are identical, so the UI/RTL logic don't care
which source feeds them.

Config (`battery:`): `capacity_ah` (100), `nominal_v` (12), `reserve_pct` (15 —
range/time are reported down to this usable reserve).

Command (set/reset for testing/demo; sim-only):

```json
{"type":"set_battery","soc_pct":<0..100>}
```

Telemetry (top-level `battery`):

```json
"battery": { "soc_pct": <f>, "voltage_v": <f>, "current_a": <f>, "draw_w": <f>, "range_m": <f>, "time_to_empty_s": <f|null> }
```

(`time_to_empty_s` is `null` when there is no meaningful draw to estimate
against; `range_m` is 0 when the boat isn't making way.)

### 2. Return-to-Launch (#61)

The launch/home point is auto-recorded on the **first good fix** (or set
explicitly). `return_to_launch` plans a *fastest* water route home with
`nav.routing.plan_route` and follows it as a `WaypointMode` route with
**anchor-on-arrival**.

Auto-recommend: each telemetry tick, when the battery `range_m` drops to within
`safety.rtl_margin_m` of the straight-line distance home (so the boat can *just*
make it back), `rtl_recommended` is set true (the UI prompts — it is **not**
forced). If `safety.auto_rtl` is true, RTL is engaged automatically.

Commands:

```json
{"type":"set_launch"}            // record launch at the current position
{"type":"return_to_launch"}      // plan + follow a route home (anchor on arrival)
```

`return_to_launch` is also exposed as `POST /api/route/rtl` (runs the heavy
plan in an executor — prefer this over the WS command, which would block the
loop). Returns `{ok, waypoints, message}`.

Telemetry:

```json
"launch": { "lat": <f|null>, "lon": <f|null>, "set": <bool> },
"rtl_recommended": <bool>
```

Config (`safety:`): `rtl_margin_m` (100), `auto_rtl` (false).

### 3. Shallow-water / geofence no-go auto-stop (#62)

Guard inside the `SafetyGovernor` (runs in `control_tick`): if the sounded
`depth_m` is **valid and below** `safety.min_depth_m`, OR the boat is inside (or
within `safety.nogo_lookahead_m` of) a no-go polygon, it **cuts thrust** and
raises an alarm. An unknown/no-return depth (`depth_m <= 0`) never trips the
shallow stop, and `min_depth_m = 0` disables the depth check entirely. Polygon
tests use shapely.

Commands:

```json
{"type":"set_min_depth","min_depth_m":<f>}
{"type":"set_nogo_zones","zones":[[[lat,lon],[lat,lon],...],...]}   // list of rings
```

Telemetry (extends `safety`):

```json
"safety": { ..., "shallow_stop": <bool>, "nogo_stop": <bool>, "min_depth_m": <f> }
```

Config (`safety:`): `min_depth_m` (0 = off), `nogo_lookahead_m` (5).

### 4. Man-overboard (#63)

`mob` records a MOB mark at the current position and immediately navigates back
to it (a single-waypoint `WaypointMode` route, **stop on arrival** so the boat
holds near the casualty). `mob_clear` cancels.

Commands:

```json
{"type":"mob"}
{"type":"mob_clear"}
```

Telemetry:

```json
"mob": { "active": <bool>, "lat": <f|null>, "lon": <f|null> }
```

### 5. Lost-connection failsafe (#64)

`ui/server.py`'s `/ws` marks a "last client seen" timestamp on connect/activity
and on disconnect. If **no** client has been connected for
`safety.link_loss_timeout_s` (default 20 s) **while underway** (a guided/cruising
mode making way — not idle manual, not anchor-hold), the Runtime auto-engages
**anchor-hold** (hold-position) at the current spot. It clears on reconnect.

The clock is injectable (`Runtime(now_fn=...)`) and the check
(`evaluate_link_failsafe(now=...)`) takes an explicit time, so it is driven by
the deterministic tick in tests rather than the wall clock.

Telemetry (top-level `link`):

```json
"link": { "client_connected": <bool>, "since_s": <f|null>, "failsafe_engaged": <bool> }
```

Config (`safety:`): `link_loss_timeout_s` (20).

### 6. Server-side depth-map gridding

The depth recorder (`nav/depth.py`) accumulates raw soundings as a breadcrumb of
`(lat, lon, depth)` points. Painting tens of thousands of individual dots in the
UI is wasteful, so the server can bin them into an averaged colour grid.

`DepthMap.as_grid(cell_m=15.0, max_cells=3000)` bins every sounding into a square
grid of ~`cell_m` metres in a local metric frame (a flat metres-per-degree
conversion at the data's mean latitude — soundings span a small area, so no
projection is needed), averaging depth per cell in a single **O(n)** pass. The
returned cell count is capped at `max_cells` by **doubling the effective cell
size** until the bins fit, and the cell size actually used is reported back.

`Runtime.depth_grid(cell_m)` wraps it, clamping `cell_m` to **2..200** m (default
15) and adding `ok: true`.

Endpoint:

```
GET /api/depth/grid?cell_m=15
```

Response:

```json
{
  "ok": true,
  "cell_m": <f>,            // cell size actually used (may exceed the request if capped)
  "min_depth": <f>,         // min cell-average depth (0.0 when empty)
  "max_depth": <f>,         // max cell-average depth (0.0 when empty)
  "count": <int>,           // total soundings binned
  "cells": [
    { "lat": <cell-center-lat>, "lon": <cell-center-lon>, "depth": <avg>, "n": <count> }
  ]
}
```

The depth map changes slowly, so the UI polls this occasionally rather than
reading it from the 5 Hz telemetry. The existing `depth_points` telemetry field
stays in place for now (the UI will switch to the grid).

### 7. Trip log (#66)

A **trip** is one continuous outing. While a trip is active the runtime samples
the boat's position into a breadcrumb track (min-distance filtered, like the
track recorder), integrates the distance travelled as the sum of segment
lengths, and tracks the maximum speed-over-ground. `TripLog` (`nav/trip.py`) is
updated on every telemetry tick from `state.position` and `state.sog_knots`,
with time supplied by the Runtime's injectable `_now_fn` (so auto start/stop is
deterministic in tests). Finished trips are persisted to
`<data_dir>/trips/<id>.json`, where `<id>` is `trip-YYYYMMDD-HHMMSS` derived
from the start timestamp.

**Auto-start / auto-stop** (config flags under `control:`):

- `auto_trip` (default `true`) — enable the automatic state machine.
- `trip_start_speed_kn` (default `0.5`) — SOG at/above which the boat is
  "making way". The first tick making way **auto-starts** a trip.
- `trip_idle_timeout_s` (default `120`) — once an *auto-started* trip has gone
  idle (SOG below the threshold) continuously for this long, it **auto-stops**
  and is persisted. The idle clock resets on every tick the boat makes way.
- `trip_min_distance_m` (default `5`) — breadcrumb spacing for the trip track.

Manual `trip_start` / `trip_stop` always work and override the automatic
behaviour; starting a new trip finalizes (persists) any trip already in
progress.

**Commands** (over `/api/command` or the WebSocket):

```json
{ "type": "trip_start", "name": "Evening run" }   // name may be null/omitted
{ "type": "trip_stop" }                            // finalize + persist
```

**Telemetry** — the *current* trip's live stats appear under `trip`:

```json
"trip": {
  "active": <bool>,        // false when no trip is recording
  "name": <str|null>,
  "distance_m": <f>,
  "duration_s": <f>,
  "avg_speed_kn": <f>,     // distance / duration
  "max_speed_kn": <f>
}
```

**Saved trip model** (`<data_dir>/trips/<id>.json`):

```json
{
  "id": "trip-20260625-101500",
  "name": <str>,
  "started_at": <epoch-s>,
  "ended_at": <epoch-s>,
  "distance_m": <f>,
  "duration_s": <f>,
  "avg_speed_kn": <f>,
  "max_speed_kn": <f>,
  "point_count": <int>,
  "points": [[lat, lon], ...]
}
```

**REST endpoints:**

```
GET    /api/trips             -> { "trips": [ <summary>, ... ] }   // newest first, no points
GET    /api/trips/{id}        -> <full trip incl. points>  (404 if absent)
GET    /api/trips/{id}.gpx    -> GPX <trk> of the points, application/gpx+xml  (404 if absent)
DELETE /api/trips/{id}        -> { "ok": <bool> }
```

The list summaries carry every field above **except** `points` (they add
`point_count` instead). The GPX export is a GPX 1.1 document with a single
`<trk>`/`<trkseg>` containing one `<trkpt lat= lon=>` per recorded point.

### 8. Named boat profiles (#75)

Several named bundles of the editable boat specs (the same fields the Init-boat
wizard edits — `length_m`, `beam_m`, `mass_kg`, `max_speed_mps`, `max_thrust_n`,
`reverse_efficiency`, `thruster_mount`, `max_steer_angle_deg`,
`autopilot_steer_deg`, `shaft_dia_mm`, `steer_range_deg`, `steer_reduction`,
`sonar_cone_deg`, …) can be saved, switched between and persisted so a helmsman
can keep, e.g., a light-kayak profile and a heavier-skiff profile and swap them.

- **Store.** Profiles live in `<data_dir>/boats.json`:

  ```json
  {
    "active_id": "bow-trolling-motor",
    "profiles": {
      "bow-trolling-motor": { "name": "Bow trolling motor", "specs": { /* … */ } },
      "light-kayak": { "name": "Light Kayak", "specs": { /* … */ } }
    }
  }
  ```

  On first run (no `boats.json`) a small set of ready-to-pick **starter presets**
  is seeded (#89), with the **bow trolling motor** active by default. Existing
  saved profiles are never clobbered. The presets differ realistically so the
  physics + steering behave differently when activated:

  | id | name | mount | `thruster_y_m` | `max_thrust_n` | `max_speed_mps` | `mass_kg` | `length_m` |
  |----|------|-------|----------------|----------------|-----------------|-----------|------------|
  | `bow-trolling-motor`     | Bow trolling motor      | bow   | 0.0  | 250 | 1.6 | 300 | 4.1 |
  | `stern-trolling-motor`   | Stern trolling motor    | stern | 0.0  | 250 | 1.6 | 300 | 4.1 |
  | `off-centre-bow-trolling`| Off-centre bow trolling | bow   | 0.35 | 250 | 1.6 | 300 | 4.1 |
  | `15-hp-stern-outboard`   | 15 HP stern outboard    | stern | 0.0  | 700 | 7.0 | 450 | 4.5 |

  Switching to a **stern** mount flips the helm `steer_sign` and the physics yaw
  (negative `thruster_x_m()`); the **off-centre** preset gives the thrust-yaw
  feed-forward (§9) a lateral offset to cancel; the **15 HP outboard** is a much
  faster, more powerful, heavier boat. Every preset is still an ordinary named
  profile the picker can list/activate/edit/delete.

  Profile **ids** are slugs derived from the name (`"Light Kayak"` →
  `light-kayak`), disambiguated with an incrementing counter on collision —
  never from the wall clock, so they are reproducible. Every stored profile is
  complete: any spec field a caller omits falls back to the `BoatConfig` default.
  (When the store is handed an explicit `BoatConfig` seed instead, it falls back
  to seeding a single `default` profile from it.)

- **Live apply.** Activating a profile (or editing the active one) writes its
  specs onto `config.boat` **and rebuilds the live physics**: the simulator
  boat's `params` are replaced with freshly-built ones via `_build_boat_params`
  and the Fossen mass/damping matrices re-derived (the model precomputes
  mass-dependent yaw inertia and the derived surge drag at build time, so an
  in-place tweak alone would be ignored). Steering authority/slew limits
  (`state.max_steer_angle_deg`, the helm `autopilot_steer_scale`, the safety
  `max_steer_slew_per_s`) and the anchor mode's `boat_max_speed_mps` are updated
  too, so changing e.g. `max_speed_mps` or `mass_kg` actually changes behaviour.
  The active selection is persisted and re-applied on restart.

- **Telemetry / `boat_profile()`.** The boat block in `GET /api/state`, the `/ws`
  stream and `GET /api/boat` gains an `active_boat_id` field naming the active
  profile.

- **Back-compat.** `POST /api/boat` still applies a partial spec edit live, and
  now **also writes the change back into the active profile** so the editable
  boat and the active profile stay in sync.

**REST endpoints:**

```
GET    /api/boat/profiles              -> { "active_id": <id>,
                                            "profiles": [ { "id", "name", ...specs }, ... ] }
POST   /api/boat/profiles              body { "name": <str>, "specs"?: {...} }
                                       -> { "id", "name", "specs": {...} }   // specs default to active boat
POST   /api/boat/profiles/{id}         body { "name"?: <str>, "specs"?: {...} }
                                       -> { "id", "name", "specs": {...} }   // 404 if unknown; applies live if active
POST   /api/boat/profiles/{id}/activate -> <boat_profile incl. active_boat_id>  // 404 if unknown; applies live
DELETE /api/boat/profiles/{id}         -> { "ok": <bool> }   // false (and no-op) if it's the last profile
```

The list endpoint flattens each profile to `{id, name, ...specs}` (specs
inline); `GET`/`POST` of a single profile nest the specs under `"specs"`.
Deleting the active profile falls the active selection back to the first
remaining one (and applies it).

### 9. Off-centre thruster: lateral offset + thrust-yaw feed-forward

A trolling motor mounted off the boat's centreline — a transom-corner stern
motor, or a slightly off-centre bow mount — yaws the boat under straight forward
thrust. With the thruster at offset `(x, y)` from the CG (`x` longitudinal,
`+ = forward`; `y` lateral, `+ = starboard`) making forward thrust `F_fwd` and a
steering-induced lateral force `F_lat`, the yaw moment is

```
N = x · F_lat − y · F_fwd
```

so even with the steering dead centre the `−y · F_fwd` term spins the boat
(a starboard-offset motor yaws it to port).

**New `boat:` config fields (all editable live via `POST /api/boat`):**

| field | default | meaning |
|---|---|---|
| `thruster_offset_m` | `null` | longitudinal CG→thruster (`+fwd`); overrides `thruster_mount` (unchanged, still longitudinal) |
| `thruster_y_m` | `0.0` | **lateral** CG→thruster offset, `+ = starboard` |
| `thrust_yaw_ff` | `null` | feed-forward steer angle (rad) overriding the geometric default |
| `thrust_yaw_ff_trim` | `0.0` | calibration-measured refinement (rad) added on top of the FF angle |

**Sim physics.** `FossenParams` gains `thruster_y_m` and the yaw moment becomes
`N = thruster_x_m · fx_sway − thruster_y_m · fx_surge`, so an off-centre motor
visibly veers the simulated boat at straight thrust.

**Feed-forward compensation.** A constant steering deflection `δ_ff` pre-cancels
the bias so the boat tracks straight under thrust without the heading loop having
to fight it (a PD helm could only counter it at a steady-state heading error).
Deflecting the motor by `δ` gives `F_fwd = F·cos δ`, `F_lat = F·sin δ`, so
`N = F·(x·sin δ − y·cos δ)` is zero when `x·sin δ = y·cos δ`, i.e.

```
δ_ff = atan2(thruster_y_m, |thruster_x_m|)            (+ thrust_yaw_ff_trim)
```

— **independent of thrust magnitude** (both terms scale with `F`) and the **same
sign in forward and reverse** (both scale with thrust's sign). The helm applies
`δ_ff` (normalised by the full mechanical swing `max_steer_angle_deg`) only while
making way (`|thrust| ≥ STEER_EPS`), **inside** `steer_sign` so a stern mount —
whose deflection yaws the boat the opposite way — automatically gets the opposite
*physical* deflection (hence the `|thruster_x_m|` in the formula). It is added to
both manual (so a hands-off, centred helm goes straight) and guided steering, and
updates live whenever the offset/profile/trim changes.

**Calibration.** The straight-line phase drives full ahead with the steering
centred and measures the residual yaw drift (heading-change rate) twice — once
with the feed-forward OFF and once ON. The drift difference over the active FF
angle gives a direct deg/s-per-radian gain, from which it solves for the trim
angle that nulls the remaining drift and writes it to `thrust_yaw_ff_trim`. The
calibration results include `yaw_drift_dps` (measured residual) and the resulting
`thrust_yaw_ff_trim`.

**Telemetry.** `boat_profile()` (the `boat` block of `GET /api/state`, the `/ws`
stream and `GET /api/boat`) now reports `thruster_offset_m`, `thruster_y_m`,
`thrust_yaw_ff` and `thrust_yaw_ff_trim`.

```
POST /api/boat   body { "thruster_offset_m": -1.6, "thruster_y_m": 0.4 }
                 -> <boat_profile>   // applies live: rebuilds sim physics +
                                     //   refreshes the helm feed-forward
```

### 10. Sim teleport (#90)

Instantly relocate the simulated boat's **ground truth** to a new position (and
optionally set its heading), zeroing its velocity so it stops dead instead of
coasting from its old momentum. Useful for jumping the boat to a test spot
without driving there.

Command (sim-only):

```json
{"type":"teleport","lat":<f>,"lon":<f>,"heading":<f>?}
```

- In **SIM** mode `Runtime.handle_command` calls `Simulator.teleport(lat, lon,
  heading=None)`, which snaps `simulator.truth().point` to the target, sets the
  heading if one is given (otherwise it is kept), and resets the boat's motion:
  the Fossen body-frame velocities `[surge, sway, yaw_rate]` (or the simple
  model's `speed`) and the ground-velocity components are all zeroed. The GPS
  spike-guard reference is re-primed so the next fix snaps to the new spot
  instead of being rejected as a position jump.
- On **real hardware** (no simulator) it is a safe **no-op** — the command is
  logged and ignored.


### 11. Hull character / tracking knob (directional stability)

A single `boat:` knob spans the boat's *directional stability* — how willingly it
holds a heading — from a flat-bottom **jon boat** (skittish, easily yawed, snappy
loose turns, lots of leeway/sideslip) through a skiff and deep-V up to a
**keelboat** (tracks straight, resists turning, little leeway).

**New `boat:` config field (editable live via `POST /api/boat`):**

| field | default | meaning |
|---|---|---|
| `hull_tracking` | `1.0` | directional stability: `~0.35` jon boat (loose) · `1.0` current skiff (default) · `~2.5` deep-V / keel (tracks). Clamped to `0.25..3.0` on use. |

**Sim physics.** `FossenParams` gains `hull_tracking`. In `__post_init__` it
derives a multiplier from the knob and a hull-slenderness factor (longer/narrower
hulls track better):

```
k = hull_tracking · clamp( (length / beam) / (4.1 / 1.7),  0.7,  1.6 )
```

The slenderness ratio is normalised to the default hull (`L=4.1`, `B=1.7`) and
clamped to `[0.7, 1.6]`; `hull_tracking` itself is clamped to `[0.25, 3.0]`. `k`
then scales the **directional** damping coefficients:

```
n_r  *= k        # yaw linear damping  (sustained turn rate + directional stability)
n_rr *= k        # yaw quadratic damping
y_v  *= k        # sway linear damping (lateral resistance vs leeway)
y_vv *= k        # sway quadratic damping
```

The surge drag and the sway↔yaw coupling terms (`y_r`, `n_v`, `y_rdot`,
`n_vdot`) are left untouched. **At `hull_tracking = 1.0` and the default `L/B`,
`k == 1.0`, so the boat is byte-identical to before this knob existed** — all
prior tuning and tests are preserved. The realised multiplier is exposed as
`FossenParams.hull_k`.

Higher `k` ⇒ slower turns for the same steering, less leeway in a turn, and
better heading hold under a beam disturbance; lower `k` ⇒ the opposite. Measured
sustained yaw rate at full thrust + full steering on the default 4.1 m hull:

| `hull_tracking` | k | sustained yaw rate |
|---|---|---|
| 0.35 (jon boat) | 0.35 | ~41 °/s |
| 1.0 (skiff, default) | 1.00 | ~17 °/s |
| 1.6 (planing/deep-V) | 1.60 | ~11 °/s |
| 2.5 (keel) | 2.50 | ~7 °/s |

**Presets (#89).** The seeded starter profiles span the range: the trolling-motor
boats sit at the default `1.0`, a literal **"Jon boat (flat-bottom)"** preset
(short/light/beamy) at `0.35`, and the **"15 HP stern outboard"** (planing-ish
deep-V) at `1.6`.

**Telemetry.** `boat_profile()` (the `boat` block of `GET /api/state`, the `/ws`
stream and `GET /api/boat`) reports `hull_tracking`.

```
POST /api/boat   body { "hull_tracking": 0.35 }
                 -> <boat_profile>   // applies live: rebuilds the sim physics
```

**Control tuning (also on real hardware).** Beyond the sim physics, `hull_tracking`
biases the **autopilot tuning** in `_apply_boat_specs`, so a boat starts sensibly
tuned even on real hardware (where the setting can't change the physics). With
`ht = clamp(hull_tracking, 0.25, 3.0)`:

- **Steering authority** scales up with tracking — a stiff/keeled hull resists
  turning, so the helm uses more deflection: `autopilot_steer_scale =
  min(autopilot_steer_deg · ht, max_steer_angle_deg) / max_steer_angle_deg`.
- **Command smoothing** scales the other way — a loose/skittish hull is prone to
  hunting, so it gets more low-pass smoothing: `steer_tau = control.steer_tau ·
  clamp(ht^(-0.5), 0.6, 1.8)`.

At `hull_tracking = 1.0` both are exact no-ops. This is a **prior** — the
auto-calibration drive then measures the real boat and refines the gains from
here.
