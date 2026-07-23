# Nav-control API

The control & navigation backend contract the web UI builds against. Commands go
over the WebSocket (`/ws`) or `POST /api/command`; telemetry fields appear in the
`/ws` stream and `GET /api/state`. These extend [`ui-contract.md`](ui-contract.md).

---

## Calibration

### GPS offset

Corrects a known-wrong receiver against a surveyed truth position. The offset is
a constant (Δlat, Δlon) added to **every subsequent fix** before it becomes
`state.fix` / `state.position`, so the correction flows to controller and
telemetry. Nothing is persisted.

| Command | Effect |
|---|---|
| `{"type":"set_gps_offset","true_lat":<f>,"true_lon":<f>}` | Set offset so the boat's **current** reported position lands on `(true_lat, true_lon)`. An active offset is adjusted, not replaced, so the displayed position is what snaps to truth. The spike-filter reference shifts by the same delta so the correction isn't mistaken for a glitch. |
| `{"type":"clear_gps_offset"}` | Zero the offset. |

Telemetry: `"gps_offset": { "dlat": <deg>, "dlon": <deg>, "active": <bool> }`

---

## Guided-mode control

### Throttle % override

An independent **engine-power** path for guided/autopilot modes (heading_hold,
waypoint, follow_apb, drift, anchor recovery). When set, a mode's non-zero thrust
**magnitude** is replaced by the override percent; the mode keeps control of
direction (sign) and of whether it wants thrust at all (a zero stays zero).
Manual mode and validated heading/anchor gains are untouched. Separate from the
speed-in-knots `cruise {knots}` hold, which still owns cruising throttle while
active; the % override does not fight it.

| Command | Effect |
|---|---|
| `{"type":"set_throttle","percent":<0..100>}` | Set override (clamped 0..100). |
| `{"type":"set_throttle","percent":null}` (or `0`) | Clear; modes revert to built-in default throttle. |

Telemetry: `"throttle_override": { "active": <bool>, "percent": <0..100> }`

### Pause / resume / stop

| Command | Effect |
|---|---|
| `{"type":"pause_nav"}` | Remember the active guided mode **and** its params (waypoints + active index, route on-arrival, target heading, anchor + radius + hold-heading, drift target, cruise knots, throttle override, `route_loop`), then hold position via anchor-hold at the current spot. No-op if already manual. |
| `{"type":"resume_nav"}` | Restore the suspended mode + params and continue (a route resumes where it left off). No-op if nothing suspended. |
| `{"type":"stop"}` | Switch to manual/idle **and** clear any suspended state. |

Telemetry: `"nav": { "paused": <bool>, "suspended_mode": <str|null> }`
(`suspended_mode` is the mode active when paused, e.g. `"waypoint"`, else `null`.)

---

## Route planning & survey

Planning endpoints run heavy work in an executor and do **not** start nav — the
UI loads the returned waypoints into its route editor. Waypoints are named
`WP1`, `WP2`, … with the last named `DEST` (or `LOOP` for island loops).

### Abort planning

`Runtime.plan_route` is cancellable. The planner
(`src/vanchor/nav/routing.plan_route`) polls an optional `cancelled` predicate
during the O(n²) visibility-graph build/search; if true it aborts and returns
`{ "ok": false, "waypoints": [], "message": "Route planning cancelled." }`.
A normal plan **resets the flag at its start**, so a stale cancel never blocks
the next plan.

- `POST /api/route/plan/cancel` → `{ "cancelled": true }` — sets the flag so an
  in-progress `POST /api/route/plan` aborts ASAP. Programmatic:
  `Runtime.cancel_route_plan()`.

### Area survey (lawnmower)

Given a closed area polygon, computes a **boustrophedon coverage route**
(back-and-forth parallel passes). Planner: `plan_survey(polygon_latlon,
spacing_m, angle_deg=None) -> SurveyResult` in
[`src/vanchor/nav/survey.py`](../src/vanchor/nav/survey.py).

- `polygon_latlon`: list of `[lat, lon]` vertices (closed ring; first point need
  not be repeated). Projects to metric UTM (`water.Projection`).
- Sweep lines `spacing_m` apart, clipped to the polygon, ordered boustrophedon
  (alternate direction, connecting nearer ends for short turns).
- Default sweep direction is the polygon's **longest axis**; `angle_deg`
  overrides.
- Passes inset half a spacing and centred: a 50 m band at 10 m spacing → 5
  passes at 5,15,25,35,45 m.
- Capped at `survey.MAX_WAYPOINTS` (300); a too-small spacing on a big area
  returns `ok: false` with a message rather than a huge route.

`POST /api/route/survey` body
`{ "polygon": [[lat,lon],...], "spacing_m": <f>, "angle_deg": <f|null> }`
→ `{ "ok": <bool>, "waypoints": [{ "name", "lat", "lon" }], "message": <str> }`.
Programmatic: `Runtime.plan_survey(polygon, spacing_m, angle_deg=None)`.

### Around-island loop

Click a patch of land the lake fully surrounds and get a closed loop route
circling it. Planner in
[`src/vanchor/nav/routing.py`](../src/vanchor/nav/routing.py):
`plan_island_loop(click_lat, click_lon, water_ll, *, boat_lat, boat_lon,
offset_m=20.0) -> RouteResult` (`RouteResult` carries `loop: bool`).

- The boat's **basin** is its water body (`_water_body_for`); an **island** is
  an interior ring (land ringed by routable water). Picks the island whose
  filled polygon **contains the click**. A click in open water, on mainland
  shore, or outside the basin returns `ok: false`.
- Buffers the island outward by `offset_m` and **clips the ring to navigable
  water** (basin outline minus every island) so the loop stays on water. If the
  full ring can't fit, the offset shrinks (message says so); if even a small
  offset won't fit, `ok: false`.
- Returns a **closed**, ordered list (`WP1…WPn` then a final `LOOP` back at
  `WP1`), `loop: true`, capped at `MAX_LOOP_WAYPOINTS` (60).

`POST /api/route/island` body `{ "lat": <f>, "lon": <f>, "offset_m": <f?> }`
→ `{ "ok": <bool>, "waypoints": [{ "name", "lat", "lon" }], "loop": <bool>, "message": <str> }`.
Shares the offline water chart/cache with `/api/route/plan`. Programmatic:
`Runtime.plan_island_loop(click_lat, click_lon, offset_m=20.0)`.

**Loop following.** `NavigationState.route_loop` (default `false`) makes a
`WaypointMode` route **circle continuously**: reaching the last waypoint with
`route_loop` set wraps `active_waypoint` back to `0` instead of setting
`route_complete`. Set via a `"loop": true` flag on route-start commands:

```json
{"type":"goto","waypoints":[...],"loop":true}
{"type":"load_route","loop":true}
```

### Offline chart prefetch

Pre-download the water/routing chart for an area at the dock so a boat can route
offline. Charts share the `WaterCache` (`<data_dir>/water_cache/`) used by
`POST /api/route/plan`.

| Endpoint | Result |
|---|---|
| `POST /api/route/prefetch` body `{ "bbox": [s,w,n,e] }` | `{ "ok", "cached", "vertices": <int>, "message" }`. Fetches the water polygon via Overpass (`water.fetch_overpass` + `assemble_water`) and caches it. Network failure → graceful `ok: false`. Already-covered area → `cached: true` without re-fetching. |
| `GET /api/route/charts` | `{ "charts": [{ "bbox": [s,w,n,e], "vertices": <int>, "size_bytes": <int> }] }` |
| `POST /api/route/charts/clear` | `{ "ok": true, "removed": <int>, "message": <str> }` |

Programmatic: `Runtime.prefetch_chart(bbox)`, `list_charts()`, `clear_charts()`.

---

## Pattern modes

Three guided modes (`ControlModeName` + `ControlMode` subclass + config
dataclass), dispatched from `Controller.handle_command` and wired into the
helm / safety / cruise / throttle-override pipeline. Each accepts optional
`speed_knots`: when given it is held via **Cruise Control** (SOG loop — the mode
is in `_CRUISING_MODES`); when `null` the mode uses its own default thrust
(contour 0.5, orbit 0.5, trolling 0.4). The throttle-% override still scales a
non-zero default thrust when no knots cruise is active.

### Contour-follow

`ControlModeName.CONTOUR_FOLLOW` / `ContourFollowMode(ContourConfig)`. Holds a
depth isobath: drives forward while steering to keep `state.depth_m` at
`target_depth_m`. Weaves a heading offset off the along-contour **base heading**
captured at engage (proportional to depth error, capped at `max_offset_deg`
30°) — too deep aims to the shallow side, too shallow to the deep side. `side`
(`"deep"`/`"shallow"`) picks which side the deep water is on. The depth **trend**
along track nudges the base heading to follow a curving isobath. Unknown depth
(`depth_m <= 0`) → just holds heading.

Command: `{"type":"contour_follow","target_depth_m":<f>,"side":"deep"|"shallow","speed_knots":<f|null>}`

Telemetry: `"contour": { "target_depth_m": <f>, "depth_m": <f>, "error_m": <f> }`
(`error_m` = `depth_m - target_depth_m`; positive = too deep.)

### Orbit

`ControlModeName.ORBIT` / `OrbitMode(OrbitConfig)`. Orbits `center_lat/lon` at
`radius_m`. Each tick heads along the **tangent** at the boat's bearing-from-
centre (on the `direction` side), biased by a radial-error correction
(`radial_gain_deg_per_m`, capped at `max_radial_correction_deg` 60°) so it
converges to the ring and holds. `direction` is `"cw"`/`"ccw"`.

Command: `{"type":"orbit","center_lat":<f>,"center_lon":<f>,"radius_m":<f>,"direction":"cw"|"ccw","speed_knots":<f|null>}`

Telemetry: `"orbit": { "center_lat": <f|null>, "center_lon": <f|null>, "radius_m": <f>, "direction": "cw"|"ccw", "range_m": <f> }`
(`range_m` = distance to centre; also mirrored into `state.distance_to_anchor_m`
for the HUD range field.)

### Trolling

`ControlModeName.TROLLING` / `TrollingMode(TrollingConfig)`. Weaves a sinusoidal
heading offset `amplitude_deg * sin(2π t / period_s)` around `base_heading` while
driving forward — the lazy-S troll. `base_heading` defaults to the boat's heading
at engage when `null`.

Command: `{"type":"trolling","base_heading":<f|null>,"amplitude_deg":<f>,"period_s":<f>,"speed_knots":<f|null>}`

Telemetry: `"trolling": { "base_heading": <f>, "amplitude_deg": <f>, "period_s": <f>, "phase": <f> }`
(`phase` = current sine phase in radians, `0..2π`.)

---

## Safety & power

Config lives under `safety:` and `battery:` (see `core/config.py` /
`DEFAULT_CONFIG_YAML`).

### Battery monitor

Simulated battery (`sim/battery.py`, `Battery`/`BatteryConfig`) wired into the
sim step: draws current = `idle_a` + `load_a·|thrust|`, integrates SOC down, and
estimates remaining range / time-to-empty from a recent-average draw and SOG. On
real hardware the live SOC/voltage/current come from a battery monitor over the
HAL — identical telemetry shape and estimates, so UI/RTL logic don't care about
the source.

Config (`battery:`): `capacity_ah` (100), `nominal_v` (12), `reserve_pct` (15 —
range/time reported down to this usable reserve).

Command (sim-only, test/demo): `{"type":"set_battery","soc_pct":<0..100>}`

Telemetry: `"battery": { "soc_pct": <f>, "voltage_v": <f>, "current_a": <f>, "draw_w": <f>, "range_m": <f>, "time_to_empty_s": <f|null> }`
(`time_to_empty_s` is `null` with no meaningful draw; `range_m` is 0 when not
making way.)

### Return-to-Launch

Launch/home point auto-recorded on the **first good fix** (or set explicitly).
`return_to_launch` plans a fastest water route home (`nav.routing.plan_route`)
and follows it as a `WaypointMode` route with **anchor-on-arrival**.

Auto-recommend: each tick, when battery `range_m` drops to within
`safety.rtl_margin_m` of the straight-line distance home, `rtl_recommended` goes
true (the UI prompts — not forced). If `safety.auto_rtl`, RTL engages
automatically.

| Command | Effect |
|---|---|
| `{"type":"set_launch"}` | Record launch at current position. |
| `{"type":"return_to_launch"}` | Plan + follow route home (anchor on arrival). |

Also `POST /api/route/rtl` (runs the plan in an executor — prefer over the WS
command, which would block the loop) → `{ok, waypoints, message}`.

Telemetry:
```json
"launch": { "lat": <f|null>, "lon": <f|null>, "set": <bool> },
"rtl_recommended": <bool>
```
Config (`safety:`): `rtl_margin_m` (100), `auto_rtl` (false).

### Shallow-water / no-go auto-stop

Guard in the `SafetyGovernor` (runs in `control_tick`): if sounded `depth_m` is
**valid and below** `safety.min_depth_m`, OR the boat is inside (or within
`safety.nogo_lookahead_m` of) a no-go polygon, it **cuts thrust** and alarms.
Unknown depth (`depth_m <= 0`) never trips it, and `min_depth_m = 0` disables the
depth check. Polygon tests use shapely.

| Command | Effect |
|---|---|
| `{"type":"set_min_depth","min_depth_m":<f>}` | Set shallow threshold. |
| `{"type":"set_nogo_zones","zones":[[[lat,lon],...],...]}` | Set no-go rings (list of rings). |

Telemetry (extends `safety`): `"safety": { ..., "shallow_stop": <bool>, "nogo_stop": <bool>, "min_depth_m": <f> }`
Config (`safety:`): `min_depth_m` (0 = off), `nogo_lookahead_m` (5).

### Man-overboard

`mob` marks the current position and immediately navigates back to it (single-
waypoint `WaypointMode` route, **stop on arrival** so the boat holds near the
casualty). `mob_clear` cancels.

Commands: `{"type":"mob"}`, `{"type":"mob_clear"}`

Telemetry: `"mob": { "active": <bool>, "lat": <f|null>, "lon": <f|null> }`

### Lost-connection failsafe

`ui/server.py`'s `/ws` marks a "last client seen" time on connect/activity/
disconnect. If **no** client has connected for `safety.link_loss_timeout_s`
(default 20 s) **while underway** (a guided/cruising mode making way — not idle
manual, not anchor-hold), the Runtime auto-engages **anchor-hold** at the current
spot. Clears on reconnect. The clock is injectable (`Runtime(now_fn=...)`) and
the check (`evaluate_link_failsafe(now=...)`) takes an explicit time, so tests
drive it deterministically.

Telemetry: `"link": { "client_connected": <bool>, "since_s": <f|null>, "failsafe_engaged": <bool> }`
Config (`safety:`): `link_loss_timeout_s` (20).

---

## Depth map

### Server-side gridding

The depth recorder (`nav/depth.py`) accumulates raw soundings as a `(lat, lon,
depth)` breadcrumb. `DepthMap.as_grid(cell_m=15.0, max_cells=3000)` bins every
sounding into a ~`cell_m` square grid in a local metric frame (flat
metres-per-degree at the data's mean latitude), averaging depth per cell in one
**O(n)** pass. Cell count is capped at `max_cells` by **doubling the effective
cell size** until the bins fit; the size actually used is reported back.
`Runtime.depth_grid(cell_m)` wraps it, clamping `cell_m` to **2..200** m (default
15) and adding `ok: true`.

`GET /api/depth/grid?cell_m=15` →
```json
{
  "ok": true,
  "cell_m": <f>,            // size actually used (may exceed request if capped)
  "min_depth": <f>,         // min cell-average depth (0.0 when empty)
  "max_depth": <f>,         // max cell-average depth (0.0 when empty)
  "count": <int>,           // total soundings binned
  "cells": [ { "lat": <cell-center>, "lon": <cell-center>, "depth": <avg>, "n": <count> } ]
}
```

The map changes slowly, so the UI polls this occasionally rather than reading the
5 Hz telemetry. The existing `depth_points` telemetry field stays for now.

### Sonar cone footprint

The depth-dot size should reflect the sonar **cone footprint**. `boat.sonar_cone_deg`
(default `20.0`) holds the transducer beam angle (NMEA `DPT`/`DBT` carry only a
depth, so a configurable default is correct). Footprint **diameter** at depth `d`:

```
footprint_diameter_m = 2 * d * tan(sonar_cone_deg / 2)
```

- Config: `boat.sonar_cone_deg` (`config.py`, `DEFAULT_CONFIG_YAML`,
  `vanchor.example.yaml`).
- Telemetry: in `boat_profile()` — the `boat` block of `GET /api/state`, `/ws`,
  and `GET /api/boat`.
- Update: `POST /api/boat` accepts `{ "sonar_cone_deg": <f> }` → updated profile.

---

## Trip log

A **trip** is one continuous outing. While active the runtime samples position
into a min-distance-filtered breadcrumb, integrates distance as the sum of
segment lengths, and tracks max SOG. `TripLog` (`nav/trip.py`) is updated every
telemetry tick from `state.position` and `state.sog_knots`, with time from the
Runtime's injectable `_now_fn` (deterministic auto start/stop in tests). Finished
trips persist to `<data_dir>/trips/<id>.json`, `<id>` = `trip-YYYYMMDD-HHMMSS`
from the start timestamp.

**Auto start/stop** (config under `control:`):

| flag | default | meaning |
|---|---|---|
| `auto_trip` | `true` | enable the automatic state machine |
| `trip_start_speed_kn` | `0.5` | SOG at/above which the boat is "making way"; first such tick auto-starts a trip |
| `trip_idle_timeout_s` | `120` | after an auto-started trip goes idle continuously this long, auto-stop + persist (idle clock resets on every making-way tick) |
| `trip_min_distance_m` | `5` | breadcrumb spacing |

Manual `trip_start` / `trip_stop` always work and override the auto behaviour;
starting a new trip finalizes (persists) any trip in progress.

Commands:
```json
{ "type": "trip_start", "name": "Evening run" }   // name may be null/omitted
{ "type": "trip_stop" }                            // finalize + persist
```

Telemetry (current trip's live stats):
```json
"trip": {
  "active": <bool>, "name": <str|null>,
  "distance_m": <f>, "duration_s": <f>,
  "avg_speed_kn": <f>,     // distance / duration
  "max_speed_kn": <f>
}
```

Saved trip model (`<data_dir>/trips/<id>.json`):
```json
{
  "id": "trip-20260625-101500", "name": <str>,
  "started_at": <epoch-s>, "ended_at": <epoch-s>,
  "distance_m": <f>, "duration_s": <f>,
  "avg_speed_kn": <f>, "max_speed_kn": <f>,
  "point_count": <int>, "points": [[lat, lon], ...]
}
```

REST endpoints:
```
GET    /api/trips             -> { "trips": [ <summary>, ... ] }   // newest first, no points
GET    /api/trips/{id}        -> <full trip incl. points>          (404 if absent)
GET    /api/trips/{id}.gpx    -> GPX <trk>, application/gpx+xml     (404 if absent)
DELETE /api/trips/{id}        -> { "ok": <bool> }
```
Summaries carry every field **except** `points` (they add `point_count`). The GPX
export is GPX 1.1 with one `<trk>`/`<trkseg>`, one `<trkpt lat= lon=>` per point.

---

## Boat profiles

Named bundles of the editable boat specs (the same fields the Init-boat wizard
edits — `length_m`, `beam_m`, `mass_kg`, `max_speed_mps`, `max_thrust_n`,
`reverse_efficiency`, `thruster_mount`, `max_steer_angle_deg`,
`autopilot_steer_deg`, `shaft_dia_mm`, `steer_range_deg`, `steer_reduction`,
`sonar_cone_deg`, …) can be saved, switched and persisted.

**Store.** Profiles live in `<data_dir>/boats.json`:
```json
{
  "active_id": "bow-trolling-motor",
  "profiles": {
    "bow-trolling-motor": { "name": "Bow trolling motor", "specs": { /* … */ } },
    "light-kayak": { "name": "Light Kayak", "specs": { /* … */ } }
  }
}
```

On first run (no `boats.json`) a set of **starter presets** is seeded (#89), with
**bow trolling motor** active. Existing saved profiles are never clobbered.
Presets differ realistically so physics + steering behave differently:

| id | name | mount | `thruster_y_m` | `max_thrust_n` | `max_speed_mps` | `mass_kg` | `length_m` |
|----|------|-------|----------------|----------------|-----------------|-----------|------------|
| `bow-trolling-motor`     | Bow trolling motor      | bow   | 0.0  | 250 | 1.6 | 300 | 4.1 |
| `stern-trolling-motor`   | Stern trolling motor    | stern | 0.0  | 250 | 1.6 | 300 | 4.1 |
| `off-centre-bow-trolling`| Off-centre bow trolling | bow   | 0.35 | 250 | 1.6 | 300 | 4.1 |
| `15-hp-stern-outboard`   | 15 HP stern outboard    | stern | 0.0  | 700 | 7.0 | 450 | 4.5 |

A **stern** mount flips the helm `steer_sign` and physics yaw (negative
`thruster_x_m()`); the **off-centre** preset gives the thrust-yaw feed-forward
(below) a lateral offset to cancel; the **15 HP outboard** is faster, more
powerful, heavier. Every preset is an ordinary named profile.

Profile **ids** are slugs from the name (`"Light Kayak"` → `light-kayak`),
disambiguated with an incrementing counter on collision — never from the wall
clock, so reproducible. Every stored profile is complete: any omitted spec field
falls back to the `BoatConfig` default. (Handed an explicit `BoatConfig` seed
instead, the store seeds a single `default` profile from it.)

**Live apply.** Activating a profile (or editing the active one) writes its specs
onto `config.boat` **and rebuilds the live physics**: the simulator boat's
`params` are rebuilt via `_build_boat_params` and the Fossen mass/damping
matrices re-derived (the model precomputes mass-dependent yaw inertia and derived
surge drag at build time, so an in-place tweak alone is ignored). Steering
authority/slew limits (`state.max_steer_angle_deg`, helm `autopilot_steer_scale`,
safety `max_steer_slew_per_s`) and the anchor mode's `boat_max_speed_mps` update
too. The active selection is persisted and re-applied on restart.

**Telemetry.** `boat_profile()` (boat block of `GET /api/state`, `/ws`,
`GET /api/boat`) gains `active_boat_id` naming the active profile.

**Back-compat.** `POST /api/boat` still applies a partial spec edit live, and
**also writes it back into the active profile** so the editable boat and active
profile stay in sync.

REST endpoints:
```
GET    /api/boat/profiles              -> { "active_id": <id>,
                                            "profiles": [ { "id", "name", ...specs }, ... ] }
POST   /api/boat/profiles              body { "name": <str>, "specs"?: {...} }
                                       -> { "id", "name", "specs": {...} }   // specs default to active boat
POST   /api/boat/profiles/{id}         body { "name"?: <str>, "specs"?: {...} }
                                       -> { "id", "name", "specs": {...} }   // 404 if unknown; applies live if active
POST   /api/boat/profiles/{id}/activate -> <boat_profile incl. active_boat_id>  // 404 if unknown; applies live
DELETE /api/boat/profiles/{id}         -> { "ok": <bool> }   // false + no-op if it's the last profile
```
The list endpoint flattens each profile to `{id, name, ...specs}`;
`GET`/`POST` of a single profile nest specs under `"specs"`. Deleting the active
profile falls back to the first remaining one (and applies it).

---

## Boat physics tuning

Config fields under `boat:`, all editable live via `POST /api/boat` (which
rebuilds sim physics and refreshes the helm) and reported in `boat_profile()`.

### Off-centre thruster

A thruster off the centreline yaws the boat under straight forward thrust. With
offset `(x, y)` from CG (`x` longitudinal `+fwd`; `y` lateral `+starboard`),
forward thrust `F_fwd`, steering lateral force `F_lat`, the yaw moment is
`N = x·F_lat − y·F_fwd` — so even dead-centre steering, the `−y·F_fwd` term spins
the boat (a starboard-offset motor yaws to port).

| field | default | meaning |
|---|---|---|
| `thruster_offset_m` | `null` | longitudinal CG→thruster (`+fwd`); overrides `thruster_mount` |
| `thruster_y_m` | `0.0` | **lateral** CG→thruster offset, `+ = starboard` |
| `thrust_yaw_ff` | `null` | feed-forward steer angle (rad) overriding the geometric default |
| `thrust_yaw_ff_trim` | `0.0` | calibration-measured refinement (rad) added on top |

**Sim physics.** `FossenParams` gains `thruster_y_m`; yaw moment becomes
`N = thruster_x_m · fx_sway − thruster_y_m · fx_surge`.

**Feed-forward.** A constant deflection `δ_ff` pre-cancels the bias so the boat
tracks straight without the heading loop fighting it. Deflecting by `δ` gives
`F_fwd = F·cos δ`, `F_lat = F·sin δ`, so `N = F·(x·sin δ − y·cos δ)` is zero when

```
δ_ff = atan2(thruster_y_m, |thruster_x_m|)   (+ thrust_yaw_ff_trim)
```

— **independent of thrust magnitude** and the **same sign in forward and
reverse**. The helm applies `δ_ff` (normalised by `max_steer_angle_deg`) only
while making way (`|thrust| ≥ STEER_EPS`), **inside** `steer_sign` so a stern
mount automatically gets the opposite physical deflection (hence `|thruster_x_m|`).
Added to both manual and guided steering; updates live on offset/profile/trim
change.

**Calibration.** The straight-line phase drives full ahead, steering centred, and
measures residual yaw drift twice — feed-forward OFF then ON. The drift
difference over the active FF angle gives a deg/s-per-radian gain, from which it
solves the trim angle nulling the remaining drift and writes
`thrust_yaw_ff_trim`. Results include `yaw_drift_dps` and `thrust_yaw_ff_trim`.

Example: `POST /api/boat` body `{ "thruster_offset_m": -1.6, "thruster_y_m": 0.4 }`
→ `<boat_profile>`.

### Hull character / tracking

A single knob spans **directional stability** — how willingly the boat holds a
heading — from a flat-bottom **jon boat** (skittish, loose, lots of leeway)
through skiff and deep-V up to a **keelboat** (tracks straight, resists turning).

| field | default | meaning |
|---|---|---|
| `hull_tracking` | `1.0` | `~0.35` jon boat (loose) · `1.0` skiff (default) · `~2.5` deep-V/keel (tracks). Clamped `0.25..3.0`. |

**Sim physics.** `FossenParams` gains `hull_tracking`. In `__post_init__` it
derives a multiplier from the knob and a hull-slenderness factor:
```
k = hull_tracking · clamp( (length / beam) / (4.1 / 1.7),  0.7,  1.6 )
```
The slenderness ratio is normalised to the default hull (`L=4.1`, `B=1.7`) and
clamped `[0.7, 1.6]`; `hull_tracking` clamped `[0.25, 3.0]`. `k` scales the
**directional** damping (`n_r`, `n_rr` yaw; `y_v`, `y_vv` sway); surge drag and
sway↔yaw coupling (`y_r`, `n_v`, `y_rdot`, `n_vdot`) are untouched. **At
`hull_tracking = 1.0` and default `L/B`, `k == 1.0`, so the boat is
byte-identical to before this knob existed.** The realised multiplier is exposed
as `FossenParams.hull_k`.

Measured sustained yaw rate at full thrust + full steering on the default 4.1 m
hull:

| `hull_tracking` | k | sustained yaw rate |
|---|---|---|
| 0.35 (jon boat) | 0.35 | ~41 °/s |
| 1.0 (skiff, default) | 1.00 | ~17 °/s |
| 1.6 (planing/deep-V) | 1.60 | ~11 °/s |
| 2.5 (keel) | 2.50 | ~7 °/s |

**Presets (#89).** Trolling-motor boats sit at `1.0`, a **"Jon boat
(flat-bottom)"** preset at `0.35`, the **"15 HP stern outboard"** at `1.6`.

**Control tuning (also on real hardware).** Beyond the sim, `hull_tracking`
biases autopilot tuning in `_apply_boat_specs` so a boat starts sensibly tuned
even where physics can't change. With `ht = clamp(hull_tracking, 0.25, 3.0)`:

- **Steering authority** scales up with tracking: `autopilot_steer_scale =
  min(autopilot_steer_deg · ht, max_steer_angle_deg) / max_steer_angle_deg`.
- **Command smoothing** scales the other way (loose hulls hunt): `steer_tau =
  control.steer_tau · clamp(ht^(-0.5), 0.6, 1.8)`.

At `hull_tracking = 1.0` both are exact no-ops — a **prior** the auto-calibration
drive then refines.

---

## Sim teleport

Instantly relocate the simulated boat's **ground truth** (optionally set heading),
zeroing velocity so it stops dead instead of coasting.

Command (sim-only): `{"type":"teleport","lat":<f>,"lon":<f>,"heading":<f>?}`

- In **SIM** mode `Runtime.handle_command` calls `Simulator.teleport(lat, lon,
  heading=None)`: snaps `simulator.truth().point`, sets heading if given (else
  kept), and zeroes motion — the Fossen body-frame velocities `[surge, sway,
  yaw_rate]` (or the simple model's `speed`) and the ground-velocity components.
  The GPS spike-guard reference is re-primed so the next fix snaps to the new spot.
- On **real hardware** (no simulator) it is a safe **no-op** — logged and ignored.
