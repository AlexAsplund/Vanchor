# Vanchor-NG firmware

Arduino firmware that lets the Raspberry Pi autopilot drive the boat's **engine
(thrust)** and **steering (azimuth)** hardware over USB serial. Two independent
sketches, one shared protocol header:

```
firmware/
├── common/vanchor_protocol.h   shared line protocol + CMD parser (the contract)
├── engine/engine.ino           thrust via a HIJACKED commercial speed controller + F/R relay
├── steering/steering.ino       closed-loop azimuth: worm gearmotor + H-bridge + feedback pot + PID
└── README.md                   this file (schematics, pin maps, wiring, BOM)
```

Target board: **Arduino Nano or Uno (ATmega328P, 5 V logic)** — one per function
(two Nanos recommended; they appear to the Pi as two USB serial ports). Standard
Arduino core only — **no external libraries required**.

---

## 1. Serial protocol (must stay plug-compatible with the Pi)

The Pi's hardware abstraction layer already defines this exactly in
`src/vanchor/hardware/serial_devices.py` (`SerialMotorController`) and
`src/vanchor/hardware/serial_link.py` (framing/baud). The firmware mirrors it.

| Aspect | Value |
|---|---|
| Electrical | USB serial (CDC), 8N1 ASCII |
| Baud | **4800** (`HardwareConfig.baudrate`, default). Change in both places together. |
| Line ending Pi→Arduino | `\r\n` (Arduino accepts `\n`, tolerates `\r`) |
| Line ending Arduino→Pi | `\r\n` |

### Command — Pi → Arduino (one per control tick)
```
CMD <pwm> <dir> <steer>\r\n
```
* `pwm`   `0..255`   thrust magnitude (`round(|thrust|*255)`)
* `dir`   `F`/`R`    forward / reverse (`R` if `thrust < 0`)
* `steer` `-100..100` steering (`round(steering*100)`; −100 hard port, +100 hard stbd)

A single `CMD` line carries **both** thrust and steering. The **engine** board
reads `pwm`+`dir` and ignores `steer`; the **steering** board reads `steer` and
ignores `pwm`+`dir`. The Pi opens a separate transport per device, so each board
gets its own `CMD` stream (or, on a shared bus, each filters the same line).

Examples: `CMD 0 F 0` (stop, centred) · `CMD 255 F 0` (full ahead) ·
`CMD 128 R -100` (half astern, hard port).

### Feedback — Arduino → Pi
Steering reports the **actual** head angle so the Pi's closed-loop telemetry
(`steering.angle_deg`, `feedback_ok`, `wrap_pct` in `app.py`) is real:
```
A <angle_deg> <ok> <wrap_pct>\r\n      e.g.  A -12.4 1 -7
```
Engine emits an optional debug ack:
```
E <pwm> <dir> <state>\r\n              state = RUN | SOFTSTART | REVDELAY | FAILSAFE
```

> **One small Pi-side change** is needed to *consume* the `A` feedback line:
> `SerialMotorController` currently only writes. Its read-loop should parse
> `A <angle> <ok> <wrap>` and set the steering telemetry fields. Until then the
> lines are harmless (the existing `_SerialNmeaSensor` would just republish them).
> Details in `docs/firmware.md`.

### Loss-of-signal failsafe
If no valid `CMD` arrives within `VANCHOR_WATCHDOG_MS` (800 ms):
* **engine** ramps throttle to neutral/stop (does not flip direction),
* **steering** *holds* the current angle (worm self-locks — safest; optional
  slow recentre is off by default).

This mirrors the Pi safety governor (`controller/safety.py`: slew limiting,
reverse delay, loss-of-fix).

---

## 2. Engine board — hijacked commercial speed controller

We **buy** a generic DC / trolling-motor PWM speed controller and **inject our
throttle** where its speed knob was, rather than build a high-current driver.
Forward/reverse is a **DPDT relay** that swaps motor-output polarity (switched
only at zero throttle — dead-time guard).

### Where to tap in
Typical buyable target: a *"6–60 V DC motor speed controller, PWM, with 10 kΩ
speed knob"* (or a trolling-motor variable-speed control). The knob is a 3-wire
potentiometer: **+5 V (or +Vref) / wiper / GND**. We replace the wiper.

Two hijack methods are implemented (`HIJACK_MODE` in `engine.ino`):

**(A) Digital potentiometer — RECOMMENDED.** An **X9C103** (10 kΩ, 100 taps,
INC/UD/CS) wired in place of the knob: top→Vref, bottom→GND, wiper→controller
throttle. The controller sees exactly the signal it expects; full 0–100 % range;
no analog drift. (MCP41010 over SPI is an equivalent alternative.)

**(B) PWM → RC low-pass → 0–5 V.** Arduino PWM pin → `1 kΩ + 10 µF` RC filter →
(optional MCP6002 op-amp buffer) → the controller's **0–5 V analog throttle**
input. Use this when the controller has an analog throttle input rather than a
bare knob. Buffer if the input loads the filter; trim `THROTTLE_MAX_PWM` if its
full-scale is < 5 V.

| | DIGIPOT (A) | PWM-DAC (B) |
|---|---|---|
| Needs | a pot-type knob input | a 0–5 V analog input |
| Range fidelity | exact, monotonic | depends on RC trim |
| Drift/ripple | none | RC ripple/settling |
| Wires | 3 (INC/UD/CS) | 1 (+ buffer parts) |
| Pick when | controller uses a knob | controller uses 0–5 V throttle |

### ASCII schematic (engine, DIGIPOT + relay)
```
  USB (to Pi)
     │
 ┌───┴────────────── Arduino Nano (5V) ───────────────┐
 │ D5 INC ─┐                                          │
 │ D6 U/D ─┤── X9C103 digital pot ── wiper ──► (speed-knob wiper IN)
 │ D7 CS  ─┘     Vcc=5V  GND=GND    └ Vh→Vref  Vl→GND  of bought controller
 │                                                    │
 │ D8 ──►[NPN/ULN2003]──► DPDT relay coil ── 12V      │   ┌──────────────┐
 │                         relay COM/NO/NC swaps  ────┼──►│  BOUGHT PWM   │
 │ D13 LED (status)        MOTOR+ / MOTOR-           │   │  speed ctrl   │──► MOTOR
 │ GND ───────── common ground (logic ⟷ ctrl) ──────┼──►│ (M+, M-, V+,  │
 └────────────────────────────────────────────────────┘  │  GND, knob)  │
                  flyback diode across relay coil          └──────────────┘
   Motor battery 12V ─[FUSE]─[KILL SWITCH]─► controller V+ ; battery GND = common GND
```
For PWM-DAC mode instead of the X9C: `D9 ──[1kΩ]──┬──► throttle-in ; └──[10µF]──GND`
(add MCP6002 voltage follower between the junction and throttle-in if needed).

### Pin map (engine)
| Arduino pin | Net | Component |
|---|---|---|
| USB | serial | Pi (CMD in / E ack out) |
| D5 | `INC`  | X9C103 increment (DIGIPOT mode) |
| D6 | `U/D`  | X9C103 up/down (DIGIPOT mode) |
| D7 | `CS`   | X9C103 chip-select (DIGIPOT mode) |
| D9 | `PWM`  | RC filter → throttle (PWM-DAC mode) |
| D8 | relay  | DPDT direction relay (via NPN/ULN2003 driver) |
| A1 | rev    | controller's own reverse input (if `USE_DIR_RELAY 0`) |
| D13 | LED   | status / failsafe |
| 5V / GND | power | logic 5 V; **GND common with controller & battery −** |

### Wiring list (engine)
1. Arduino 5 V/GND ↔ X9C103 Vcc/GND. X9C wiper → controller knob-wiper terminal;
   X9C Vh → knob-top (Vref), X9C Vl → knob-bottom (GND). (Remove/disconnect the
   original knob.)
2. D8 → base of an NPN (e.g. 2N2222 + 1 kΩ) or a ULN2003 channel → relay coil −;
   relay coil + → +12 V. **Flyback diode (1N4007) across the coil.**
3. DPDT relay COM = controller motor outputs; NO/NC cross-wire so energised =
   reversed polarity to the motor.
4. Motor battery + → **fuse** → **kill switch** → controller V+. Battery − →
   controller GND **and** Arduino GND (single common ground / star point).
5. Optional: 100 nF + 100 µF across the controller throttle input for noise.

---

## 3. Steering board — closed-loop azimuth

Worm-gear DC gearmotor (quiet, self-locking — the user rejected steppers) driven
through a **BTS7960** H-bridge; a **feedback potentiometer** on the turret/ring
gives the actual angle; a **PID** loop holds the target the Pi sends and reports
the measured angle back.

> The CAD prototype (`cad/steering_BOM.md`) uses an **AS5600 magnetic encoder**
> over I²C. This firmware uses the **potentiometer** variant. To switch to the
> AS5600, replace `readAngleDeg()` with an I²C read of the AS5600 raw angle
> (×360/4096, unwrapped) — the PID loop is otherwise identical.

H-bridge choice: **BTS7960** recommended (≈43 A peak, built-in current sense).
Alternatives noted: **DRV8871** (≤3.6 A, tiny, fewer pins) or **L298N** (cheap,
lossy, ~2 A) — both drop-in by re-mapping the two PWM + enable pins.

### ASCII schematic (steering, BTS7960 + feedback pot)
```
  USB (to Pi)
     │
 ┌───┴───────────── Arduino Nano (5V) ─────────────┐
 │ D9  RPWM ─────────────────► BTS7960 RPWM        │      ┌─────────┐
 │ D10 LPWM ─────────────────► BTS7960 LPWM        │  M+  │  WORM   │
 │ D4  R_EN ─┬───────────────► BTS7960 R_EN        ├─────►│ GEAR    │── pinion ─► ring/turret
 │ D7  L_EN ─┘ (tie together)► BTS7960 L_EN        │  M-  │ MOTOR   │            (steering head)
 │ A2  R_IS ◄───────────────── BTS7960 R_IS  (opt) │      └─────────┘                 │
 │ A3  L_IS ◄───────────────── BTS7960 L_IS  (opt) │                                   │
 │                                                 │   feedback POT (3-wire) on the ───┘
 │ A0  ◄──── pot wiper ◄───── 5V ─[POT]─ GND       │   turret/ring shaft
 │ D13 LED (status)                                │
 │ GND ─────────── common ground ──────────────────┤  BTS7960 VCC=5V(logic) GND=common
 └───────────────────────────────────────────────────┘  BTS7960 B+/B- = 12V motor batt
   Motor battery 12V ─[FUSE]─[KILL]─► BTS7960 B+ ; batt − = common GND
   (recommended: opto-isolate RPWM/LPWM/EN, or keep motor wiring away from A0/pot leads)
```

### Pin map (steering)
| Arduino pin | Net | Component |
|---|---|---|
| USB | serial | Pi (CMD in / `A` feedback out) |
| D9  | `RPWM` | BTS7960 RPWM (drive +angle / stbd) |
| D10 | `LPWM` | BTS7960 LPWM (drive −angle / port) |
| D4  | `R_EN` | BTS7960 enable (HIGH = run) |
| D7  | `L_EN` | BTS7960 enable (tie to R_EN) |
| A0  | wiper  | feedback potentiometer wiper |
| A2  | `R_IS` | BTS7960 current sense (optional, stall) |
| A3  | `L_IS` | BTS7960 current sense (optional, stall) |
| D13 | LED    | status (solid = feedback lost / stalled) |
| 5V / GND | power | logic + pot top/bottom; **common GND** |

### Wiring list (steering)
1. BTS7960 `RPWM/LPWM/R_EN/L_EN` → D9/D10/D4/D7. `VCC`=5 V (logic), `GND`=common.
   `B+`/`B-` = motor battery (fused). `M+`/`M-` = gearmotor.
2. Feedback pot: top → 5 V, bottom → GND, wiper → A0. Mount so its travel maps
   the head's `±185°` (use a **multi-turn pot** or gear the pot up — a single-turn
   ~300° pot can't cover ±185° directly).
3. **Calibrate** in `steering.ino`: set `ADC_AT_NEG` / `ADC_AT_POS` to the raw
   `analogRead()` values at the two mechanical extremes; the firmware maps
   linearly and flags `feedback_ok=0` outside `[ADC_MIN_VALID, ADC_MAX_VALID]`.
4. Motor battery + → **fuse** → **kill switch** → `B+`. Battery − = common GND.
5. Recommended: opto-isolate the PWM/EN lines (e.g. 6N137) and keep the pot/A0
   wiring twisted and away from the motor leads (the H-bridge is electrically
   noisy and the ADC is sensitive).

---

## 4. Combined BOM (concrete, buyable parts)

| Qty | Part | Notes / spec |
|---|---|---|
| 2 | Arduino Nano (ATmega328P) | one per board; 5 V logic, USB-serial to Pi |
| 1 | Commercial DC/trolling PWM speed controller | matched to motor V/A; 10 kΩ-knob or 0–5 V throttle input (the HIJACK target) |
| 1 | **X9C103** digital pot (10 kΩ) | hijack method A (recommended); or MCP41010 (SPI) |
| — | `1 kΩ` + `10 µF` + MCP6002 op-amp | hijack method B (PWM→RC→buffer), if used instead |
| 1 | DPDT relay (coil = system V, contacts ≥ motor current) | forward/reverse polarity swap |
| 1 | NPN (2N2222) + 1 kΩ, **or** ULN2003 | relay coil driver |
| 1 | **1N4007** flyback diode | across the relay coil (+ optional RC snubber on contacts) |
| 1 | **BTS7960** H-bridge module | steering gearmotor driver (DRV8871 / L298N alt.) |
| 1 | 12 V worm gearmotor (JGY-370 class, 10–50 RPM) | quiet, self-locking steering drive (per `cad/steering_BOM.md`) |
| 1 | **multi-turn potentiometer** (e.g. 10 kΩ, 3- or 10-turn) | steering feedback over ±185°; or AS5600 + magnet (CAD default) |
| 2 | inline fuse holders + fuses (sized to motor) | one per motor battery feed |
| 2 | kill switch / main disconnect | emergency stop, each motor circuit |
| — | 6N137 opto-isolators (recommended) | isolate PWM/EN on the noisy steering side |
| — | 100 nF + 100 µF decoupling caps | across throttle input / logic rails |
| — | PG7 gland, marine wire, ferrules | per CAD sealing notes |

---

## 5. Grounding, EMI, voltage & safety

* **Voltage domains:** Arduino + sensors are **5 V logic**; motors run on the
  **motor battery (12 V+)**. Never feed motor V into a logic pin. The X9C/pot
  wipers and the 0–5 V throttle stay within 0–5 V.
* **Common ground, separated power:** tie all grounds at **one star point**
  (battery −). Run logic power and motor power on separate conductors; don't
  share a thin ground with the motor return.
* **EMI:** the H-bridge and brushed motor are noisy. Twist motor leads, keep the
  feedback-pot/A0 wiring short, twisted, away from motor cabling, and
  **opto-isolate** the steering PWM/EN lines where practical. Decouple the
  throttle input.
* **Flyback/snubber:** diode across the relay coil; optional RC snubber across
  the DPDT contacts; the bought controller handles motor-side flyback.
* **Fusing & kill:** every motor battery feed gets an inline **fuse** sized to
  the motor and a **kill switch / disconnect**. The serial watchdog brings the
  hardware to a safe state if the Pi link drops.
* **Reverse interlock:** the DPDT relay only switches at zero throttle after the
  reverse dead-time, so it makes-before-breaks with the motor unpowered.

See `docs/firmware.md` for the protocol↔code mapping and the one Pi-side change.

---

## 6. Split firmware protocol (BENCH-VERIFY)

> **Status as of 2026-07-06:** No physical split boards exist yet. The Pi
> driver (`src/vanchor/hardware/serial_channels.py`) implements the protocols
> below; they must be bench-verified against actual firmware before deploying
> on a boat.

When a **modified Minn Kota head** (or other custom hardware) puts steering
and thrust on two separate Arduino boards, the Pi uses two independent serial
links. Each board gets its own command stream and sends its own feedback; the
Pi never interleaves commands across boards.

The framing (baud / parity / stop bits) is the same as the combined protocol
(8N1, default 4800 baud, `\r\n` line endings) unless overridden per channel in
the Pi config.

### 6.1 Steering-only board — `STEER` command

```
STEER <steer>\r\n
```

* `steer`  integer `-100..100` — steering angle (`round(normalized * 100)`;
  −100 hard port, +100 hard starboard, 0 centred).

Examples:

```
STEER 0          # centred
STEER 100        # hard starboard
STEER -50        # half port
```

Feedback: the board sends the standard `A <angle_deg> <ok> <wrap_pct>` line
(same format as the combined firmware — Section 1) at ~10 Hz.

Loss-of-signal failsafe (same 800 ms watchdog as the combined firmware): the
board holds the current angle when no `STEER` arrives within the window.

### 6.2 Thrust-only board — `THRUST` command

```
THRUST <pwm> <dir>\r\n
```

* `pwm`   integer `0..255` — thrust magnitude (`round(|normalized| * 255)`)
* `dir`   `F` or `R` — forward (`normalized ≥ 0`) or reverse

Examples:

```
THRUST 0 F       # stopped
THRUST 255 F     # full ahead
THRUST 128 R     # half astern
```

Feedback: the board sends the standard `E <pwm> <dir> <state>` line (same
format as the combined firmware — Section 1) at ~5 Hz.

Loss-of-signal failsafe: same 800 ms watchdog; engine ramps to neutral/stop
(does not flip direction on a watchdog trip).

### 6.3 Implementing the split firmware

A split steering board can be a fork of `firmware/steering/steering.ino` that
reads `STEER <steer>` instead of the `steer` field of `CMD`; it can discard the
`pwm` and `dir` fields entirely. A split thrust board is similarly a fork of
`firmware/engine/engine.ino` that reads `THRUST <pwm> <dir>` instead of `CMD`.
The shared protocol header (`firmware/common/vanchor_protocol.h`) will need
a new parser for each split command variant.
