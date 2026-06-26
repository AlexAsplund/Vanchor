# Architecture

> Part of the `docs/llms/` developer guide. 🔁 **If you change the runtime
> shape, the loops, the shared state, or an invariant below, update this file.**

## One-paragraph mental model

`Runtime` (in `app.py`) is the spider in the web. At startup it builds either a
**simulator** or **real hardware devices**, a **`Navigator`** (turns sensor
sentences into a perceived state), and a **`Controller`** (turns perceived state
+ the active mode into a motor command). It then runs a handful of `asyncio`
loops that step physics, sample sensors, tick the controller, and broadcast
telemetry to the browser over a WebSocket. Everything shares one mutable
`NavigationState`.

## Data flow (the closed loop)

```
            ┌──────────── ground truth (sim) OR the real world ───────────┐
            │                                                              │
   sim/simulator.py  ── physics step ──►  BoatState (true pos/heading/vel) │
            │                                                              │
            ▼                                                              │
   sim/devices.py (or hardware/) ── emit NMEA ──►  GPS / compass / depth   │
            │  (adds realistic noise; see simulation.md)                   │
            ▼                                                              │
   nav/navigator.py  handle_sentence()                                     │
            │  parses NMEA, spike-guards, updates ...                      │
            ▼                                                              │
   core/state.py  NavigationState   ◄── the ONE shared mutable object ──┐  │
            │  perceived position, heading, fix (SOG/COG), waypoints,   │  │
            │  mode, cross_track, depth, anchor, alerts, ...            │  │
            ▼                                                            │  │
   controller/controller.py  control_tick(dt)                           │  │
            │  active mode (modes.py) computes a Setpoint               │  │
            │  (target_heading + thrust, or manual thrust+steering)     │  │
            │  helm converts heading-intent → steering, applies         │  │
            │  steer_sign, thrust-yaw feed-forward, slew/safety limits  │  │
            ▼                                                            │  │
        MotorCommand (thrust, steering)                                 │  │
            │                                                           │  │
            ▼                                                           │  │
   sim motor  ── force/torque ──►  back into the physics step ──────────┘  │
   (or hardware serial motor)                                              │
            │                                                              │
            └──────────────►  telemetry (state.to_dict) ── /ws ──► browser ┘
```

The browser sends **commands** (`POST /api/command` or over the socket) which
`Controller.handle_command` / `Runtime.handle_command` apply by mutating the
state / switching modes. See [api.md](api.md).

## The loops (rates matter)

The runtime drives several periodic tasks. The **rates are an invariant** — the
control law was tuned against them, and the test harness reproduces them
exactly. Canonical rates:

| Loop | Rate | Job |
|------|------|-----|
| physics step | ~20 Hz (`dt≈0.05`) | advance the simulator (no-op on real hardware) |
| control tick | ~5 Hz | run the active mode + helm → motor command |
| GPS sample | ~1 Hz | emit an RMC fix (this is the slow, quantised one) |
| compass sample | ~5 Hz | emit a heading sentence |
| telemetry broadcast | ~5 Hz | push `state.to_dict()` to all `/ws` clients |

⚠️ **Do not tick the controller every physics step in a test** — that produces a
faster control loop than reality and manufactures phantom oscillation. Use
`tests/harness.py`, which schedules each loop at its real period.

## Key objects (the vocabulary)

| Object | File | Role |
|--------|------|------|
| `Runtime` | `app.py` | owns everything; the async loops; `handle_command` |
| `NavigationState` | `core/state.py` | the single shared mutable state; `to_dict()` is the telemetry |
| `BoatState` | `core/models.py` | sim ground truth (point, heading, velocities) |
| `GeoPoint` / `Fix` | `core/models.py` | a lat/lon; a parsed GPS fix (SOG/COG) |
| `Environment` | `core/models.py` | wind/current/gust for the sim |
| `BoatConfig` | `core/config.py` | the boat's physical params (mass, thruster geometry, `hull_tracking`, …) |
| `Setpoint` (`GuidedSetpoint` / `ManualSetpoint`) | `controller/modes.py` | a mode's output: heading-intent+thrust, or raw thrust+steering |
| `MotorCommand` | `core/models.py` | the helm's output: thrust + steering to the motor |
| `ControlMode` | `controller/modes.py` | one steering behaviour (anchor, waypoint, orbit, …) |
| `Helm` | `controller/controller.py` | heading-intent → steering, with sign/feed-forward/limits |

## Invariants you must preserve

- **One shared `NavigationState`.** The navigator writes perceived fields; the
  controller reads them and writes its outputs/telemetry fields. Don't fork it.
- **Sim vs hardware are interchangeable behind the same interfaces, *per
  device*.** Devices emit NMEA; the motor takes a `MotorCommand`. Each of
  gps/compass/depth/motor independently picks `sim` / `serial` / `nmea` / `both`
  (see [backend.md](backend.md)), so you can bench-test a real servo on a
  simulated boat, or take GPS from external NMEA — code above the device layer
  never checks which. New device/motor types implement the same interface
  (`hardware/interfaces.py`, mirrored by `sim/devices.py`).
- **`steer_sign`** (in the helm) is +1 for a bow-mounted thruster and -1 for a
  stern mount, because a stern thruster yaws the boat the opposite way. It is
  derived from `BoatConfig.thruster_x_m()` sign and multiplies *all* steering.
  Getting this wrong is the classic "boat turns the wrong way / won't track" bug.
- **Thrust-yaw feed-forward** pre-cancels the yaw from a laterally-offset motor;
  geometry-derived, refined by calibration. See [simulation.md](simulation.md)
  and `docs/nav-control-api.md`.
- **Default = no-op.** New parameters default so behaviour is unchanged and the
  suite stays green (see e.g. `hull_tracking=1.0`, `gps_noise_m`).

## Where to go next

- Changing control/nav/config → [backend.md](backend.md).
- Changing physics/sensors/boat params → [simulation.md](simulation.md).
- Changing the UI → [frontend.md](frontend.md).
- Changing endpoints/commands/telemetry → [api.md](api.md).
