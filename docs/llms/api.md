# API contract — REST + WebSocket

> Part of the `docs/llms/` developer guide. 🔁 **If you add/rename/remove an
> endpoint, command type, or telemetry field, update this file** (and, for the
> deeper control/routing semantics, `docs/nav-control-api.md` /
> `docs/routing-weather-api.md`).

All routes live in `src/vanchor/ui/server.py`. They are thin: each calls a
method on the shared `Runtime` (`app.py`) and returns its dict. Commands are
applied by `Runtime.handle_command` (runtime/sim-level) which falls through to
`Controller.handle_command` (steering modes).

## WebSocket: `/ws`

The browser connects once and receives **telemetry** at ~5 Hz: the JSON of
`NavigationState.to_dict()` (`core/state.py`). This is the single source of
truth for the UI. To add a field, add it in `to_dict()` and document it; the
front end reads it from `VA.last` (see [frontend.md](frontend.md)). Commands may
also be sent over the socket, but the canonical path is `POST /api/command`.

Telemetry includes (non-exhaustive — read `state.to_dict()` for the full set):
position, heading_deg, fix (sog/cog), mode, waypoints + active_waypoint,
cross_track_m, distance_to_waypoint_m, bearing_to_dest, depth_m, anchor +
distance_to_anchor_m, battery, sim_enabled, route_loop, route_patrol,
alerts/banners (battery
RTL, shallow, no-go, link-loss, MOB), and the boat profile.

## Commands: `POST /api/command  { "type": "...", ... }`

Steering/mode commands (handled in `controller/controller.py`):

`manual` (thrust+steering) · `stop` · `anchor_hold` / `anchor` · `heading_hold`
(heading[, throttle]) · `goto` (waypoints[], on_arrival) · `cruise` · `drift` ·
`orbit` · `trolling` · `contour_follow` · `follow_apb` · `backtrack` · `jog` ·
`set_throttle` · `set_launch` · `set_min_depth` · `set_nogo_zones` · `mob` /
`mob_clear` · `pause_nav` / `resume_nav` · `record` · `load_route` · `start` /
`clear`.

`goto` and `load_route` both take an optional boolean `loop` (close the ring —
circle the route continuously) and `patrol` (at each end reverse and run the
route back — a there-and-back patrol); these surface as `route_loop` /
`route_patrol` in telemetry.

Runtime/sim commands (handled in `app.py` `Runtime.handle_command`):

`teleport` (lat, lon[, heading] — sim only: move ground truth + zero velocity) ·
`set_environment` / `weather_preset` (sim wind/current) · `set_battery` ·
`inject_nmea` · `set_gps_offset` / `clear_gps_offset` · `return_to_launch` ·
`trip_start` / trip controls.

When you add a command: handle it in the right layer (mode/steering →
controller; sim/runtime → app), and if the UI triggers it, wire it from a JS
module via `VA.send({type:"..."})`.

## REST endpoints (grouped)

| Endpoint | Purpose |
|----------|---------|
| `GET /` , `GET /sw.js` , `GET /manifest.webmanifest` | the PWA shell |
| `GET /api/state` | one-shot telemetry snapshot (same shape as `/ws`) |
| `GET /api/log?n=` | recent log lines (NMEA/console) |
| `POST /api/command` | apply a command (above) |
| `POST /api/route/plan` , `/api/route/plan/cancel` | smart water routing ("take me here") |
| `POST /api/route/island` | loop-around-island ring of waypoints |
| `POST /api/route/rtl` | return-to-launch route |
| `POST /api/route/survey` | lawnmower survey route over a polygon |
| `POST /api/route/prefetch` | pre-cache OSM water + tiles for a bbox (offline) |
| `GET /api/route/charts` , `POST /api/route/charts/clear` | cached chart management |
| `GET /api/depth/grid?cell_m=&west=&south=&east=&north=&field=` | gridded depth / bottom-hardness chart (see below) |
| `POST /api/depth/import` | import an open-format depth file (CSV/XYZ or GeoJSON) |
| `GET /api/depth/contours?west=&south=&east=&north=` | imported isobath polylines (windowed) |
| `GET /api/depth/composition?west=&south=&east=&north=` | imported bottom-composition polygons (windowed) |
| `GET /api/depth/water?west=&south=&east=&north=` | OSM water polygon(s) to clip overlays to water |
| `GET /api/weather/presets` | named sim weather presets |
| `GET/POST /api/boat` | read / live-update the active boat's `BoatConfig` fields |
| `GET /api/boat/profiles` , `POST .../profiles[/{id}][/activate]` , `DELETE` | boat profile CRUD + activate |
| `GET /api/config/devices` | current device/hardware config + selectable `options` |
| `POST /api/config/devices` | persist+validate a device-config edit (applies on next restart; `restart_required: true`) |
| `POST /api/calibrate` , `/api/calibrate/cancel` | run / cancel the auto-calibration drive |
| `POST /api/tune` , `GET /api/tune/jobs` | the offline auto-tuner |
| `POST /api/debug/start` , `/api/debug/stop` | record/replay debug sessions |
| `POST /api/backup` | download a versioned backup ZIP of all persistent state |
| `POST /api/restore` | restore a backup ZIP (multipart upload) |

**Backup / restore (`/api/backup`, `/api/restore`).** A *backup* is one ZIP
bundling the worth-keeping `data_dir` files (`boats.json`, `depthmap.json`,
`devices.json`, every `trips/*.json`) plus a `client.json` (the UI's
`localStorage` slice, keys prefixed `vanchor-`) and a self-describing
`manifest.json`. Regenerable caches (`water_cache/`, `debug/`) are excluded.

```jsonc
// manifest.json (inside the zip)
{
  "format": "vanchor-backup",   // constant magic; restore rejects anything else
  "schema_version": 1,           // bumps on incompatible layout changes
  "app_version": "0.1.0",       // package version that wrote it
  "created_at": "2026-06-26T12:00:00Z",  // ISO8601 (request time)
  "contents": ["boats.json", "depthmap.json", "trips/trip-...json", ...]
}

// POST /api/backup  body { "client": { "vanchor-...": "..." } }   (optional)
//   -> 200 application/zip, Content-Disposition: attachment; filename="vanchor-backup-<date>.zip"

// POST /api/restore  multipart form, field `file` = the .zip
//   -> 200 { "ok": true, "schema_version": 1, "app_version": "0.1.0",
//            "created_at": "...", "restored": ["boats.json", ...],
//            "client": { "vanchor-...": "..." }, "warnings": [...],
//            "restart_required": false }
//   -> 400 { "ok": false, "error": "<msg>" }   (corrupt / non-vanchor zip)
```

The frontend POSTs its `localStorage` (keys prefixed `vanchor-`) as `client`,
saves the returned zip, and on restore reads `client` back to repopulate
`localStorage`. A backup from a NEWER `schema_version` still restores
best-effort with a `warnings` entry; an older one runs through a migration hook
(see [backend.md](backend.md)). `restart_required` is true when something can't
take effect live — notably restored **device config**, which (like editing it
directly) only applies on the next restart.

`POST /api/boat` accepts **any** `BoatConfig` field generically and applies it
live (rebuilds sim physics + helm tuning). That's why adding a boat parameter
needs no endpoint change — only `BoatConfig`, the sim wiring, and (if relevant)
the UI. See [simulation.md](simulation.md) and [backend.md](backend.md).

**Device config (`/api/config/devices`).** Unlike the load-only YAML, the
device/hardware config is editable + persisted to `<data_dir>/devices.json`. It
is **persist + apply on restart** — POST validates and writes `devices.json`, and
the new device set takes effect on the **next restart** (`restart_required:
true`). It is **not** hot-swapped live: a live device reload was prototyped and
reverted as unreliable (it can trip the fix-loss failsafe mid-operation). A
`Runtime.reload_devices()` method exists but is **not auto-invoked** today.
Shapes:

```jsonc
// GET /api/config/devices
{
  "hardware": { "enabled": false, "gps_port": "/dev/ttyUSB0",
    "compass_port": "/dev/ttyUSB1", "motor_port": "/dev/ttyUSB2", "baudrate": 4800,
    "gps_source": null, "compass_source": null, "depth_source": null, "motor_source": null },
  "nmea_tcp": { "enabled": false, "port": 10110 },
  "options": { "sensor": ["sim","serial","nmea"], "motor": ["sim","serial","both"] },
  "restart_required": false   // true after a pending edit that needs a restart
}
// POST /api/config/devices  body { "hardware": {...}, "nmea_tcp": {...} }  (both keys optional, partial OK)
//   -> 200 { "ok": true, "restart_required": true }   (persisted to devices.json; applies on next restart)
//   -> 400 { "ok": false, "error": "<msg>" }          (bad source / non-int port|baudrate)
```

**Depth overlays (`/api/depth/*`).** These feed the depth / bottom-hardness map
overlay. The bbox params (`west,south,east,north`, lon/lat degrees) are Tier-1
**viewport windowing** — only the soundings/features inside the window are
returned/gridded, so a large chart ships just what's on screen.

```jsonc
// GET /api/depth/grid?cell_m=15&west=&south=&east=&north=&field=depth
//   cell_m  : grid cell size in metres, clamped 2..200 (default 15)
//   bbox    : west,south,east,north all optional; given => only that window is gridded
//   field   : "depth" (default) | "hardness" (bottom-hardness, 0..127)
//   Bins soundings into ~cell_m cells, averaging the value per cell.
{ "ok": true, "field": "depth", "cell_m": 15.0,
  "min_depth": 0.4, "max_depth": 31.2, "count": 1234,
  "cells": [ { "lat": .., "lon": .., "depth": .., "n": 3, "est": false, "kind": "measured" }, ... ] }

// POST /api/depth/import  multipart form field `file` (CSV/XYZ or GeoJSON), `?replace=<bool>`
//   replace=true swaps the whole chart; default merges.
//   -> { "ok": true, "imported": <n>, "hardness": <n>, "contours": <n>,
//        "composition": <n>, "total": <n> }
//   -> { "ok": false, "error": "<msg>", "imported": 0 }   (parse failure)

// GET /api/depth/contours?west=&south=&east=&north=   (windowed)
//   imported isobath polylines (a large chart has 80k+ lines)
//   -> { "ok": true, "count": <n>,
//        "contours": [ { "d": <depth_m>, "pts": [[lat,lon], ...] }, ... ] }

// GET /api/depth/composition?west=&south=&east=&north=   (windowed)
//   imported bottom-composition polygons (rendered filled)
//   -> { "ok": true, "count": <n>,
//        "polygons": [ { "pct": 0..100, "ring": [[lat,lon], ...] }, ... ] }

// GET /api/depth/water?west=&south=&east=&north=
//   OSM water polygon(s) to CLIP overlays to water. Cached via the routing
//   WaterCache; fetched from Overpass + stored if absent (so offline the area
//   must be pre-cached). GeoJSON-style MultiPolygon coords, lon/lat:
//     [ [ [ [lon,lat], ... ]=exterior, [hole], ... ], ... ]
//   -> { "ok": true, "water": <MultiPolygon coords> }
```

## Deeper references

- **Control & nav semantics** (modes, calibration, cross-track, feed-forward,
  hull tracking): `docs/nav-control-api.md`.
- **Routing & weather**: `docs/routing-weather-api.md`.
- **The full telemetry/UI field contract**: `docs/ui-contract.md`.
