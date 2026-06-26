# Assumptions

Decisions made to reach a working baseline without hardware. Each is a
deliberate simplification, not an oversight; revisit as the project matures.

## Hardware / environment
- **No physical GPS, compass, or motor controller is available.** Everything
  runs against simulated devices that implement the same interfaces.
- Python 3.11+ (developed/tested on 3.12). asyncio is the only concurrency
  model; no threads.

## Navigation & sensors
- **Magnetic heading == true heading.** We do not yet apply magnetic
  declination/variation (the original used a WMM2020 model + geomag). Anchor
  hold and waypoint steering are unaffected because they steer on *bearings*
  derived from GPS positions; only absolute compass-vs-chart alignment is off.
- **Spherical-Earth geodesy** (radius 6 371 km). Sub-metre accurate over the
  tens–hundreds of metres relevant to anchoring/close steering. Not for ocean
  passages.
- GPS emits **RMC** (position + SOG + COG); compass emits **HDM**. GGA and HDT
  are parsed if received but not emitted by the default sim.
- Simulated GPS **COG/SOG are the true velocity over ground** (hull motion +
  wind/current drift), as a real GPS reports. This is what lets the controller's
  velocity term *anticipate* drift and lets the anchor mode estimate the drift
  vector (shown as `est_drift_mps`/`est_drift_dir` in telemetry). COG/SOG are
  reported clean (no extra noise); when nearly stationary COG falls back to
  heading.
- Sensor noise is **Gaussian and seeded** (GPS ~1.5 m 1σ, compass ~1° 1σ) for
  reproducible tests.

## Boat physics (simulator)
Two interchangeable models (config `sim.model`), both reading boat geometry from
the `boat` config section (default: a **4.1 m** boat, 1.7 m beam, ~300 kg):

- **`fossen`** (default) — a 3-DOF surge–sway–yaw maneuvering model. The trolling
  motor is modelled as a **single steerable thrust vector applied at the
  configured mount position** (`boat.thruster_mount`, default `bow`, ~1.7 m
  forward of CG). Consequences that match real hardware: steering authority
  **scales with thrust** (a trolling motor can't steer without running), and a
  **bow** mount pulls the bow around (a stern mount would push it — opposite yaw
  sign, handled automatically by the signed longitudinal offset). Surge damping
  is derived so full thrust settles at `boat.max_speed_mps` (default 1.6 m/s);
  full-steer turn ≈ 17°/s. Visible sway/crab during turns.
- **`simple`** — a first-order speed lag + kinematic yaw rate (steering →
  yaw independent of thrust and mount). Lightweight; does **not** model bow-mount
  effects. Good for fast deterministic control tests.
- Wind/current produce a **linear drift velocity** added to hull motion;
  leeway = 3% of wind speed. No waves, no heel, no prop-walk.

## Steering actuator
- The steering head has a finite **rotation speed** (`boat.max_steer_rate_dps`,
  default 50 deg/s) — the safety governor slew-limits the steering command so it
  is physically realisable (a geared stepper can't slam back and forth).
- The helm output is **low-passed** (`control.steer_tau`, default 0.6 s) so the
  command isn't driven by ~1° compass noise; without it the motor would slew at
  its max rate continuously. Together these cut steady steering activity ~5×
  (e.g. heading-hold mean ~25→5 deg/s) while *improving* control (faster heading
  settle, higher anchor within-radius %). Measured with `analysis.steering_activity`.

## Anchor hold (virtual anchor)
- Two phases with hysteresis: **return** (outside the radius → point the bow at
  the anchor and drive back) and **station-keep** (inside → by default hold the
  heading captured when the anchor was dropped, rather than spinning to face the
  mark). Radius and hold-heading are settable live from the UI / per command.
- With the `fossen` model, holding heading while station-keeping needs a little
  thrust (`anchor_station_thrust`) because steering authority requires flow over
  the motor — a faithful limitation of a single bow thruster, not a bug.

## Control
- `MotorCommand` is normalized: `thrust ∈ [-1, 1]`, `steering ∈ [-1, 1]`. The
  mapping to real ESC/PWM and stepper counts is deferred to the real driver.
- One shared heading PID (the Helm) serves all guided modes.
- PID gains are **hand-tuned for the default simulated boat**. Real hardware
  (and different boats) will need re-tuning; gains are constructor parameters,
  not hard-coded magic.
- Anchor hold uses a distance PID with a dead-band at half the anchor radius to
  avoid motor chatter near the centre; equilibrium sits a few metres out under
  steady drift, inside the radius.

## UI
- Leaflet + OpenStreetMap tiles loaded from public CDNs ⇒ the UI needs internet
  for the map tiles and Leaflet JS/CSS (the control loop itself does not).
- The UI shows both **ground truth** (green arrow) and the **GPS fix** (blue
  dot) for debugging; a real deployment would only have the GPS fix.
