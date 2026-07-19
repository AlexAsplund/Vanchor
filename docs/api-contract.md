# API contract

Vanchor's HTTP/WebSocket API is a **versioned, self-describing contract** — the
lesson borrowed from SignalK: *the data model is the contract*. Rather than an
informal, drifting telemetry dict, the payload shape is declared in one place
([`src/vanchor/core/contract.py`](../src/vanchor/core/contract.py)) and served
live so any client can introspect it.

## Discover it at runtime

```
GET /api/contract
```

returns:

```json
{
  "schema_version": "1.0",
  "envelope_version": 3,
  "units": "angles in degrees, distances in metres, speed in knots ...",
  "telemetry": { "heading_deg": {"type": "number", "unit": "deg", "desc": "..."}, ... },
  "commands":  { "anchor_hold": {"desc": "PID position hold at a point"}, ... }
}
```

- **`schema_version`** — the payload contract version (semver-ish). Breaking a
  field's meaning/type/unit bumps it; additive fields are a minor bump.
- **`envelope_version`** — the WebSocket envelope protocol version (`{v, type,
  seq, ts}` from #21), separate from the payload schema.
- **`telemetry`** — every top-level field of `GET /api/state` / the WS telemetry
  frame, with a coarse `type`, an optional `unit`, and a one-line `desc`.
- **`commands`** — every command `type` the server accepts (`POST /api/command`
  / the WS command channel).

## It can't silently drift

[`tests/test_contract.py`](../tests/test_contract.py) fails if the server ever
emits a telemetry key, or the controller accepts a command, that isn't declared
here. So the contract stays honest without manual upkeep — add a field, declare
it, or CI is red.

## Units

Angles are **degrees** (true, 0–360), distances **metres**, speed-over-ground
**knots**, drift **m/s**, depth **metres**. (SignalK-style SI/radians is *not*
used on the wire — the mapping to SI lives in any future SignalK bridge, not the
core API.)

## Endpoints (overview)

All endpoints are in `src/vanchor/ui/server.py`.

**Core telemetry + command**
- `GET /api/state` — full telemetry snapshot (same shape as the WS telemetry frame).
- `GET /api/contract` — this contract (telemetry types + accepted command types).
- `POST /api/command` / `WS /ws` — send a command `{type, …}`; `stop` is dual-path and always accepted.
- `GET /api/log?n=50&full=0` — recent frames from the in-memory telemetry ring.
- `GET /api/audit?n=50` — command-audit ring: who commanded what, accepted/denied/error.
- `GET /api/alerts`, `POST /api/alerts/clear` — server-persisted alert history.
- `GET /api/logs` — in-memory application log records (for the "View logs" panel).

**Device / hardware config**
- `GET /api/config/devices`, `POST /api/config/devices` — read/write device config.
- `GET /api/devices/serial-ports` — auto-detected serial ports on the host.
- `GET /api/hw/scan` — hardware wizard: candidate endpoints (serial + I2C) without opening anything.
- `POST /api/hw/probe` — briefly open one port/bus to identify what's on it (passive; 409 if another probe is in progress or the driver already owns the port).

**Routing + depth**
- `POST /api/route/plan`, `POST /api/route/plan/cancel` — water-only A→B routing.
- `POST /api/route/island` — closed loop around a clicked island.
- `POST /api/route/rtl` — plan + follow a Return-to-Launch route (prefer over the WS `return_to_launch` command — runs the heavy plan in an executor).
- `POST /api/route/survey` — boustrophedon area-survey route.
- `POST /api/route/work_area` — serpentine work-area spot grid.
- `POST /api/route/contour` — route following the nearest imported depth contour.
- `POST /api/route/prefetch`, `GET /api/route/charts`, `POST /api/route/charts/clear` — offline chart management.
- `GET /api/depth/grid`, `/at`, `/contours`, `/composition`, `/water`, `POST /api/depth/import` — depth chart overlays.

**Boat / calibration**
- `GET /api/boat`, `POST /api/boat` — active boat profile.
- `GET|POST /api/boat/profiles`, `POST /api/boat/profiles/{id}`, `/activate`, `DELETE` — named profiles.
- `POST /api/calibrate`, `POST /api/calibrate/cancel` — auto-calibration drive.
- `GET|POST /api/fusion/calibration`, `/calibrate/start|stop|save|reset`, `POST /api/fusion/interference-comp` — GNSS/INS fusion calibration.
- `GET|POST /api/calibrate/mag/start|stop|cancel|status` — interactive magnetometer calibration.
- `GET /api/tune/jobs`, `POST /api/tune` — auto-tuner.

**Web Push notifications** (see [`push-notifications.md`](push-notifications.md))
- `GET /api/push/status` — availability + subscription summary.
- `GET /api/push/pubkey` — VAPID public key (generates keypair on first call).
- `POST /api/push/subscribe`, `/unsubscribe`, `/test` — manage browser subscriptions.

**Supervisor (host-side update / backup daemon)**
- `GET|POST /api/supervisor/proxy/{path}` — constrained proxy to the supervisor's `/v1/…` API. Blocked in demo-readonly mode. Destructive paths (`update/apply`, `rollback`, `restore`) return 409 while underway unless `force:true`.
- `POST /api/supervisor/upload?name=&offset=&done=` — chunked `.bundle.tar` upload for OTA.

**WiFi (Raspberry Pi / SD-image setup)**
- `GET /api/system/wifi` — network mode, SSID, IP, hotspot state.
- `GET /api/system/wifi/scan` — visible WiFi networks.
- `POST /api/system/wifi/join {ssid, psk}` — join a network (background; restores hotspot on failure).

**Preferences / backup / misc**
- `GET /api/prefs`, `PUT /api/prefs` — UI-preference KV store (browser-as-cache).
- `POST /api/backup`, `POST /api/restore` — versioned backup ZIP.
- `GET /api/trips`, `/trips/{id}`, `/trips/{id}.gpx`, `DELETE /api/trips/{id}` — trip log.
- `GET /api/session/list`, `POST /api/session/upload`, `GET /api/session/upload/status` — opt-in session upload.
- `GET /api/debug/sessions|download`, `POST /api/debug/start|stop|replay|replay/stop`, `GET /api/blackbox/dumps|download` — debug recorder + flight-recorder.
- `GET /api/connectors`, `POST /api/connectors/{name}/arm|settings`, `GET /api/connectors/{name}/debug` — connector framework.
- `GET /api/weather/presets` — named sim weather presets.
- `POST /api/restart` — re-exec the server process.
- `POST /api/client-log` — RUM / client log ingestion.

## Known command gap: `return_to_launch`

`return_to_launch` is accepted by `Runtime.handle_command` (dispatched in
`app.py`) but is **not declared in `contract.py` COMMANDS**. It is therefore
invisible to the contract drift test and to any client introspecting
`GET /api/contract`. The canonical path for RTL is `POST /api/route/rtl` (runs
the heavy plan off the event loop in an executor). The WS/REST command shorthand
`{"type":"return_to_launch"}` works but is intentionally undeclared — code that
needs a guaranteed-stable type should use the REST endpoint instead.

Other accepted-but-undeclared commands (sim/testing, intentionally excluded from
the drift test): `trip_start`, `trip_stop`, `set_environment`, `weather_preset`,
`set_battery`, `teleport`, `inject_nmea`, `set_gps_offset`, `clear_gps_offset`,
`sim_fault`.

See [`docs/ui-contract.md`](ui-contract.md) for the full command payloads.
