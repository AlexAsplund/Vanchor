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
  `events.py` (async pub/sub `EventBus`). Also: `anchor_alarm.py` (passive
  motor-OFF GPS watch circle, adoption #10), `backup.py` (config/data
  backup+restore), `boat_profiles.py` (per-boat preset profiles),
  `capabilities.py` (device capability registry), `contract.py` (API contract
  types), `debug_recorder.py` (chunked gzip flight recorder, #20),
  `observability.py` (always-on blackbox ring buffer), `prefs.py`
  (server-side user preferences), `alertlog.py` (alarm history log).
- **nav/** — `nmea.py` parses *and* encodes RMC/GGA/HDM/HDT/APB with checksums
  (no pynmea2 dependency); `navigator.py` is the single writer of *perceived*
  position/heading from sentences.
- **hardware/** — `interfaces.py`: the HAL. `Sensor`, `MotorController`,
  `Actuator` ABCs. This is the seam between the controller and the physical
  world. Shipped additions: `probe.py` (passive serial hardware fingerprinting
  for the setup wizard), `i2c_link.py` (I²C transport for the helm-Pico motor
  controller tunnel), `registry.py` (device driver registry), `link_plan.py`
  (motor-link planner for split-motor configs), `serial_channels.py` /
  `serial_devices.py` / `serial_link.py` (serial HAL), `split_motor.py`
  (dual-channel motor support), `watchdog.py` (external GPIO hardware watchdog).
- **controller/** — `controller.py` owns the active mode, the steering `Helm`
  (one shared heading PID = the "autopilot inner loop"), and translates UI
  commands. `modes.py` holds the control behaviours as pure strategies.
  Also: `anchor_ml.py` (`AnchorMLMode` Smart hybrid + `AnchorLeifMode` pure
  learned station-keepers), `safety.py` (safety governor), `calibration.py`
  (auto-calibration runner), `estimator.py` (GNSS/INS fusion).
- **sim/** — `boat.py` physics, `devices.py` (`SimGps`/`SimCompass`/
  `SimMotorController`/`SimServo`, each implementing a HAL ABC), `simulator.py`
  ties them together and owns ground truth.
- **ui/** — FastAPI server: WebSocket telemetry + REST/WS commands + a Leaflet
  map. `app.py` wires a `Runtime` and runs every async loop.
- **Top-level app modules** (under `src/vanchor/`):
  - `push.py` — Web Push notification dispatch (adoption #7); server-initiated
    alarms to a locked phone.
  - `wifi.py` — nmcli-backed WiFi scan/join for the SD-image setup flow.
  - `discovery.py` — mDNS service advertisement (`vanchor.local`).
  - `supervisor_client.py` — stdlib-only HTTP client for the host-side
    `supervisor/` daemon (lifecycle management, OTA updates, disk monitor).
  - `tls.py` — self-signed TLS cert generation for the HTTPS listener.
- **`supervisor/` package** (top-level, Pi-host only) — runs *outside* the
  app container. Provides the update, rollback, backup, and disk-monitoring
  API that `supervisor_client.py` calls over localhost:9300. Key modules:
  `vanchor_supervisor/core.py`, `selfupdate.py`, `disk.py`, `backup.py`,
  `bundles.py`, `versionspec.py`.
- **UI static modules** (`src/vanchor/ui/static/`) — the shipped Evolution+
  rehaul added: `menu.js` (command-menu / category tiles), `views.js`
  (URL-addressable view system: chart / helm / instruments / manual),
  `mobile.js` (mobile sheet + landscape parity), `layout.js` (responsive
  layout engine), `safety.js` (governor advisory + anchor-alarm banner, incl.
  4 s dwell), `armbar.js` (arm-to-engage bar), `pinpopup.js` (map-tap
  popup), `wifi.js` (WiFi setup UI), `wizard.js` / `hwwizard.js` (setup +
  hardware wizards), `supervisor.js` (OTA/update UI), `push.js` (Web Push
  opt-in), `roles.js` (observer/operator role gate), `demo.js` (demo mode),
  `themectl.js` (daylight/dark theme toggle), among others. Leaflet, uPlot,
  and fonts are vendored under `static/vendor/` — no external CDN dependency
  at runtime.

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
