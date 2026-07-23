# Simulator

The simulator lets the whole autopilot run — control loops, modes, safety, UI —
with no boat and no hardware. The controller can't tell it's in a sim: it steers
only on *perceived* sensor state, and the command path is byte-identical to the
real one.

## Design

Two clean seams make the physics and the sensors swappable:

- **Physics** — `Boat.step(dt, command, env) -> BoatState` (`sim/boat.py`).
  Anything that turns a `(thrust, steering)` command into ground-truth
  `(lat/lon, heading, speed)` can replace it.
- **Sensors** — `SimGps` / `SimCompass` (`sim/devices.py`) derive noisy NMEA
  from the truth and publish to `events.NMEA_IN`, exactly as a real driver would.

Every sim-only tool (teleport, weather, battery setter, fault injection) is gated
behind `simulator is not None`, so none of it can leak into a real deployment.

## Physics model

Two models ship; pick with `sim.model`:

| `sim.model` | What it is |
|---|---|
| **`fossen`** (default) | 3-DOF maneuvering model (surge/sway/yaw) with coupled dynamics, current/wind forces, Coriolis, and bow-mount awareness. Realistic momentum and turning. |
| `simple` | Zero-coupling kinematic model. Lighter; fine for quick control-loop work. |

The Fossen model is validated against Fossen's reference implementation; it's the
default because it captures the momentum and crab that a trolling boat actually
has.

## Fidelity vs. the real boat

The sim is close but not perfect. What matches, and what to keep in mind:

**Matches reality.** Control loop + governor (same code, 5 Hz, same slew limits),
every software failsafe (fix-loss, shallow-stop, no-go, drag alarm, battery
ladder, RTL, link-loss, land-guard — the chaos suite pins them), calibration and
auto-tune, the learned anchor policies, and — by default — the firmware's motor
feel (1.0/s throttle slew, 1 s reverse dead-time, 0.8 s watchdog, 8-bit PWM
quantization). GPS runs at the M9N's 10 Hz.

**Known gaps** (documented, not yet closed — most need bench/water data):

| Gap | Impact | Note |
|---|---|---|
| Steering-head lag | Medium | Real head has a PID position loop (1.2° deadband, settle time); sim applies the slewed command directly. Needs a bench-measured model. |
| No steering feedback in sim | Medium | `steering.angle_deg` is measured on real hardware, modeled in sim; UI ghost needles show command, not truth. |
| Prop spin-up lag | Medium | `thrust_lag_tau_s` exists but defaults 0 — a real prop takes ~100–500 ms to bite. Needs water data. |
| Anchor-policy reverse dead-time | Medium | The ES training env doesn't model the motor's 1 s reverse dead-time, so learned policies over-reverse. Mitigated by a 0.7 s output low-pass; real fix is a retrain — see [anchor-ml.md](anchor-ml.md). |
| Fusion / GPS-noise off by default | Test coverage | Velocity fusion and realistic GPS/compass noise exist but default off so regressions stay attributable. Enable `sensors.gps_velocity: true` / `gps_jitter: indoor` to exercise them. |
| Hardware watchdog GPIO | Medium | The relay motor-cut on Pi hang can't exist in sim — a real-hardware checklist item. |

**Rule of thumb:** anything tuned in sim gets a short on-water shakedown —
head-settle and prop spin-up are optimistic, so gains may need a nudge down.
`sim.time_scale != 1` is for demo videos, never for judging control quality (the
control loop stays wall-clock; only the physics speeds up).

## Demo mode

`vanchor --demo` (or the `demo:` config block) boots the sim already moving, so a
first-time visitor sees a live map the instant their browser connects. It forces
full simulation, uses a throwaway data dir (never touches `vanchor_data/`), and
shows a DEMO badge.

| Key | Default | Meaning |
|---|---|---|
| `demo.enabled` | `false` | Master switch. |
| `demo.readonly` | `false` | Pin every client to observer; STOP still works. |
| `demo.scenario` | `route` | `route` (looping triangle) or `anchor` (hold at start). |
| `demo.start_lat` / `demo.start_lon` | charted lake | Where to start. |
| `demo.weather_preset` | `lake` | Sim weather at boot (`""` = calm). |

Env equivalents: `VANCHOR_DEMO=1`, `VANCHOR_DEMO_READONLY=1`.
