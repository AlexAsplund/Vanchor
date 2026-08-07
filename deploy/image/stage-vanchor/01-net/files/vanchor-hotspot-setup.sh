#!/bin/bash
# Bring up the vanchor access point at boot. Offline-first: a boat has no
# upstream WiFi, so the Pi IS the network and must always be joinable.
#
# The broadcast SSID is per-device -- vanchor-<last 4 hex of the wlan0 MAC> --
# so two units on the same water don't collide. The NetworkManager connection
# *id* stays the stable "vanchor-setup" (what src/vanchor/wifi.py keys on); only
# the 802-11-wireless.ssid field is rewritten here.
#
# BENCH-VERIFY: NM AP activation + SSID-modify timing is unverifiable without
# real Pi WiFi hardware.
set -u

PROFILE="vanchor-setup"

# Derive vanchor-<last4> from the wlan0 MAC (e.g. dc:a6:32:ab:cd:ef -> vanchor-cdef).
mac="$(cat /sys/class/net/wlan0/address 2>/dev/null || true)"
hex="${mac//:/}"           # strip colons
suffix="${hex: -4}"        # last 4 hex digits (note the space before -4)
suffix="${suffix,,}"       # lowercase
if [ -n "$suffix" ]; then
    nmcli connection modify "$PROFILE" 802-11-wireless.ssid "vanchor-${suffix}" || true
fi

# Always (re)activate the AP. If the operator has explicitly joined a home
# network this session, that join holds the single radio; on the next reboot the
# higher-priority AP profile wins again, which is why we come back to the AP.
nmcli connection up "$PROFILE" || true
