# Hardware setup wizard

The hardware setup wizard guides you through identifying and configuring your
physical devices — GPS receiver, compass/AHRS, and motor controller — in about
two minutes, with wiring help included.

## Opening the wizard

In the app: **Settings → Devices & hardware → Guided hardware setup…**

The button is always visible in the Devices panel. In demo mode (`--demo`) or
if no hardware is present, the scan returns an empty result gracefully.

## What it does — step by step

### Step 0 — Scan

The wizard calls `GET /api/hw/scan` and lists every serial port and I²C bus the
Pi can see. Each port shows its OS description and a device hint where the
hardware can be identified from USB vendor/product strings (e.g. `u-blox GNSS
receiver`).

### Step 1 — GPS

Pick the port your GPS receiver is plugged into and click **Probe** (fires
`POST /api/hw/probe`). The server opens the port (read-only, 2.5 s) and
classifies what it hears:

| Detected | Confidence | What it means |
|---|---|---|
| `ublox` | high | UBX binary frames seen |
| `nmea-gps` | high/medium | GGA/RMC sentences |
| `unknown` | none | Nothing recognised — try another baud |

Enable **Deep-identify u-blox** to send a `MON-VER` poll and surface the
receiver's firmware version, hardware, and protocol string. This is a
read-only identify query; it does not change any receiver settings.

The wizard suggests the right `gps_source` and `baudrate` for the detected
receiver type.

### Step 2 — Compass

Works the same as GPS. The probe listens for WitMotion 11-byte IMU frames (HWT901B-TTL)
or NMEA HDG/HDT sentences. WitMotion at 9600 baud is the default; 115200 is
tried second.

### Step 3 — Motor

Pick the motor controller port (serial USB at 115200 or I²C at bus:addr) and
probe it. The probe uses a two-path strategy:

1. **INFO command** (preferred) — sends `INFO*<CRC>\r\n` to the helm board
   firmware and reads back structured identify lines:
   ```
   I fw v1.2-3 board helm-4.2 mcu pico2
   I proto 2.1 crc 1 wdog 800
   I up 7423 vbat 12.6 ang -3.2 fb 1
   I end 4
   ```
   Fields `fw`, `board`, `mcu`, `proto`, `vbat`, `ang`, and `fb` are surfaced
   in the wizard result.

   **BENCH-VERIFY:** the INFO command was added to the helm board firmware on
   2026-07-18 and has not yet been verified on real hardware. The fallback path
   is verified.

2. **A/E passive fallback** — if the firmware does not answer INFO (older
   boards), the probe passively listens for the `A` (angle/state) and `E`
   (encoder) feedback lines that all boards emit continuously.

**Safety guarantee:** the probe never sends `CMD`, `STEERD`, or `THRUST` —
nothing that can actuate the motor. The INFO command is a read-only firmware
query. The motor deadman, dual-path STOP, and link-loss failsafes are
completely unaffected.

### Step 4 — Finish

A review table shows what was detected for each device and what config change
will be applied. Click **Save & restart** to write the settings and trigger a
server restart. The page reloads automatically after ~8 seconds.

If you want to apply a different configuration than what was detected, click
**Back**, check **Use this port anyway (override mismatch warning)**, and
proceed.

## Conflict protection

If a port is already owned by a running driver (GPS, compass, or motor is
actively reading from it), the probe returns HTTP 409 and explains which driver
owns it. Stop the driver (or switch to sim mode) before probing that port.

Parallel probes are prevented by a server-side asyncio lock — a second probe
request while one is in progress also returns 409.

## I²C probing

Only two named addresses are probed, never a bus-wide sweep:

| Address | Kind | What is checked |
|---|---|---|
| 0x42 | helm-Pico | WHOAMI register returns 0x42 |
| 0x40–0x4F | INA226 | Manufacturer ID register (0xFE = 0x5449 = "TI") |

## Offline / demo mode

In `--demo` mode `hw_scan()` returns empty lists for ports and buses, and the
capabilities dict shows `serial: false, i2c: false`. The wizard UI handles this
gracefully (shows a "no hardware detected" hint). The hw endpoints are not on
the demo-readonly allowlist, so a read-only demo client cannot call them.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Permission denied" on probe | Add user to `dialout` group: `sudo usermod -a -G dialout $USER` then re-login |
| Port not in list | Replug the USB device and click **🔄 Scan** |
| Motor probe returns `unknown` | Check the USB cable; confirm the firmware is flashed; try 115200 baud |
| INFO command times out | Old firmware — probe still works via A/E passive fallback |
| 409 conflict | A driver is using the port — switch to Simulation mode in Devices first |
