# u-blox M9N (UBX) + HWT901B IMU fusion

A GNSS/INS path for a tighter anchor hold, added **additively** — every existing
hardware combo (sim, serial-NMEA, NMEA-bridge, HWT-only, no-GPS) behaves exactly
as before. It has three layers.

## 1. UBX ingestion — `nav/ubx.py` + `hardware/drivers/ublox.py`
The M9N speaks NMEA by default, but its native **UBX-NAV-PVT** message carries
what NMEA can't: the **NED ground-velocity vector** (`velN/velE/velD`) and
**per-fix accuracy** (`hAcc`, `sAcc`). The velocity vector is clean *even at ~0
speed* — precisely the anchor hold regime where NMEA's COG is undefined.

- `nav/ubx.py` — a pure, stdlib-only parser: frame sync + Fletcher checksum,
  `parse_stream` (resyncs past garbage, tolerates partial frames), `decode_nav_pvt`,
  and `cfg_valset` / `cfg_marine_10hz` config builders (10 Hz, **sea** dynamic
  model, NAV-PVT on, NMEA off — configured on **both UART1 and USB**, so it works
  however the receiver is wired; the unused port's keys are valid no-ops). All CFG
  key IDs are **bench-verified on a real M9N** (see below).
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
  vectored anchor hold push against the *actual* drift, not the heading.
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
- **`interference`** (bow tied off, ramp the motor AND sweep the steering) → how
  far the magnetic heading drifts from the magnetics-free **gyro reference**,
  fitted as a 2D thrust×steer model (the steering servo rotates the motor, so its
  field direction turns too). Reported as max drift, per-thrust coefficients, and
  a **0–100 quality score** (0 = interference makes the compass unusable, 100 =
  the motor doesn't move it at all), plus **escalating mitigation
  recommendations** (move the IMU / twist supply pair → mu-metal shielding /
  bonding → dual-antenna GNSS) and an opt-in **experimental software remedy** that
  subtracts the fitted drift from the heading in real time.

The navigator records during a capture, subtracts the gyro bias from the IMU rate,
adds the mounting offset to the heading, and applies tuned gains live
(`apply_calibration`); results persist to `fusion_cal.json` and re-apply at
startup. Driven by `/api/fusion/calibrate/{start,stop,save,reset}` (start takes a
`mode`) + a "Sensor calibration" step in Devices — each mode runnable on its own,
plus a **Calibrate-all** guided sequence for first setup so nothing is missed.
Kept **separate from the one-time boat-setup wizard** (re-run when a sensor moves).
The captured noise is also a natural input for matching the sim/ML to a specific
boat.

## Bench verification (2026-07-05, real M9N over USB)
Everything the parsing/config layer claims was validated against a physical M9N
(`/dev/ttyACM0`, the stable `/dev/serial/by-id/usb-u-blox_AG_...` path):

- **Every `cfg_marine_10hz` VALSET key ACKs**, and the rate config takes effect —
  **10 Hz NAV-PVT confirmed** on the wire.
- **The MSGOUT keys are per-port** (`I2C=6 / UART1=7 / UART2=8 / USB=9 / SPI=a`).
  The first config only enabled UART1 and a USB-connected M9N stayed silent — the
  driver now configures **both UART1 and USB** (NAV-PVT on, UBX on, NMEA off).
- The `UbloxGps` driver ran end-to-end: opened, configured, parsed ~10 frames/s,
  and correctly **dropped not-OK fixes** — indoors the receiver often reports a 3D
  solution with `gnssFixOK = 0` (outside its accuracy limits); the driver only
  publishes fixes the receiver itself trusts. The per-device 🐞 **Debug view**
  shows `fix_type` vs `valid(gnssFixOK)` side by side so this state is
  diagnosable at a glance.
- Config is written to the **RAM layer only** — the receiver reverts to stock
  (multi-GNSS NMEA) on power-cycle; the driver re-applies it on every connect.

## Using it
- Settings → Devices → GPS source → **"u-blox M9N (UBX)"**, port = the
  `/dev/serial/by-id/...` entry from the dropdown (survives replug/renumber).
  Note **"Auto (follows mode)" never selects the ublox** — Auto resolves to
  serial-NMEA/sim off the hardware switch; the UBX driver is always an explicit
  choice.
- On **USB** (`ttyACM*`) the baud/framing settings are ignored (USB CDC-ACM);
  any values work. On the **Pi UART** use 38400 8N1 (the M9N UART default).
  The process needs the `dialout` group to open the port.
- Watch it live via the GPS 🐞 Debug stream (frames, fix quality, the NED
  velocity vector, hAcc/sAcc).

## Measured reality: indoor multipath (and what actually helps)
A 60 s stationary capture indoors-by-a-window characterised the M9N's worst-case
jitter: **~5.7 m 2D RMS** position scatter that is a **slow random-walk**
(consecutive fixes ~0.08 m apart but wandering ~9 m/min — multipath, not white
noise), a **~0.4 m/s phantom velocity**, and a self-reported **hAcc ≈ 15 m** (vs
~1.5 m open-sky). Consequences, all shipped:

- `sensors.gps_jitter: "indoor"` gives the **sim GPS the same jitter character**
  (OU random-walk + phantom velocity + large hAcc) for testing the autopilot.
- `sensors.gps_position_filter` enables an **accuracy-weighted position low-pass**
  (time constant grows with hAcc above a good-fix threshold; passthrough on good
  fixes). It cuts the scatter the anchor hold sees by ~20% but **cannot** remove
  the slow wander or the phantom velocity — no causal filter can.
- The honest mitigation is **hAcc-adaptive control tolerance** (don't hold tighter
  than the receiver's reported accuracy) — still an open roadmap item.

## Status
- Fully unit-tested (parser / fusion / driver / navigator wiring / calibration,
  incl. the non-blocking guarantee) and the UBX + driver path is
  **hardware-verified** per above. Suite green.
- **Still open**: field-tuning the fusion gains on the water (the still-capture
  calibration measures them per boat), the hAcc-adaptive hold radius, and — for
  genuinely magnetics-free heading at zero speed — a dual-antenna (F9-class)
  receiver. The M9N is standard-precision (~1.5 m open sky); this stack buys
  *smoother, faster, drift-aware, crab-aware* state, not RTK precision.
