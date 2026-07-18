# First-flash bench test — manual checklist

Nobody has booted this image yet in an automated way. Every box below is a
**BENCH-VERIFY** item: it can only be confirmed on real hardware (a Raspberry
Pi 4 or 5 with a 16 GB SD card). File a GitHub issue for any failing box,
quoting which checklist item failed and what you observed.

This file covers the complete first-flash experience from flashing through
steady-state operation.

---

## Pre-flash: CI verification

- [ ] **CI-1**: First push of a `v*` tag: open the Actions tab, watch the
  `SD Image` workflow run both `bundle` and `image` jobs to completion.
  Check the `size-report.txt` artifact — compare img.xz size against the
  §3 budget (expected ~0.9–1.2 GB). Record actual vs estimated.
- [ ] **CI-2**: `os_list.json` is uploaded as a release asset and accessible
  at `https://github.com/AlexAsplund/vanchor-ng/releases/latest/download/os_list.json`.

---

## Flash + first boot

1. **Pi Imager repository**: Paste
   `https://github.com/AlexAsplund/vanchor-ng/releases/latest/download/os_list.json`
   into Raspberry Pi Imager → "Use custom" / repository setting. Confirm
   that **Vanchor-NG** appears with the correct name and description. Flash
   a 16 GB (or larger) SD card.

2. **Imager customization dialog**: Before flashing, open the customization
   gear (Raspberry Pi Imager ≥ v1.8). Set hostname / username / SSH / WiFi
   if desired. Confirm the customization applies on first boot
   (`init_format: "systemd"` path via `raspberrypi-sys-mods` hooks).

3. **First-boot sequence** (ethernet cable unplugged, no WiFi configured):
   - Root partition auto-expands (one automatic reboot is normal and
     expected; `init_resize` in `cmdline.txt`, provided by stage2).
   - `vanchor-load-images.service` runs and completes (logs via
     `journalctl -u vanchor-load-images`; expect 30–60 s on Pi 4).
   - `vanchor-supervisor.service` starts and `docker ps` shows the
     `vanchor` container running.
   - Total first-boot time to responsive UI: ≤ ~3 min.

4. **Setup hotspot**: Within ~30 s of boot, the SSID `vanchor-setup` is
   visible on a phone or laptop. Connect with WPA2 password `vanchor-boat`.
   Phone should receive a `10.42.0.x` lease (dnsmasq via NM shared mode).

5. **UI reachable on hotspot**:
   - `http://10.42.0.1:8000` loads the Vanchor UI.
   - `http://vanchor.local:8000` also resolves (NM dnsmasq alias in
     `vanchor-dnsmasq.conf` → `address=/vanchor.local/10.42.0.1`).
   - Test on both iOS **and** Android — Android historically struggles with
     mDNS in browsers; the dnsmasq alias is the mitigation.

6. **WiFi setup card**:
   - Open Settings → "Data & system" → "WiFi & network" card appears
     (not hidden) and shows mode = `hotspot`, IP = `10.42.0.1`.
   - Click "Scan for networks" — your home network appears in the list with
     a reasonable signal percentage.
   - Click your home network, enter the correct password → join starts in
     background, UI shows the reconnect hint, hotspot drops.
   - Reconnect phone to home WiFi, open `http://vanchor.local:8000` — UI
     loads (avahi mDNS on home LAN).

   **Note**: python-zeroconf (used by the app's `discovery.py`) may log a
   name-conflict warning against avahi for `vanchor.local`; this is
   non-fatal (wrapped in `try/except`) and the UI still responds normally.

7. **Wrong password recovery**: Join with incorrect PSK → after ≤ ~60 s the
   `vanchor-setup` hotspot returns (hotspot-check fallback or join failure
   path in `wifi.py` restores it). The WiFi card shows the failed
   `last_join` with error message. Confirm the error message does NOT contain
   the incorrect PSK.

8. **Reboot on home LAN**: With the Pi on home WiFi, reboot. Confirm it
   rejoins home WiFi directly (autoconnect-priority: home network 0 >
   hotspot -10). Hotspot stays down.

9. **Out-of-range reboot**: Move the Pi out of range of all known networks
   (or `nmcli connection delete` all non-hotspot profiles). Reboot. Confirm
   hotspot returns within ~40 s (`vanchor-hotspot-check` service, 25 s
   sleep + NM bring-up).

10. **SSH**: By default SSH is disabled (`ENABLE_SSH=0`). Confirm `ssh
    vanchor@10.42.0.1` is refused. Enable SSH: mount the boot partition
    (`/boot/firmware`) on a laptop and `touch ssh`, then reboot. Confirm
    `sshd` is up. Check the motd contains the Vanchor note. Console login
    `vanchor`/`vanchor` works (change with `passwd`).

11. **Disk usage after first boot**: Run `df -h` and `docker images -a` on
    the Pi. Record:
    - Root partition usage vs the §3 budget (estimated 3.1–3.5 GB used).
    - Docker image sizes (estimated 321 MB for the vanchor image).
    - Free space on 16 GB card (expected ~10–11 GB).
    Compare against `size-report.txt` from CI.

12. **ModemManager absent**: Run `systemctl status ModemManager`. Expect
    `not-found` (purged by `01-run-chroot.sh`). Plug in a USB GPS on
    `/dev/ttyACM0` — confirm it is NOT seized by MM (e.g. `cat /dev/ttyACM0`
    shows NMEA sentences, not garbled data).

13. **Supervisor integration** (task 5 integration):
    - `curl http://localhost:9300/v1/health` → `{"ok": true, ...}`.
    - Download the release's `vanchor-update-<version>.bundle.tar`, upload
      via Settings → System & updates → Sideload. Confirm update applies,
      health-gates, and the previous container is gone.
    - Upload a deliberately corrupt bundle → confirm auto-rollback restores
      the previous image.

14. **Power-cut resilience**: Cut power 3× mid-operation (during first boot,
    during sideload, during normal operation). Each time confirm clean boot
    with no fsck errors, journald intact (volatile — empty on reboot is
    expected), and stack comes back up.

15. **zeroconf vs avahi**: Check `journalctl -u vanchor` for the mDNS
    name-conflict warning (python-zeroconf vs avahi both advertising
    `vanchor.local`). Confirm it is a warning, not an error, and the UI
    remains reachable.

---

## SD-write minimization verification

16. **Volatile journal**: After reboot, run `journalctl --disk-usage`.
    Confirm the journal is stored in RAM (`/run/log/journal/`) and size is
    bounded (≤ 32 MB). Logs from before the reboot are gone — this is
    expected. To enable persistent logging for debugging: remove
    `/etc/systemd/journald.conf.d/50-vanchor.conf` and reboot.

17. **tmpfs /var/log**: Run `mount | grep /var/log`. Confirm it is a tmpfs
    with `size=64M`. Confirm `/tmp` is also on tmpfs (systemd default on
    modern Bookworm; check `mount | grep /tmp`).

18. **noatime**: Run `mount | grep ' / '`. Confirm `noatime` appears in the
    mount options for the root partition.

19. **No SD swap**: Run `swapon --show`. Confirm no swap device backed by the
    SD card (`/var/swap` absent). zram swap should appear if zram-tools
    installed: `swapon --show` shows a `/dev/zram0` device.

20. **App-writer audit (steady-state write estimate)**:
    Run `iostat -d 60 5 | grep mmcblk` to measure SD writes over 5 minutes
    of idle operation. Expected steady-state:
    - `server.log`: rotated by the app's logging config (check
      `VANCHOR_DATA_DIR/server.log`; rotation bounded).
    - Debug recorder: only active when explicitly started — confirm disabled
      by default (no writes observed in `VANCHOR_DATA_DIR/debug_recordings/`
      unless you start it).
    - Blackbox ring: bounded ring buffer (`VANCHOR_DATA_DIR/blackbox/`) —
      confirm size is stable at idle.
    - Depth-chart saves: triggered by user action (depth sounder active) —
      confirm no spurious writes at idle.
    - Docker logs: bounded by `daemon.json` `local` driver (max 5 MB × 2).
    - journald: volatile (RAM only; zero SD writes).
    - Estimated steady-state write volume (typical fishing day, 8 h):
      * Docker logs (local driver): ≤ 10 MB/day
      * App server.log (rotated): ≤ 5 MB/day
      * Blackbox ring (active GPS): ≤ 20 MB/day
      * Debug recorder: 0 MB/day (opt-in only)
      * Depth chart saves: ≤ 10 MB/day (if sounder active)
      * OS writes (cron, logrotate, etc.): ≤ 5 MB/day (tmpfs-buffered)
      * **Total estimated: ≤ 50 MB/day** (well within SD endurance for
        a typical card rated 100+ TBW at these volumes).
    - **Follow-up items**: If `server.log` is found to grow unboundedly
      (no rotation), file an issue. If blackbox ring does not self-cap, file
      an issue. Do not fix app-level write issues in this task (task 6
      scope is infra).

21. **Docker log driver on SD image**: Run `docker inspect vanchor | jq
    '.[0].HostConfig.LogConfig'`. Confirm `"Type": "local"` and
    `"max-size": "5m"`. This verifies `daemon.json` is effective.

---

## Notes on known behaviour

- **NM AP autoconnect timing**: The `vanchor-hotspot-check` service sleeps
  25 s — this is a tunable; adjust if the hotspot consistently takes longer.
- **Single-radio trade-off**: The Pi has one WiFi radio. Joining a home
  network necessarily drops the hotspot (the card warns about this). There
  is no seamless handover.
- **polkit + uid-0 in container**: nmcli inside the container reaches host NM
  via the D-Bus socket mount. Default polkit policy typically permits NM
  operations for root (uid 0). If polkit denies operations, the workaround
  is to add a polkit rule in `/etc/polkit-1/rules.d/` on the host.
  **BENCH-VERIFY**: test this on a real Pi 5 with the latest Bookworm polkit.
- **WiFi PSK in argv**: `nmcli device wifi connect` does not accept the
  password via stdin or a file descriptor — the PSK is passed as a
  command-line argument and is briefly visible in `/proc/<pid>/cmdline` and
  `ps` output for the duration of the `nmcli` process (typically < 1 s).
  Vanchor never logs or persists the PSK. See also `docs/deploy-pi.md`
  (security notes) and the code comment in `src/vanchor/wifi.py`.
