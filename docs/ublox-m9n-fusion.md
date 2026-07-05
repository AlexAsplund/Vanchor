# u-blox M9N (UBX) + HWT901B IMU fusion

A GNSS/INS path for a tighter spot-lock, added **additively** — every existing
hardware combo (sim, serial-NMEA, NMEA-bridge, HWT-only, no-GPS) behaves exactly
as before. It has three layers.

## 1. UBX ingestion — `nav/ubx.py` + `hardware/drivers/ublox.py`
The M9N speaks NMEA by default, but its native **UBX-NAV-PVT** message carries
what NMEA can't: the **NED ground-velocity vector** (`velN/velE/velD`) and
**per-fix accuracy** (`hAcc`, `sAcc`). The velocity vector is clean *even at ~0
speed* — precisely the spot-lock regime where NMEA's COG is undefined.

- `nav/ubx.py` — a pure, stdlib-only parser: frame sync + Fletcher checksum,
  `parse_stream` (resyncs past garbage, tolerates partial frames), `decode_nav_pvt`,
  and `cfg_valset` / `cfg_marine_10hz` config builders (10 Hz, **sea** dynamic
  model, NAV-PVT on, NMEA off). **The CFG key IDs need bench verification on a
  real M9N** (flagged in the code) — the parsing is exact and fully tested.
- `hardware/drivers/ublox.py` — `UbloxGps`, a registry driver (`gps_source: ublox`)
  that configures the receiver on open, parses NAV-PVT off a byte transport, and
  publishes a rich `GpsFix` (with velocity + accuracy) on the new `GPS_FIX_IN`
  event. Reconnects through drops like the NMEA devices.

`GpsFix` gained optional `vel_n_mps/vel_e_mps/vel_d_mps/h_acc_m/s_acc_mps`
(None on NMEA/sim fixes, so nothing else changes).

## 2. GNSS/INS fusion — `nav/fusion.py`
`NavFusion` is a pure, loosely-coupled **complementary filter** (not an EKF):

- **Heading**: gyro-integrated, complementary-corrected toward the compass →
  smooth, high-rate heading + a real **yaw-rate** signal.
- **Ground velocity**: low-passed toward the GPS velocity vector (or derived from
  SOG/COG, or a position delta) → clean low-speed velocity.
- **Crab / set**: `course − heading` (positive = set to starboard) → lets a
  vectored spot-lock push against the *actual* drift, not the heading.
- **Dead-reckoning**: coasts position from the last velocity through a brief GPS
  gap (under a dock/bridge), flagged `dead_reckoning`.

It degrades gracefully with partial sensors (NMEA + IMU still gets heading-rate +
crab; no compass still gets velocity).

## 3. Wiring — additive, guarded
The navigator feeds the fusion from whatever arrives (RMC/GGA, `GPS_FIX_IN`, IMU,
compass) and writes **only** the additive `state` fields — `yaw_rate_dps`,
`ground_vel_n/e_mps`, `crab_deg`, `dead_reckoning` (surfaced under telemetry
`fusion`). **Heading, position and the control law are untouched**, so the filter
can never change how an existing boat behaves; the controller/ML can opt into the
richer signals later (a natural retrain target). Enabled by
`sensors.fusion_enabled` (default on); `fusion=None` disables it entirely.

## Capability-driven activation (source-agnostic)
The enhanced behaviour keys off **what a `GpsFix` carries, not which driver made
it** — the velocity/accuracy live on `GpsFix`, and the fusion branches on
`fix.has_velocity` / `has_3d_velocity` / `has_accuracy`. So *any* source that
fills those fields lights up the same path: the UBX M9N today, a future GNSS
module, a SignalK/NMEA-2000 bridge, or the simulator. Concretely, a **measured**
velocity vector (vs one derived from SOG/COG) sets `velocity_measured`, exposes
vertical velocity, and unlocks **crab at low speed** (a real receiver's velocity
is trustworthy near-stationary where COG is not). The sim proves it: set
`sensors.gps_velocity` and `SimGps` emits a velocity-carrying fix — the identical
fusion features activate with zero driver-specific code (and it gives an in-sim
harness to validate the whole path).

## Sensor calibration (capture-and-tune system-ID)
The fusion ships with hand-picked constants; short guided captures measure this
boat's sensors and tune to them. `nav/calibration.py` is the pure core — a
`CaptureBuffer` records raw per-channel samples + heading *frames* (each carries
the concurrent course, speed, thrust and a gyro-integrated reference), and mode
tuners analyse the same recording three ways. Every result is a `FusionCalibration`
where an unmeasured field is `None`, and results **merge**, so running one mode
never clobbers another's.

- **`still`** (boat stationary, motor off) → **gyro bias** (mean resting yaw rate)
  + per-sensor **noise σ**, from which the gains are derived (monotonic, clamped:
  noisier velocity → more smoothing + higher crab thresholds; noisier compass →
  gentler complementary blend).
- **`align`** (drive straight at cruise) → the steady compass-vs-GNSS-course
  difference is the compass/IMU **mounting yaw offset**, applied to the heading.
- **`interference`** (bow tied off, ramp the motor) → how far the magnetic heading
  drifts from the magnetics-free **gyro reference** as thrust rises, reported as
  max drift, a °/thrust slope, and a **0–100 quality score** (0 = interference
  makes the compass unusable, 100 = the motor doesn't move it at all).

The navigator records during a capture, subtracts the gyro bias from the IMU rate,
adds the mounting offset to the heading, and applies tuned gains live
(`apply_calibration`); results persist to `fusion_cal.json` and re-apply at
startup. Driven by `/api/fusion/calibrate/{start,stop,save,reset}` (start takes a
`mode`) + a "Sensor calibration" step in Devices — each mode runnable on its own,
plus a **Calibrate-all** guided sequence for first setup so nothing is missed.
Kept **separate from the one-time boat-setup wizard** (re-run when a sensor moves).
The captured noise is also a natural input for matching the sim/ML to a specific
boat.

## Status
- Fully unit-tested: UBX parser (15), fusion (9), driver (4), navigator wiring (3),
  incl. the non-blocking guarantee. Suite green.
- **Bench items** (can't verify without an M9N): the UBX-CFG key IDs, and tuning
  the fusion gains (`heading_gain`, `vel_tau_s`, `dr_timeout_s`) against real
  sensor noise. The M9N is standard-precision (~1.5 m) — this buys *smoother,
  faster, drift-aware, crab-aware* state, not RTK precision (that's the F9P).
