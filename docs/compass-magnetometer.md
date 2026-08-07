# I2C magnetometer compass

Vanchor can use the small 3-axis **magnetometer** found on combo GNSS boards
(Beitian **BN-880 / BE-880** and many drone/rover modules) or a standalone
magnetometer breakout, as a heading source. It **autodetects** which chip is
fitted, so you only pick "magnetometer" and point it at the I2C bus.

| | |
|---|---|
| **Source** | `compass_source: magnetometer` |
| **Chips** | HMC5883L, QMC5883L, IST8310 (autodetected by id register) |
| **Bus** | `compass_port: i2c:<bus>[:<addr>]` — address optional (autodetect) |
| **Output** | true heading (`HDT`), declination applied |
| **Needs** | the `i2c` extra: `pip install 'vanchor[i2c]'` |

> ⚠️ **Read the [tilt limitation](#tilt-a-bare-magnetometer-assumes-its-level)
> below before you rely on it.** A bare magnetometer is only accurate when it
> sits level.

## Wiring

The magnetometer is a **separate device from the GPS** even on a combo board: the
GNSS is serial (UART), the magnetometer is I2C. Wire its four I2C pins to the
Raspberry Pi:

| Module pin | Pi pin |
|---|---|
| GND | GND |
| VCC / 3V3 | 3V3 (check your board — many are 3.3 V only) |
| SDA | GPIO 2 (SDA1) |
| SCL | GPIO 3 (SCL1) |

Enable I2C on the Pi once: `sudo raspi-config` → **Interface Options → I2C →
Yes**, then reboot. The bus is then `/dev/i2c-1`.

## Enable it

**Easiest — the wizard.** **Settings → Devices & hardware → Guided hardware
setup…**, scan, and the wizard probes the known magnetometer addresses (0x0D /
0x1E / 0x0E). A detected chip is offered as a one-click compass; **Save &
restart**.

**Manual.** In **Settings → Devices**, set the compass source to
**magnetometer**. Leave the port as `i2c:1` to autodetect the address, or pin it
with `i2c:1:0x0d`. Save and restart.

## Calibrate it (do this — it won't read right otherwise)

A magnetometer is distorted by the metal and magnets around it (hard/soft iron).
Until you correct for that, the heading is simply wrong. The panel nudges you
until it's calibrated. It takes a minute:

1. **Settings → Devices → Compass**.
2. Press **Start calibration**.
3. **Turn the boat slowly through at least one full circle** (on the water, or
   spin the whole boat on land — the sensor must rotate with the hull).
4. Press **Finish calibration**. The fit is saved and survives restarts.

Re-calibrate if you move the sensor, or add/remove metal or electronics near it.

## Tilt: a bare magnetometer assumes it's level

**This is the one real limitation, so it's worth being clear about.** A plain
magnetometer measures the Earth's magnetic field but has **no way to sense
gravity**, so it cannot tell heading from tilt. Vanchor computes the heading from
the horizontal field **assuming the sensor is roughly level**.

The consequences:

- On a boat that **heels or pitches** (chop, a lean under way, weight to one
  side) the reported heading will **swing with the tilt** — sometimes by tens of
  degrees. It is steadiest on flat water with the sensor mounted level.
- There is **no tilt compensation** in this driver, because that requires an
  **accelerometer** to know "down". Adding one is not a setting — it's a
  different class of sensor.

What to do about it:

- **Mount the sensor level and low** (near the waterline, away from the motor,
  battery, and steel), and it's fine for anchor-hold and gentle trolling on
  reasonable water.
- **If your boat tilts a lot**, or you want heading you can trust while heeled,
  use a **fused AHRS** instead: the WitMotion **HWT901B**
  (`compass_source: hwt901b`) combines a magnetometer with an accelerometer and
  gyro and does the tilt compensation for you. The trade-off is a second device
  and a serial port; the bare magnetometer's advantage is that it's already on
  your GPS board.

## Options

Set these in **Settings → Devices → Compass**:

| Setting | What it does |
|---|---|
| **Declination** | `auto` learns declination + mount offset from GPS course (drive straight a while); `manual` = type your local declination; `off` = magnetic heading. |
| **Update rate** | Reads per second (default 5 Hz). |
| **Bow / Starboard axis** | Which sensor axis points forward / right, for how the board is mounted. Only needed if the heading is rotated or runs backwards; the `auto` declination corrects a constant offset on its own. |
| **Mirror correction** | Flip if the heading turns the *wrong way* as you turn. |

## Troubleshooting — "Dump raw I2C"

If it won't detect, or the heading looks wrong, use **Settings → Devices →
Compass → Dump raw I2C**. It reads the raw registers (the identity register and a
window of data registers, sampled a few times) at the known addresses and shows a
hex block you can **copy and paste into a GitHub issue**. That's usually enough to
tell apart a wrong chip-id, swapped bytes, or a dead / miswired sensor — without
anyone needing the board in hand. It works even when autodetection is failing.

Common fixes: enable I2C (`raspi-config`); check 3V3 (not 5 V) and SDA/SCL aren't
swapped; run `i2cdetect -y 1` to confirm the chip acknowledges at an address.

## Adding another chip

Support is table-driven. A new magnetometer is one `ChipSpec` entry in
`src/vanchor/nav/magnetometer.py`'s `CHIP_TABLE` (address, id register + expected
id, init writes, byte order, axis order); the driver **and** the setup-wizard
probe both read that table. See
[the developer guide](llms/device-drivers.md#second-worked-example-an-i2c-sensor-with-autodetection-magnetometerpy).
