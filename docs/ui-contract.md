# UI ↔ runtime contract

The web UI talks to the runtime over **one WebSocket** (`/ws`, ~5 Hz telemetry JSON
down; command JSON up) plus a few **REST** endpoints. This is the stable contract
the front-end builds against.

## Telemetry (WS `/ws` and `GET /api/state`) — fields
Read everything defensively (older runtimes may omit fields).

Core nav: `mode` (manual|anchor_hold|anchor_ml|heading_hold|waypoint|work_area|follow_apb|drift|contour_follow|orbit|trolling),
`position{lat,lon}`, `heading_deg`, `sog_knots`, `depth_m`,
`truth{lat,lon,heading_deg,speed_mps}` (sim only), `fix_seq`, `has_fix`.

Anchor / nav targets: `anchor{lat,lon}|null`, `anchor_radius_m`, `anchor_heading`,
`target_heading`, `distance_to_anchor_m`, `distance_to_waypoint_m`,
`cross_track_m`, `bearing_to_dest`, `waypoints[]`, `active_waypoint`,
`route_complete`, `route_loop`, `route_patrol`, `drift_target_knots`,
`est_drift_mps`, `est_drift_dir`.

Work Area: `work_holding` (bool — currently holding position at a spot), `work_dwell_remaining_s`,
`work_spot_count`.

Safety / nav extras: `launch{lat,lon,set}` (recorded home point), `rtl_recommended` (bool),
`mob{active,lat,lon}` (man-overboard mark), `nav{paused,suspended_mode}`.

Guided pattern modes: `contour{target_depth_m,depth_m,error_m}`,
`orbit{center_lat,center_lon,radius_m,direction,range_m}`,
`trolling{base_heading,amplitude_deg,period_s,phase}`.

Speed/throttle: `cruise{enabled,target_knots}`, `throttle_override{active,percent}`,
`gps_offset{dlat,dlon,active}`, `battery{soc_pct,voltage_v,current_a,draw_w,range_m,time_to_empty_s}`.

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

**Motion / modes**
- `manual {thrust(-1..1), steering(-1..1)}` · `stop {}` — relative steering (deflection off the bow)
- `manual {thrust(-1..1), steer_bearing(0..360)}` — ABSOLUTE steering: the motor head holds the compass bearing (0=N, 180=S) while the boat yaws (recomputed from the live heading each tick); any relative `manual` clears it
- `manual {thrust(-1..1), steer_course(0..360)}` — COURSE hold: follow the ground-track LINE drawn from the engage position along the bearing (cross-track corrected, ±45° authority). The line anchors when the course value changes; re-sending the same course (thrust tweaks) keeps it. Telemetry `manual_course {bearing,lat,lon}|null` carries the anchored line for the chart overlay
- link-loss failsafe: telemetry `link.failsafe_action` reports what engaged — `"continue"` (guided modes keep flying; the default), `"hold"` (anchor-hold, `link_loss_continue_mission: false`), `"stop"` (manual deadman)
- `anchor_hold {anchor?{lat,lon}, radius_m?}` — drop anchor at current position (or supplied point)
- `anchor_ml {anchor?{lat,lon}, radius_m?}` — ML-trained anchor hold; falls back to PID if model absent
- `heading_hold {throttle?, heading?}` — hold a compass heading (defaults to current heading). DEPRECATED in the UI (2026-07-15): superseded by `manual {steer_bearing|steer_course}`; kept for the API / RF remotes / NMEA2000 connectors
- `goto {waypoints:[{lat,lon,name?,throttle_pct?,speed_kn?},...], on_arrival?, loop?, patrol?, throttle?, active?}` — follow waypoints; `active` for live in-place edits (resume from that index without restarting). A waypoint's optional `throttle_pct` (engine %, 0..100) **or** `speed_kn` (SOG target) is adopted on arrival at that mark for the following legs, via the throttle-override / cruise channels (so `set_throttle`/`cruise` sent mid-route override it until the next speed-carrying mark)
- `load_route {gpx, loop?, patrol?, throttle?}` — start navigation from GPX text
- `follow_apb {throttle?}` — track external autopilot bearing (NMEA APB sentences)
- `drift {knots?, heading?}` — controlled drift at a target SOG; heading defaults to current

**Fishing / pattern modes**
- `contour_follow {target_depth_m, side?: deep|shallow, speed_knots?}` — hold a depth contour (isobath)
- `orbit {center_lat, center_lon, radius_m?, direction?: cw|ccw, speed_knots?}` — loop a fixed point
- `trolling {base_heading?, amplitude_deg?, period_s?, speed_knots?}` — sinusoidal S-curve weave
- `work_area {waypoints:[{lat,lon,heading?,name?},...], dwell_s?, advance?: manual|timed, loop?, patrol?, throttle?}` — visit spots, hold position at each
- `next_spot {}` — advance to the next Work Area spot (manual-advance mode)

**Speed / nav control**
- `jog {direction: forward|back|left|right, distance_m?}` — nudge the anchor point boat-relative
- `cruise {knots}` / `cruise {enabled:false}` — hold SOG via PID; 0/false disables
- `set_throttle {percent: 0..100}` — override guided-mode engine power % (0 to clear)
- `pause_nav {}` — suspend the active guided mode and hold position (anchor-hold)
- `resume_nav {}` — restore the mode that was paused

**Track recording**
- `record {action: start|stop|clear}` — breadcrumb recorder
- `replay {}` — navigate forward along the recorded track
- `backtrack {}` — navigate backward along the recorded track

**Safety**
- `set_nogo_zones {zones:[[[lat,lon],...],...]}`  — update geofence no-go polygon rings (empty list clears)
- `set_min_depth {min_depth_m}` — shallow-water auto-stop threshold
- `set_fix_failsafe {enabled}` — enable/disable loss-of-fix motor cut
- `set_auto_apb {enabled}` — auto-engage Follow-APB when an APB feed appears (idle-manual only; persisted; telemetry `auto_apb {enabled, engaged}`)

**Return-to-launch / MOB**
- `set_launch {}` — record the current position as the launch/home point
- `return_to_launch {}` — navigate back to launch via water routing; anchors on arrival
- `mob {}` — mark current position as Man-Overboard and return to it
- `mob_clear {}` — cancel an active MOB return

**Trip log**
- `trip_start {name?}` — manually start a trip log (replaces any active one)
- `trip_stop {}` — stop and persist the active trip

**Sim / testing**
- `set_environment {current_speed?,current_dir?,wind_speed?,wind_dir?,gust_amplitude_mps?}` — live-edit sim environment
- `weather_preset {id}` — apply a named preset (calm|lake|river|coastal) to the sim
- `set_battery {soc_pct}` — override sim battery state-of-charge (0..100 %)
- `teleport {lat,lon,heading?}` — snap the simulated boat to a new position
- `inject_nmea {sentence}` — inject a raw NMEA sentence into the navigator
- `set_gps_offset {true_lat, true_lon}` — calibrate a GPS position bias
- `clear_gps_offset {}` — remove the GPS position offset

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
