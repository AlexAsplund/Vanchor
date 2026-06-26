# Firmware integration (Pi ↔ Arduino)

How the Arduino firmware in [`firmware/`](../firmware/README.md) plugs into the
Vanchor-NG Python app. Full schematics, pin maps, wiring and BOM live in
`firmware/README.md`; this page is the **software contract** and the one change
the Pi needs to read steering feedback.

## What the firmware is

Two Arduino sketches that turn the autopilot's normalized `MotorCommand`
(`thrust ∈ [-1,1]`, `steering ∈ [-1,1]`) into real motion:

* `firmware/engine/engine.ino` — thrust via a **hijacked commercial speed
  controller** (digital pot replacing the speed knob, recommended; or PWM→RC→0–5 V)
  plus a **DPDT forward/reverse relay**. Soft-start slew, reverse dead-time,
  serial watchdog → neutral.
* `firmware/steering/steering.ino` — **closed-loop azimuth**: worm gearmotor via
  a **BTS7960 H-bridge**, a **feedback potentiometer** → ADC → angle, a **PID**
  loop holding the Pi's target, soft endstops at the `±185°` cable-wrap limit,
  deadband, stall detection, and **actual-angle feedback** back to the Pi.

## The protocol it speaks

Taken verbatim from `src/vanchor/hardware/serial_devices.py`
(`SerialMotorController`) and `src/vanchor/hardware/serial_link.py`:

| Direction | Line | Fields |
|---|---|---|
| Pi → Arduino | `CMD <pwm> <dir> <steer>\r\n` | `pwm 0..255`, `dir F/R`, `steer -100..100` |
| Arduino → Pi (steering) | `A <angle_deg> <ok> <wrap_pct>\r\n` | actual angle, feedback healthy 1/0, cable-wrap % |
| Arduino → Pi (engine, debug) | `E <pwm> <dir> <state>\r\n` | applied state ack |

* **Baud 4800** = `HardwareConfig.baudrate`. Keep both in sync.
* Mapping (already in `SerialMotorController`): `pwm = round(|thrust|*255)`,
  `dir = R if thrust<0 else F`, `steer = round(steering*100)`.
* Steering target angle on the Arduino: `target_deg = (steer/100) * steer_range_deg`
  with `steer_range_deg = 185.0` (`BoatConfig`).

## Steering feedback is consumed Pi-side (#83, done)

`SerialMotorController` now **reads** the `A <angle_deg> <ok> <wrap_pct>`
feedback line off the same transport it writes `CMD` to, so the closed-loop
steering telemetry (`steering.angle_deg`, `feedback_ok`, `wrap_pct`) reflects
the real measured azimuth on hardware. The path:

* `SerialMotorController.start()` launches a `_read_feedback()` task that reads
  lines from `self.transport`, parses each with
  `parse_steering_feedback()` (lenient: malformed/partial/`E`/`CMD` lines are
  ignored), and stores the latest `SteeringFeedback(angle_deg, ok, wrap_pct)` on
  `self.last_feedback`. The loop never raises out on noisy serial.
* `VanchorApp._build_telemetry` reads
  `getattr(self.controller.motor, "last_feedback", None)`; when present it
  overrides `steering.angle_deg` / `wrap_pct` / `feedback_ok` with the real
  feedback. The simulator's motor controller has no `last_feedback` attribute,
  so the **sim path is completely unaffected** (it keeps the modelled values).

No firmware change is needed — the firmware already emits `A` at ~10 Hz. See
`tests/test_serial_feedback.py` for the parse + integration coverage.

## Failsafes (mirror `controller/safety.py`)

* **Slew / soft-start** — engine throttle change capped at `1.0/s`
  (`max_thrust_slew_per_s`).
* **Reverse dead-time** — direction flip blocked until throttle has rested at ~0
  for `1.0 s` (`reverse_delay_s`); the relay switches unpowered.
* **Loss of signal** — no `CMD` within `800 ms`: engine → stop/neutral, steering
  → hold (worm self-locks). Matches the governor's loss-of-fix behaviour.
* **Feedback loss** — implausible pot reading → steering holds and reports
  `feedback_ok=0` (never drives blind).

## Bring-up checklist

1. Flash both sketches (Arduino IDE / `arduino-cli`, board = Nano/Uno). No
   libraries needed.
2. Set `HardwareConfig.enabled = true` and the three serial ports in the Pi
   config; confirm baud = 4800 on both sides.
3. Engine: pick `HIJACK_MODE` (default `HIJACK_DIGIPOT`); verify throttle ramps
   and the relay only switches at zero throttle.
4. Steering: **calibrate** `ADC_AT_NEG` / `ADC_AT_POS` to the raw `analogRead()`
   at the mechanical extremes, then tune `KP/KI/KD` and `DEADBAND_DEG` on the
   bench; confirm `A` lines report the angle the HUD expects.
5. Fuse + kill-switch each motor battery feed; test the watchdog by unplugging
   USB and confirming safe-state.
