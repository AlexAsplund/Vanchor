# UI ↔ runtime contract

The web UI talks to the runtime over **one WebSocket** (`/ws`, ~5 Hz telemetry JSON
down; command JSON up) plus a few **REST** endpoints. This is the stable contract
the front-end builds against.

## Telemetry (WS `/ws` and `GET /api/state`) — fields
Read everything defensively (older runtimes may omit fields).

Core nav: `mode` (manual|anchor_hold|heading_hold|waypoint|follow_apb|drift),
`position{lat,lon}`, `heading_deg`, `sog_knots`, `depth_m`,
`truth{lat,lon,heading_deg,speed_mps}` (sim only), `fix_seq`, `has_fix`.

Anchor / nav targets: `anchor{lat,lon}|null`, `anchor_radius_m`, `anchor_heading`,
`target_heading`, `distance_to_anchor_m`, `distance_to_waypoint_m`,
`cross_track_m`, `bearing_to_dest`, `waypoints[]`, `active_waypoint`,
`route_complete`, `est_drift_mps`, `est_drift_dir`.

Motor / **steering** (closed-loop unit): `motor{thrust,steering,steer_angle_deg}`,
and `steering{ commanded(-1..1), target_deg, angle_deg(feedback), rate_dps,
range_deg, wrap_pct(-100..100), feedback_ok }`.

Subsystems: `safety{...}`, `cruise{enabled,target_knots}`,
`track{recording,count,points[[lat,lon]]}`, `depth_points[[lat,lon,depth]]`,
`sensors{heading_rejected,position_rejected}`,
`environment{current_speed,current_dir,wind_speed,wind_dir,gust_amplitude_mps,wind_gust_now}`.

**New** `boat{ length_m, beam_m, mass_kg, max_speed_mps, max_thrust_n,
thruster_mount("bow"|"stern"), max_steer_angle_deg, max_turn_rate_deg,
shaft_dia_mm, steer_range_deg, steer_reduction }` — current boat profile.

**New** `calibration{ running, phase(idle|straight|turn|coast|tuning|done|error),
progress(0..1), message, results|null }` where `results = { max_speed_mps,
accel_tau_s, max_turn_rate_dps, steering_sign(±1), drag_tau_s,
tuned{heading_kp,heading_kd,anchor_kp,...} }`.

## Commands (WS up, or `POST /api/command`) — `{type, ...}`
- `manual {thrust(-1..1), steering(-1..1)}`
- `anchor_hold {radius_m?, hold_heading?}` · `heading_hold {throttle?, heading?}`
- `goto {lat, lon, on_arrival?}` · `load_route {points:[[lat,lon]], on_arrival?}`
- `follow_apb {}` · `drift {target_knots?}`
- `jog {direction: forward|back|left|right}` (nudges the anchor)
- `cruise {knots}` / `cruise {enabled:false}`
- `record {}` `replay {}` `backtrack {}` · `stop {}`
- `set_environment {current_speed?,current_dir?,wind_speed?,wind_dir?,gust_amplitude_mps?}`
- `teleport {lat,lon}` · `inject_nmea {sentence}` (sim/testing)

## REST
- `GET /api/state` → full telemetry snapshot.
- `POST /api/command` → `{ok:true}`.
- `GET /api/tune/jobs` · `POST /api/tune {job,max_evals,apply}` — auto-tuner.
- **New** `GET /api/boat` → boat profile (same shape as telemetry `boat`).
- **New** `POST /api/boat {fields...}` → update + apply live; returns the profile.
- **New** `POST /api/calibrate {mode:"quick"|"full"}` → `{started:true}`; an
  auto-calibration **drive** runs maneuvers, measures the boat, auto-tunes, and
  applies the result. Progress streams in telemetry `calibration`.
- **New** `POST /api/calibrate/cancel` → `{cancelled:true}`.

`sim_enabled` (bool) is in telemetry so the UI can show/hide simulator-only
controls (environment sliders, teleport).
