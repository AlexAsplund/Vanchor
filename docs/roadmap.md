# Roadmap

## Done (implemented this far)

All of the original roadmap items are now in the codebase:

- **Fossen 3-DOF physics** (`sim/fossen.py`) — surge–sway–yaw, modelling a single
  steerable **bow-mounted** trolling motor (steering authority scales with
  thrust; bow vs stern mount flips the yaw). Default model; geometry configurable
  via the `boat:` config section (default 4.1 m boat). See
  `docs/simulator-options.md`.
- **Real-hardware serial HAL** (`hardware/serial_link.py`, `serial_devices.py`) —
  `SerialGps`/`SerialCompass`/`SerialMotorController` behind the same interfaces
  as the sim, an abstracted transport (testable with a fake, no port needed), and
  a `--hardware` flag.
- **NMEA over TCP** (`nav/nmea_net.py`, `:10110`) for phone nav apps.
- **FollowAPB mode** (`controller/modes.py`) — steer from external APB sentences.
- **GPX routes** (`nav/routes.py`) — load/save; route editor in the UI.
- **Safety governor** (`controller/safety.py`) — thrust slew limit, fwd/rev delay,
  loss-of-fix failsafe, anchor drag alarm; surfaced in telemetry.
- **Observability** (`core/observability.py`) — structured logging, event
  wiretap, telemetry recorder + `/api/log`.
- **Typed YAML/JSON config** (`core/config.py`, `vanchor.example.yaml`).
- **UI polish** — mode-aware panels, route editor, NMEA console, safety banner,
  dark mode, **time-series charts**, and a **trolling-motor direction arrow**.
- **Anchor hold** rewritten: reverse thrust + velocity braking (no orbit),
  graduated recover/station-keep/idle response (no GPS-noise hunting, no spin),
  steering-freeze at idle (servo protection). Tuned with the new analysis
  framework — see `docs/analysis.md`.
- **Simulation analysis framework** (`vanchor.analysis`) — headless scenario
  runner, metrics, reports, plots.

## Candidate features (from GPS trolling-motor research)

Researched against Minn Kota i-Pilot, Garmin Force, MotorGuide, Lowrance Ghost,
Rhodan. Ranked by value-for-effort and fit with our event-driven
`ControlMode`/`Helm`/PID architecture. **Tiers 1–2 and Tier 3 #6 are now done;
plus bonus modes not originally listed (Work Area survey, learned ML anchor).**

### Tier 1 — DONE ✅
1. **Spot-Lock Jog** — `{type:"jog",direction:...}` nudges `state.anchor` 1.5 m
   boat-relative; `AnchorHoldMode` re-centres automatically. (`controller.py`,
   UI d-pad.)
2. **Cruise Control (constant SOG)** — a SOG PID on the controller takes over the
   throttle of guided "underway" modes (heading/waypoint/follow-APB) when a
   target speed is set (`{type:"cruise",knots:N}`). Holds target SOG to ~0 error
   in sim. The reusable primitive that also unlocks Drift mode.
3. **Record-a-track & replay + BackTrack** — `TrackRecorder` (`nav/track.py`)
   breadcrumbs `state.position`; `{type:"replay"}` / `{type:"backtrack"}` feed the
   points (forward / reversed) into `WaypointMode`.

### Tier 2 — DONE ✅
4. **Drift mode** (`DriftMode`) — holds a heading while a *bidirectional* SOG PID
   holds a low target drift speed (`{type:"drift",heading:H,knots:S}`); the motor
   trims speed (incl. reverse braking) as wind/current carry the boat. Holds
   target SOG to ~0 error under winds 0–6 m/s in sim.
5. **Chart-tap "go to" + on-arrival action** — `{type:"goto",...,
   on_arrival:"anchor"|"stop"|"none"}`; `WaypointMode` sets `route_complete` at
   the end and the controller fires the action once (auto-anchor or stop). UI:
   tap the map to go.

### Tier 3 — DONE ✅
6. **Follow shoreline / depth contour at an offset** — `CONTOUR_FOLLOW`
   (Along-contour) mode: tap a contour line on the chart and it builds a track
   along it (chaining same-depth pieces), with optional patrol
   (`nav/contour_route.py`). Also shipped a **Work Area** survey mode
   (`WORK_AREA`, `nav/survey.py`) — a drawn area covered spot-by-spot with
   per-spot dwell + heading — which wasn't in the original list.

### Bonus modes (not originally listed)
- **Learned ML anchor** (`anchor_ml`, `controller/anchor_ml.py`) — a ~1.6k-param
  numpy MLP station-keeper trained by ES on the Fossen physics
  (`experiments/anchor_policy/`), shipped as `controller/anchor_policy.json`.
  Selectable in the UI via the Anchor panel's "Smart station-keeping" toggle;
  falls back to the PID `anchor_hold` if the model is unavailable.

## Engineering debt / smaller follow-ups
- **Auto-assisted PID tuning — DONE** ✅ (`vanchor.analysis.tuning` +
  `python -m vanchor.analysis.tune`, **and** in the web UI via `POST /api/tune`):
  a dependency-free coordinate-descent optimiser scores scenarios via metrics to
  suggest gains for the heading, anchor, cruise and drift loops, with optional
  live-apply. Adopted defaults after cross-loop validation: heading kp
  0.025→**0.035** (faster settle, anchor-safe — the tuner's raw 0.051 regressed
  anchor-drift so a validated compromise was used), cruise kp 0.5→**0.64**. Still
  TODO: per-boat saved gain profiles; persist applied gains back to a config file.
- The "Hold heading while anchored" UI checkbox is now a passive no-op (the boat
  holds heading inherently at idle) — remove or repurpose it.
- The async runtime loops (`Simulator.run`, `Controller.run`, sensor `_loop`s) are
  exercised only indirectly via the API tests — no dedicated timing tests.
- No hardware-in-the-loop tests (requires hardware); serial drivers are unit-
  tested against a fake transport only.
- **COG/declination** — now handled for the **HWT901B AHRS** compass
  (`hardware/drivers/hwt901b.py`): it learns declination + the compass mount
  offset from GPS course-over-ground on straight runs, no magnetic model needed.
  Still stubbed (magnetic == true) for the sim/other compass sources — see
  `docs/assumptions.md`.
- **Interactive magnetometer calibration** — the HWT901B `calibrate_mag` action
  is stubbed ("coming next"); the rotate-through-360° hard/soft-iron fit routine
  is not built yet.
- **Persist the learned compass offset** — the GPS-learned declination/mount
  offset lives in memory and re-learns after each restart; not yet persisted.
- Optional realism upgrade: vectored/azimuth station-keeping that exploits the
  motor's full ~360° rotation (currently steering uses an effective ±35° band).
