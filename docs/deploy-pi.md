# Deploying Vanchor-NG on a Raspberry Pi

A concrete, boat-ready deployment guide. The recommended path is the
**pre-built SD image** — flash it, power on, connect to the hotspot, and the
UI is live without installing anything. The bare-metal venv path is preserved
in [Appendix A](#appendix-a-bare-metal-venv-install) for advanced users.

> **Sim-first, always.** Vanchor-NG **defaults to full simulation** — it runs
> with no motor, GPS, or a single wire attached. Get it running in sim first
> (open the UI, drop an anchor), *then* enable hardware one device at a time.
> Real-hardware support mirrors the simulated devices but is far less exercised
> — treat it as experimental (see the README "Alpha status").

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

## 2. Flash the SD image (recommended)

The release SD image contains the complete docker stack (app + supervisor),
hotspot first-boot, and no moving parts. The **Pi never needs internet** —
updates arrive as a bundle you download on your phone/laptop and upload in
Settings.

### What the image contains

- Raspberry Pi OS Lite arm64 (Bookworm, 64-bit) — stages 0–2 + our custom stage
- Docker CE + docker-compose plugin
- The `vanchor/vanchor` docker image pre-loaded (no first-boot pull)
- The `vanchor-supervisor` host daemon (update / backup / disk / device policy)
- NetworkManager hotspot (`vanchor-setup`) for initial phone connection
- SD-write minimisation: volatile journald, tmpfs `/tmp`+`/var/log`, noatime,
  bounded docker logs (`local` driver, max 5 MB × 2), zram swap

### Flash with Raspberry Pi Imager

1. Open [Raspberry Pi Imager](https://www.raspberrypi.com/software/) (≥ v1.8).
2. Click **OS → Use custom → Provide URL**, paste:
   ```
   https://github.com/AlexAsplund/vanchor-ng/releases/latest/download/os_list.json
   ```
   Select **Vanchor-NG** from the list.
3. *(Optional)* Click the gear ⚙ to pre-configure hostname / user / SSH / WiFi.
4. Select your 16 GB (or larger) SD card and flash.

### First boot (no ethernet, no WiFi configured)

1. Insert the card and power on. The root partition auto-expands (one automatic
   reboot is normal and expected).
2. `vanchor-load-images` runs in the background (~30–60 s on Pi 4, once only).
3. The docker stack comes up. Total time to a responsive UI: ≤ ~3 min.
4. The setup hotspot **`vanchor-setup`** appears (WPA2 password `vanchor-boat`).
   Connect your phone to it.
5. Open **`http://10.42.0.1:8000`** (or **`http://vanchor.local:8000`**) — the UI
   loads.

### Join your home WiFi

In the UI → Settings → "Data & system" → **WiFi & network** card:
1. Click **Scan for networks** — your home network appears.
2. Click it, enter the password → the join runs in the background.
3. The hotspot drops while joining (single radio). Reconnect your phone to your
   home WiFi, then open **`http://vanchor.local:8000`** (avahi mDNS on the LAN).
4. If the wrong password was entered, the hotspot returns automatically after
   ~60 s.
5. On subsequent reboots the Pi joins your home WiFi directly (autoconnect
   priority: saved home profiles 0 > hotspot -10). The hotspot only returns when
   no known network is reachable.

### Default credentials

| Item | Value |
|---|---|
| Hotspot SSID | `vanchor-setup` |
| Hotspot password | `vanchor-boat` |
| Console login | `vanchor` / `vanchor` (change with `passwd`) |
| SSH | **Disabled by default.** Enable: touch a file named `ssh` on the boot partition and reboot. |

> **Security posture.** The Pi is LAN-only (SSH off, hotspot WPA2, no open
> ports beyond :8000). The default password is documented — **change it** with
> `passwd` if the boat is ever reachable from untrusted networks.

> **WiFi PSK note.** When you join a home network via the UI, Vanchor passes
> the WiFi password to `nmcli device wifi connect` as a command-line argument.
> `nmcli` does not provide a stdin or file-descriptor interface for the
> password — argv is the only available option. The PSK is briefly visible in
> `/proc/<pid>/cmdline` and `ps` output for the lifetime of the `nmcli`
> process (typically under one second). The password is **never written to
> disk** by Vanchor and is **never logged** (sanitised before any log
> statement). On a LAN-only headless boat computer this is an acceptable
> trade-off, but be aware if your threat model includes local-user access to
> the Pi while a WiFi join is in progress.

---

## 3. Updates, backups, and disk

### Sideload updates (recommended — no Pi internet required)

On your phone or laptop:
1. Go to **[GitHub Releases](https://github.com/AlexAsplund/vanchor-ng/releases/latest)**
   and download `vanchor-update-<version>.bundle.tar`.
2. In the Vanchor UI → Settings → **System & updates** → "Sideload update bundle"
   → upload the bundle.
3. The supervisor verifies the checksum, loads the new image, recreates the
   container, health-gates (polls `/api/state`), and auto-rolls back to the
   previous image if the health check fails within 60 s. You never brick the Pi.

> **What the sha256 protects — and what it does not.**
> The bundle manifest carries a SHA-256 hash of the embedded `image.tar.gz`.
> The supervisor verifies this hash before loading the image, which catches
> accidental corruption in transit (a bad download, a flipped SD-card bit, a
> truncated upload).  It does **not** protect against a compromised release or
> a tampered CI pipeline — the hash and the payload come from the same CI job
> and are not independently signed.  No code-signing exists yet; you are
> trusting GitHub Actions and the GitHub release page.  If you build the image
> yourself the hash is computed over your own build output, which is equally
> trustworthy.  A future release will add a detached GPG signature over the
> manifest to close this gap.

### Online pull (optional — Pi needs internet)

If the Pi can reach the internet (marina WiFi), the supervisor can pull the
tagged image directly from GHCR. Check the task-5 docs for the `docker pull`
path via the supervisor API.

### Backups

Settings → System & updates → **Volume backups** → Snapshot now. Backups land
in `/var/lib/vanchor-supervisor/backups/` on the host. Download through the UI
or `scp`.

### Disk stewardship

The supervisor watches `df` and `docker system df`. When usage exceeds
configurable thresholds it emits a telemetry notification and a banner in the
UI. Settings → System → **Prune old images** removes all but the current and
previous image (rollback). See the supervisor docs for thresholds.

---

## 4. Building the image yourself

See **[`deploy/image/README.md`](../deploy/image/README.md)** for the full local
build guide.

**Short version:**
```bash
# Build the update bundle first (needs Docker)
python3 scripts/make_bundle.py app \
    --image "vanchor/vanchor" --tag <version> \
    --min-supervisor 0.1.0 --arch arm64 \
    --image-tar <saved-image.tar.gz> --out bundle.tar

# Build the SD image (needs Docker + ~15 GB scratch; privileged)
deploy/image/build.sh --version <version> --bundle bundle.tar
```

**Honest build times** (from `deploy/image/README.md`):
- Native arm64 (GH `ubuntu-24.04-arm` or Pi 5): **40–75 min** total.
- x86 laptop + qemu-user-static + binfmt: **2–4 h** worst case.

CI (`.github/workflows/image.yml`) builds on `ubuntu-24.04-arm` on every `v*`
tag push and uploads all release assets automatically.

---

## 5. The host guard (`VANCHOR_ALLOWED_HOSTS`)

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
# .env or docker-compose environment:
VANCHOR_ALLOWED_HOSTS=boat.example.com,vanchor.mydomain.net
```

An unlisted public-looking `Host` gets a `400 Host not allowed`.

---

## 6. Plain HTTP on the LAN vs HTTPS (Wake Lock)

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

## See also

- [`firmware/README.md`](../firmware/README.md) — schematics, pin maps, BOM
- [`docs/firmware.md`](firmware.md) — the Pi ↔ Arduino software contract
- [`docs/safety-matrix.md`](safety-matrix.md) — failure-mode × layer × test matrix
- [`docs/image-testing.md`](image-testing.md) — first-flash BENCH-VERIFY checklist
- [`.env.example`](../.env.example) / [`vanchor.example.yaml`](../vanchor.example.yaml) — every config key
- [`requirements.lock`](../requirements.lock) — the pinned install set

---

## Appendix A: Bare-metal venv install

> For advanced users who want to run directly on the OS (no Docker) or need
> to customise below the container boundary. The SD image path (section 2) is
> simpler and recommended for new installs.

### A.1 OS preparation

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

### A.2 Install Vanchor-NG from the lockfile

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
once the wheels are cached.

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

### A.3 Run as a systemd service (autostart)

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
# Only needed for a custom reverse-proxy hostname (see section 5):
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

### A.4 Updating (bare-metal)

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

## Appendix B: SD-write minimization — debugging trade-offs

The SD image ships with volatile journald and tmpfs `/var/log` to minimise SD
card wear. This means:

- **Logs are lost on reboot** — this is expected. To debug a hard crash or
  persistent issue, flip the journal to persistent:

  ```bash
  sudo rm /etc/systemd/journald.conf.d/50-vanchor.conf
  sudo systemctl restart systemd-journald
  ```

- **`/var/log` is RAM-backed (64 MB limit)** — if you need logs to survive a
  reboot, redirect to the data volume (`/opt/vanchor/data/`) which is on the
  SD rootfs but written to intentionally.

- **noatime on root partition** — breaks some old tools that rely on atime.
  Rarely an issue on a headless server. Disable with `mount -o remount,relatime /`
  if needed.
