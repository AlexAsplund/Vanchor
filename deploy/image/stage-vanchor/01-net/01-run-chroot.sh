#!/bin/bash -e
# Set WiFi country (Bookworm blocks wifi via rfkill until this is set).
raspi-config nonint do_wifi_country "${WPA_COUNTRY:-SE}" || true
# Purge ModemManager — it probes /dev/ttyUSB* and /dev/ttyACM* on hotplug
# and can seize the GPS and motor serial adapters.
apt-get purge -y modemmanager || true

# Free the GPIO UART (/dev/serial0) for a pin-wired GPS/compass. By default the
# Pi keeps a login console on that port, so a GPS wired to the TX/RX pins can't
# be read — the #1 wired-GPS gotcha. Enable the UART, take the console off it,
# and (Pi 3/4) hand the reliable PL011 to the GPIO pins by disabling onboard
# Bluetooth (unused on a boat; the flaky mini-UART otherwise lands on the pins).
# USB GPS is unaffected. BENCH-VERIFY: exact UART routing (mini-UART vs PL011,
# Pi 5 specifics) is unverifiable without hardware.
BOOT_DIR=/boot/firmware        # Bookworm; older layouts use /boot
[ -d "$BOOT_DIR" ] || BOOT_DIR=/boot
if [ -f "$BOOT_DIR/config.txt" ]; then
    grep -q '^enable_uart=1'        "$BOOT_DIR/config.txt" || echo 'enable_uart=1'        >> "$BOOT_DIR/config.txt"
    grep -q '^dtoverlay=disable-bt' "$BOOT_DIR/config.txt" || echo 'dtoverlay=disable-bt' >> "$BOOT_DIR/config.txt"
fi
if [ -f "$BOOT_DIR/cmdline.txt" ]; then
    # Drop the serial-console token so getty no longer holds the port.
    sed -i -E 's/console=(serial0|ttyAMA0|ttyS0),[0-9]+ ?//g' "$BOOT_DIR/cmdline.txt"
fi
systemctl disable hciuart 2>/dev/null || true              # BT-over-UART (paired with disable-bt)
systemctl mask serial-getty@ttyAMA0.service 2>/dev/null || true
systemctl mask serial-getty@ttyS0.service 2>/dev/null || true
systemctl mask serial-getty@serial0.service 2>/dev/null || true

# Boot time: don't gate boot on "network online". docker.service Wants=
# network-online.target, which runs NetworkManager-wait-online -- and on the
# boat (an AP with no upstream internet) that blocks for its FULL timeout
# (30-60 s) before Docker (and therefore vanchor) even starts. The boat never
# needs to be "online" to serve its own AP; disabling the wait lets the target
# activate immediately. (Dependencies can't be removed via drop-in, so
# disabling the wait service is the standard appliance fix.)
systemctl disable NetworkManager-wait-online.service 2>/dev/null || true

# Boot time: shave firmware delays (no rainbow splash, no boot_delay wait).
if [ -f "$BOOT_DIR/config.txt" ]; then
    grep -q '^disable_splash=1' "$BOOT_DIR/config.txt" || echo 'disable_splash=1' >> "$BOOT_DIR/config.txt"
    grep -q '^boot_delay=0'     "$BOOT_DIR/config.txt" || echo 'boot_delay=0'     >> "$BOOT_DIR/config.txt"
fi

# Enable the access-point service (brings the AP up at every boot; offline-first).
systemctl enable vanchor-hotspot.service
