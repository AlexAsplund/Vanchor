# HotRC → Vanchor RF Remote Translator

An Arduino sketch that bridges a **HotRC** (or any standard RC PWM receiver) to
the Vanchor-NG **`rf-remote` connector**. It reads PWM pulse widths from the
receiver's channels and emits the text protocol the Pi expects over USB serial.

This is a **dumb translator** — all motor safety (deadman switch, control
grants, slew limiting) lives in the Pi's `rf-remote` connector and the governed
command path. This board never touches the motor bus directly.

---

## Why?

The existing `rf-remote` connector expects a line protocol over serial. A HotRC
(or FlySky, Radiolink, etc.) receiver outputs raw PWM pulses. This Arduino
sits between them:

```
HotRC Transmitter (in your hand)
    │ 2.4 GHz
    ▼
HotRC Receiver (on the boat)
    │ PWM channels
    ▼
Arduino Nano (this sketch)
    │ USB serial "STICK 0.5 -0.3\r\n" / "BTN ANCHOR\r\n"
    ▼
Raspberry Pi (vanchor --hardware)
    │ rf-remote connector reads this
    ▼
Motor controller(s) via governed path
```

## Channel mapping

| Receiver channel | Arduino pin | Function |
|---|---|---|
| CH1 (steering) | D3 (INT1) | Steering left/right [-1, 1] |
| CH2 (thrust)   | D2 (INT0) | Thrust forward/reverse [-1, 1] |
| CH3 (switch)   | D4         | Mode: STOP / MANUAL / ANCHOR |

CH3 is typically a 2- or 3-position switch on the transmitter:

| Switch position | Pulse width | Vanchor command |
|---|---|---|
| Low (~1000 µs)  | < 1250 µs  | `BTN STOP` — emergency stop |
| Mid (~1500 µs)  | 1250–1750 µs | `BTN MANUAL` — sticks control the motor |
| High (~2000 µs) | > 1750 µs  | `BTN ANCHOR` — hold current GPS position |

## Protocol output

Exactly what `src/vanchor/connectors/rf_remote.py` expects:

```
STICK <thrust> <steer>\r\n   — 20 Hz while sticks are off-centre
BTN STOP\r\n                 — CH3 low, or signal lost
BTN MANUAL\r\n               — CH3 mid (sticks active)
BTN ANCHOR\r\n               — CH3 high (engage position hold)
PING\r\n                     — keep-alive when sticks centred
```

## Wiring

```
                    HotRC 6-ch Receiver
                    ┌─────────────────┐
         CH1 sig ──┤ CH1              │
         CH2 sig ──┤ CH2              │
         CH3 sig ──┤ CH3              │   Receiver powered from
              GND ─┤ GND      VCC ────┤── Pi 5V (via Arduino)
                    └─────────────────┘   or receiver's own BEC

     Arduino Nano
     ┌───────────────────────────────┐
     │ D2 ◄──── CH2 sig (thrust)    │
     │ D3 ◄──── CH1 sig (steering)  │
     │ D4 ◄──── CH3 sig (mode sw)   │
     │ GND ──── receiver GND        │
     │ 5V ───── receiver VCC (if no BEC) │
     │ USB ───► Pi USB port          │
     └───────────────────────────────┘
```

**Important:** Use D2 and D3 for thrust/steering because they support hardware
interrupts (INT0/INT1) on ATmega328P boards. CH3 uses `pulseIn()` which is
adequate for the infrequent mode switch reads.

## BOM (additional to the main Vanchor build)

| Qty | Part | Notes |
|---|---|---|
| 1 | Arduino Nano (ATmega328P) | translator board |
| 1 | HotRC 2.4 GHz transmitter + receiver | e.g. [HotRC DS-4A](https://a.aliexpress.com/_m0PQPBN) |
| 3 | Dupont jumper wires (M-F) | receiver signal → Arduino pins |
| 1 | USB cable (Nano to Pi) | powers the Nano + serial data |

Total added cost: ~$25-30 USD.

## Pi-side configuration

Enable the `rf-remote` connector in your Vanchor config (YAML):

```yaml
connectors:
  rf-remote:
    enabled: true
    port: /dev/ttyUSB2       # adjust to the translator Nano's port
    baudrate: 115200
    expiry_s: 1.0            # deadman timeout (seconds of radio silence)
```

Or use Settings → Devices in the web UI to configure it.

## Calibration

1. **Stick range:** If your transmitter doesn't output exactly 1000–2000 µs,
   adjust `PWM_MIN`, `PWM_CENTRE`, `PWM_MAX` in the sketch.
2. **Deadzone:** Increase `PWM_DEADZONE` (default 40 µs) if sticks jitter near
   centre.
3. **Mode thresholds:** If your CH3 switch pulse widths differ, adjust
   `MODE_STOP_THRESH` and `MODE_ANCHOR_THRESH`.
4. **Channel swap:** If your transmitter's CH1/CH2 are reversed, swap
   `PIN_CH_THRUST` and `PIN_CH_STEER`.

## Testing

1. Upload the sketch to an Arduino Nano.
2. Open the Arduino Serial Monitor at 115200 baud.
3. Move sticks — you should see `STICK 0.45 -0.20` lines at 20 Hz.
4. Flip the mode switch — you should see `BTN ANCHOR` / `BTN MANUAL` / `BTN STOP`.
5. Turn off the transmitter — you should see `BTN STOP` (signal loss).

Once verified, plug the Nano into the Pi's USB and configure the `rf-remote`
connector port.

## Safety notes

- **Signal loss = STOP.** If the receiver loses the transmitter signal, the
  translator immediately sends `BTN STOP`.
- **Deadman on the Pi side.** Even if this translator malfunctions, the
  Pi's `rf-remote` connector has its own 1-second deadman that will STOP the
  boat if STICK updates cease.
- **STOP always wins.** The `BTN STOP` command bypasses the control grant
  system — it is always accepted regardless of which connector has the grant.
- **No direct motor access.** This board only talks to the Pi. It cannot drive
  the motor even if it wanted to.
