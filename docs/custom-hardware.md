# Custom hardware: independent Steering + Thrust channels

Vanchor's motor is modelled as two logical channels — **steering** (the azimuth
head) and **thrust** (the prop) — that normally live on one microcontroller (the
default rig: a single Arduino speaking the combined `CMD <pwm> <dir> <steer>*HH`
frame; see `firmware/README.md`). If your hardware isn't vanchor's own — say a
modified trolling-motor head with its own steering servo and a separate thrust
ESC — the channels can be sourced, configured, health-checked and debugged
**independently**.

## How the split works

- The control loop always emits one atomic command (thrust + steering) to a
  single motor seam — STOP and the deadman zero **both** channels through that
  one object no matter how the hardware is wired.
- At startup the channel configs are resolved **by physical link**: if both
  channels name the same serial port (or both are unset and follow the legacy
  `motor_*` settings), the ONE combined controller is built exactly as before.
  Only genuinely different endpoints construct the split composite.
- Same port with mismatched baud/framing is a **config error** (surfaced in the
  UI on save), never a silent pick. Aliased paths (`/dev/ttyUSB0` vs a
  `/dev/serial/by-id/...` symlink) are resolved to the same device.
- A channel that fails to build or open never crashes startup: the other channel
  keeps working, the failed one shows unhealthy in Settings → Devices, and modes
  that need it are disabled with the channel named ("Steering not connected").
- Declaring a channel **Not connected** while the other rides a shared combined
  board sends neutral (0) for the disabled field and gates the modes that need
  it.

## Configuring it

Settings → Devices → Motor → **Advanced: split channels**:

| Channel  | Source            | Port + framing |
|----------|-------------------|----------------|
| Steering | sim / serial / none | its own port, baud, bits/parity/stop |
| Thrust   | sim / serial / none | its own port, baud, bits/parity/stop |

Leave the advanced section untouched and the single **Motor** setting behaves
exactly as it always has (the legacy `motor_source`/`motor_port` config keeps
working unchanged). Note: configuring a channel switches that channel to its
*own* baud/framing (it stops inheriting the motor defaults).

## Recipe: modified trolling-motor head (e.g. a Minn Kota-style unit)

A common conversion keeps the OEM lower unit + prop but replaces the head with
vanchor's steering gearbox (see the CAD in the companion repo) or a custom servo
driver, while thrust is driven by a separate ESC/driver board:

1. **Steering board** — flash/keep a controller that accepts the line protocol
   `STEER <int -100..100>` (see "Split firmware protocol" in
   `firmware/README.md`; the existing steering feedback lines are understood for
   health/closed-loop display). Wire it to its own USB/UART port.
2. **Thrust board** — a controller accepting `THRUST <pwm 0..255> <dir F/R>`
   driving the ESC/H-bridge. Own port.
3. In the UI set Steering → serial + its port, Thrust → serial + its port, pick
   baud/framing per board, save. The same-port validation, per-channel debug
   streams (🐞), and per-channel health apply immediately.
4. Run the **interference calibration** afterwards — a relocated motor/servo
   changes the magnetic picture at the compass.

> **BENCH-VERIFY**: the split line protocols are defined and unit-tested against
> fakes, but no physical split board has been driven yet — verify on the bench
> before first water use, exactly like the combined firmware was.

## Driver packs

Both channel kinds participate in the #43 driver registry, so a pack can ship a
ready-made channel driver (e.g. a specific servo controller) that appears as a
selectable Steering/Thrust source without editing core.
