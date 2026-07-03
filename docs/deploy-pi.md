# Deploying Vanchor-NG on a Raspberry Pi

A concrete, boat-ready deployment guide: wire the hardware, prepare the OS,
install from the pinned lockfile, and run Vanchor-NG as an autostarting systemd
service that serves the PWA to your phone over the boat's WiFi.

> **Sim-first, always.** Vanchor-NG **defaults to full simulation** — it runs
> with no motor, GPS, or a single wire attached. Get it running headless on the
> Pi in sim first (open the UI, drop an anchor), *then* enable hardware one
> device at a time. Real-hardware support mirrors the simulated devices but is
> far less exercised — treat it as experimental (see the README "Alpha status").

---

## 1. Hardware overview

Vanchor-NG runs on any single-board computer that can reach a motor + steering
driver and the sensors over serial. A Raspberry Pi 4 / 5 / Zero 2 W is plenty —
the whole control loop is a few numpy matrix multiplies at 5 Hz.

The Pi talks to four things, all over **serial** (USB-serial adapters or the
Pi's UART):

| Device | Talks | Notes |
|---|---|---|
| **GPS receiver** | NMEA-0183 in (RMC/GGA) | `gps_port`, **≥ 38400 baud** for 5 Hz (see below) |
| **Compass / AHRS** — WitMotion **HWT901B-TTL** | heading in | `compass_port`, 4800 baud (NMEA) or the `hwt901b` driver |
| **Motor + steering controller** (Arduino) | `CMD`/`A` line protocol | `motor_port`, 4800 baud |
| **Depth / sounder** (optional) | NMEA DPT/DBT | via `nmea` source or the NMEA-over-TCP bridge |

### Motor & steering — the firmware boundary

The Pi does **not** drive high current directly. It sends a normalized
`MotorCommand` (`thrust ∈ [-1,1]`, `steering ∈ [-1,1]`) over serial to the
Arduino firmware in [`firmware/`](../firmware/README.md), which turns it into
real motion. One ASCII line per control tick carries both:

```
Pi → Arduino:   CMD <pwm 0..255> <dir F/R> <steer -100..100>\r\n
Arduino → Pi:   A <angle_deg> <ok 1/0> <wrap_pct>\r\n      (steering feedback)
```

- **Engine board** (`firmware/engine/engine.ino`) — thrust via a *hijacked*
  commercial speed controller (X9C103 digital pot in place of the speed knob,
  recommended) plus a **DPDT forward/reverse relay** switched only at zero
  throttle.
- **Steering board** (`firmware/steering/steering.ino`) — closed-loop azimuth: a
  worm gearmotor via a **BTS7960 / BTS7960 H-bridge**, a **feedback
  potentiometer** → ADC → angle, a PID holding the Pi's target, soft endstops at
  the ±185° cable-wrap limit, and it reports the **actual** angle back on the `A`
  line.

Full schematics, pin maps, wiring and BOM live in
[`firmware/README.md`](../firmware/README.md); the software contract (protocol,
baud, mapping, feedback) is in [`docs/firmware.md`](firmware.md).

> **Safety floor — the hardware deadman.** The firmware runs its own watchdog:
> if no valid `CMD` arrives within **800 ms**, the engine ramps to neutral/stop
> and steering holds (the worm self-locks). This mirrors the Pi's safety
> governor (`controller/safety.py`) so **STOP survives even a Pi crash or a USB
> unplug**. Keep it: fuse and kill-switch each motor battery feed, and test the
> watchdog (unplug USB, confirm the motor goes safe) during bring-up.

### The vanchor-pcb option

If you'd rather not design the helm wiring, the companion
[vanchor-pcb](https://github.com/AlexAsplund/vanchor-pcb) is an open-hardware
carrier (Orange Pi Zero 3 / Raspberry Pi + a Pi Pico 2 real-time motor
controller, servo bridge, thrust-driver board, HWT901B + GPS headers). It is
**prototype-stage** — review it before ordering. It is not required; any Pi +
the Arduino firmware works.

---

## 2. OS preparation

Use **Raspberry Pi OS (64-bit, Bookworm or newer)** — the pinned wheels in
`requirements.lock` are aarch64 manylinux, so nothing compiles on the Pi.

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3 python3-venv python3-pip git
```

Requires **Python ≥ 3.11** (`python3 --version`). Bookworm ships 3.11; that is
fine.

### Serial ports

Real devices appear as `/dev/ttyUSB0`, `/dev/ttyUSB1`, … (USB adapters) or
`/dev/ttyAMA0` / `/dev/serial0` (the Pi's own UART). Add your service user to
the `dialout` group so it may open them without root:

```bash
sudo usermod -aG dialout $USER      # log out/in (or reboot) for it to take effect
```

USB port order is not stable across reboots. For a fixed wiring, pin each device
with a **udev rule** by its adapter serial/vendor so `motor_port` always points
at the same physical cable:

```bash
# /etc/udev/rules.d/99-vanchor.rules  (example — match your adapter's attrs)
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{serial}=="ABC123", SYMLINK+="vanchor-motor"
```

Then set `motor_port: /dev/vanchor-motor` in the config.

> **GPS baud.** Set `gps_baud ≥ 38400` for a 5 Hz GPS. At 4800 baud two
> sentences per fix already exceed link capacity, the OS RX buffer fills, and
> fixes arrive stale within seconds (`hardware.gps_baud` default is 38400 for
> exactly this reason). `compass_baud` / `motor_baud` stay at 4800.

---

## 3. Install Vanchor-NG from the lockfile

Deploy under a service user (here `pi`) in `/opt/vanchor` (or a home dir):

```bash
sudo mkdir -p /opt/vanchor && sudo chown $USER /opt/vanchor
git clone https://github.com/AlexAsplund/vanchor-ng /opt/vanchor
cd /opt/vanchor

python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip

# Pinned, reproducible runtime install (core + water routing + serial):
pip install -r requirements.lock
# Install the vanchor package itself WITHOUT letting pip re-resolve deps —
# the lock owns the versions:
pip install -e . --no-deps
```

This installs the `vanchor` console script. Everything in `requirements.lock`
ships as a prebuilt aarch64 wheel, so the install is fast and offline-friendly
once the wheels are cached. See [`requirements.lock`](../requirements.lock) for
what is pinned and how to regenerate it.

**Optional — HWT901B compass driver.** Not on PyPI yet; install the sibling
checkout when you use `compass_source: hwt901b`:

```bash
pip install -e ../python-hwt901b-ttl[serial]
```

### Configure

Copy the example config and the environment file, then edit:

```bash
cp vanchor.example.yaml vanchor.yaml
cp .env.example .env
```

Key settings for a real boat (in `vanchor.yaml` or as `VANCHOR_*` env vars — the
environment wins over the file):

```yaml
server:
  host: 0.0.0.0        # serve to phones on the boat WiFi (not just loopback)
  port: 8000
hardware:
  enabled: true        # master switch: use real serial devices
  gps_port: /dev/ttyUSB0
  compass_port: /dev/ttyUSB1
  motor_port: /dev/ttyUSB2
  gps_baud: 38400
```

Per-device sources (`gps_source` / `compass_source` / `depth_source` /
`motor_source` = `sim` | `serial` | `nmea`; motor also `both`) let you enable
one real device at a time while the rest stay simulated — e.g. bench-test the
steering servo (`motor_source: serial`) against an otherwise fully simulated
autopilot. See [`.env.example`](../.env.example) and
[`vanchor.example.yaml`](../vanchor.example.yaml) for every key.

> **Working directory ⇒ data directory.** `vanchor_data/` (boat profiles, depth
> map, cached charts) is resolved **relative to the process cwd**. The systemd
> unit below sets `WorkingDirectory=` so the data dir is stable; if you run by
> hand, start from the repo root.

Smoke-test it by hand before making it a service:

```bash
cd /opt/vanchor
.venv/bin/vanchor --config vanchor.yaml --host 0.0.0.0 --port 8000
# open http://<pi-ip>:8000 from a phone on the same WiFi
```

---

## 4. The host guard (`VANCHOR_ALLOWED_HOSTS`)

Vanchor-NG rejects requests whose `Host` header is not one it trusts
(DNS-rebinding protection, `_HostCheckMiddleware`). **Accepted automatically:**

- any **IP literal** (v4/v6) — e.g. `http://192.168.1.50:8000`,
- **`localhost`**,
- a **bare single-label** name with no dot — e.g. `http://vanchor:8000`,
- names under a **private-LAN suffix** — `.local` (mDNS), `.lan`, `.home`,
  `.internal`, `.localdomain`.

So reaching the Pi by its IP, by `vanchor-pi.local`, or by a bare hostname works
with no configuration. You only need `VANCHOR_ALLOWED_HOSTS` (comma-separated)
if you front it with a **custom domain / reverse proxy** whose hostname has a
dot and a public-looking suffix:

```bash
# .env or the systemd Environment= line:
VANCHOR_ALLOWED_HOSTS=boat.example.com,vanchor.mydomain.net
```

An unlisted public-looking `Host` gets a `400 Host not allowed`.

---

## 5. Plain HTTP on the LAN vs HTTPS (Wake Lock)

For a boat LAN, **plain HTTP is the normal, expected setup** — the Pi serves
`http://<pi-ip>:8000` to your phone over WiFi and everything works: map, control,
STOP, WebSocket telemetry, PWA install, offline.

The **one** browser feature that needs a *secure context* is the **Screen Wake
Lock** (`wakelock.js`), which keeps the phone screen awake while a motor mode is
active. Over plain HTTP it **silently no-ops** — the app is fully functional, the
screen just may sleep. Two ways to get a secure context if you want the wake lock:

1. **`localhost` is already secure** — a phone tethered/tunnelled to reach the Pi
   as `localhost` gets it for free (rarely practical on a boat).
2. **Terminate HTTPS in front of Vanchor-NG.** Put a reverse proxy (Caddy /
   nginx) on the Pi with a self-signed or internal-CA cert and proxy to
   `127.0.0.1:8000`. Add the proxy's hostname to `VANCHOR_ALLOWED_HOSTS` (a
   dotted public-looking name won't pass the guard otherwise), and make sure it
   forwards the WebSocket upgrade for `/ws`.

Vanchor-NG itself serves plain HTTP; TLS, if you want it, lives in the proxy.
Not having HTTPS costs you only the screen-stay-awake convenience.

---

## 6. Run as a systemd service (autostart)

Create `/etc/systemd/system/vanchor.service` (adjust `User`, paths):

```ini
[Unit]
Description=Vanchor-NG trolling-motor autopilot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
Group=pi
WorkingDirectory=/opt/vanchor
Environment=VANCHOR_HOST=0.0.0.0
Environment=VANCHOR_PORT=8000
# Only needed for a custom reverse-proxy hostname (see section 4):
# Environment=VANCHOR_ALLOWED_HOSTS=boat.example.com
ExecStart=/opt/vanchor/.venv/bin/vanchor --config /opt/vanchor/vanchor.yaml
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Enable and start it (autostarts on every boot):

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vanchor.service
systemctl status vanchor.service
journalctl -u vanchor.service -f      # follow logs
```

The service reads config from `--config` (and `.env` in `WorkingDirectory`); CLI
flags and `VANCHOR_*` env vars override the file. Reachable device-config edits
made through the UI persist to `vanchor_data/devices.json` and apply on the next
restart (`sudo systemctl restart vanchor`).

> **Restart safely.** A lingering old process silently serves stale code and can
> rewrite `boats.json`. Prefer `systemctl restart vanchor`; if you started one by
> hand on port 8000, `fuser -k 8000/tcp` then confirm `ss -ltnp | grep :8000`.

---

## 7. Updating

```bash
cd /opt/vanchor
sudo systemctl stop vanchor
git pull
. .venv/bin/activate
pip install -r requirements.lock          # picks up any dependency bumps
pip install -e . --no-deps                # refresh the package/entry point
sudo systemctl start vanchor
```

If a UI update changed shell assets, the **network-first** service worker may
serve cached assets until the phone reloads once or twice (or the installed PWA
is reopened) — tell users to reload twice after an update. Back up persistent
state first with the in-app **backup / restore** (one ZIP of `vanchor_data/`)
before a big jump.

---

## See also

- [`firmware/README.md`](../firmware/README.md) — schematics, pin maps, BOM
- [`docs/firmware.md`](firmware.md) — the Pi ↔ Arduino software contract
- [`docs/safety-matrix.md`](safety-matrix.md) — failure-mode × layer × test matrix
- [`.env.example`](../.env.example) / [`vanchor.example.yaml`](../vanchor.example.yaml) — every config key
- [`requirements.lock`](../requirements.lock) — the pinned install set
