# Architecture

## Goals

1. **Software-first.** Develop and verify all navigation/control logic without
   any physical GPS, compass, or motor controller.
2. **Event-driven & async.** A clean asyncio core, not a tangle of threads.
3. **Hardware-swappable.** Simulated devices implement the *same* interfaces the
   real devices will, so swapping hardware in changes only device construction.
4. **Testable.** Pure logic (geo, PID, parsing, control modes) with no I/O, plus
   a deterministic closed-loop integration harness.

## What we learned from the original Vanchor

The original is a working, clever project but optimised for "it runs on my Pi",
not for testability:

- Event bus: `pymitter` (synchronous, wildcard string events).
- Concurrency: hand-rolled threaded workers polling at intervals.
- State: one big stringly-typed nested dict (`DataNode`) keyed by paths like
  `Navigation/Coordinates`. Powerful but untyped and easy to typo.
- Devices loaded by dynamic import from YAML config.
- Functions: `Vanchor` (virtual anchor, distance PID), `HoldHeading`
  (heading lock w/ or w/o GPS via cross-track error), `AutoPilot` (consume APB
  or generate APB from GPX waypoints). PID steering throughout.
- No simulator, no tests, no types; a few latent bugs (undefined variables).

We kept the **good ideas** (event-driven design, virtual anchor via a distance
PID, APB/cross-track steering, a per-function "mode" model) and discarded the
**implementation** (threads, stringly-typed global state, dynamic imports).

## The new design

### Layers (strict dependency direction: outer depends on inner)

```
        ui ──▶ app ──▶ controller ──▶ nav ──▶ core
                │           │           │       ▲
                └──▶ sim ───┴───────────┴───────┘   (sim implements hardware/)
                          hardware/ (HAL ABCs)
```

- **core/** — no I/O, no async. `models.py` (typed dataclasses: `GeoPoint`,
  `GpsFix`, `MotorCommand`, setpoints, `BoatState`, `Environment`), `geo.py`
  (haversine, bearing, destination, cross-track), `pid.py` (PID with
  anti-windup), `state.py` (`NavigationState`, the single typed state object),
  `events.py` (async pub/sub `EventBus`).
- **nav/** — `nmea.py` parses *and* encodes RMC/GGA/HDM/HDT/APB with checksums
  (no pynmea2 dependency); `navigator.py` is the single writer of *perceived*
  position/heading from sentences.
- **hardware/** — `interfaces.py`: the HAL. `Sensor`, `MotorController`,
  `Actuator` ABCs. This is the seam between the controller and the physical
  world.
- **controller/** — `controller.py` owns the active mode, the steering `Helm`
  (one shared heading PID = the "autopilot inner loop"), and translates UI
  commands. `modes.py` holds the four behaviours as pure strategies.
- **sim/** — `boat.py` physics, `devices.py` (`SimGps`/`SimCompass`/
  `SimMotorController`/`SimServo`, each implementing a HAL ABC), `simulator.py`
  ties them together and owns ground truth.
- **ui/** — FastAPI server: WebSocket telemetry + REST/WS commands + a Leaflet
  map. `app.py` wires a `Runtime` and runs every async loop.

### The closed loop

```
SimMotorController.command  ─▶  Boat.step(dt, command, env)         (truth)
        ▲                              │
        │                     SimGps / SimCompass .sample(truth)    (noisy NMEA)
        │                              │
   Helm + ControlMode                 ▼
        ▲                       Navigator.handle_sentence           (perceived)
        │                              │
        └────── Controller.control_tick(dt) ◀── NavigationState ◀───┘
```

Crucially the controller only ever reads the **perceived** state (from noisy
GPS/compass), exactly as it will with real hardware. Ground truth lives only in
the simulator.

### Control modes (the "functions")

Each mode is a pure strategy `update(state, dt) -> Setpoint`:

| Mode          | Output intent                                              |
|---------------|------------------------------------------------------------|
| Manual        | direct `(thrust, steering)`                                |
| Heading hold  | `GuidedSetpoint(target_heading, throttle)`                 |
| Anchor hold   | distance-PID thrust + heading = bearing back to the anchor |
| Waypoint      | heading = bearing to mark, biased by cross-track error     |

A **`GuidedSetpoint`** expresses *"point the boat this way"*; the **Helm** turns
it into a concrete steering command with a single shared, well-tuned heading
PID. So every guided behaviour reuses one autopilot loop instead of each
re-implementing steering. `ManualSetpoint` bypasses the helm.

### Why this is testable

- `control_tick(dt)`, `mode.update(...)`, `helm.compute(...)`,
  `boat.step(...)`, `navigator.handle_sentence(...)`, and the sensors'
  `sample(...)` are all **synchronous and deterministic**. The async runtime is
  a thin timer/event wrapper over them.
- `tests/harness.py` steps the entire system in lockstep with seeded noise and
  no wall-clock, so integration tests run in milliseconds and never flake.

### Swapping in real hardware (future)

Implement against `hardware/interfaces.py`:

- `SerialGps(Sensor)` / `SerialCompass(Sensor)` — read a serial port, push raw
  NMEA onto the bus (topic `nmea.in`). The navigator doesn't change.
- `SerialMotorController(MotorController)` — translate `MotorCommand.thrust` to
  ESC/PWM and `.steering` to a stepper/servo position (an `Actuator`) over the
  Arduino line protocol.

Then in `app.py`, construct those instead of the `Sim*` devices. Nothing in
`core/`, `nav/`, or `controller/` changes.
