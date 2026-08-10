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

# Radio tuning for a phone-on-deck link:
# - Disable WiFi power-save on the Pi's radio (2 = disable). Power-napping
#   caused missed WS keepalives -> the phone saw periodic disconnects.
# - Prefer 5 GHz (band a, ch 36) when the hardware supports it (Pi 3B+/4/5):
#   cleaner spectrum + iPhones nap less aggressively there. Falls back to the
#   universal 2.4 GHz profile default when 5 GHz is unsupported, and reverts at
#   the activation step below if the 5 GHz attempt fails for any other reason.
# BENCH-VERIFY: 5 GHz AP channel acceptance is region/hw dependent.
nmcli connection modify "$PROFILE" 802-11-wireless.powersave 2 || true
if [ "$(nmcli -g WIFI-PROPERTIES.5GHZ device show wlan0 2>/dev/null)" = "yes" ]; then
    nmcli connection modify "$PROFILE" 802-11-wireless.band a 802-11-wireless.channel 36 || true
fi

# Always (re)activate the AP. If the operator has explicitly joined a home
# network this session, that join holds the single radio; on the next reboot the
# higher-priority AP profile wins again, which is why we come back to the AP.
if ! nmcli connection up "$PROFILE"; then
    # 5 GHz attempt (or anything else) failed -> force the universal 2.4 GHz
    # setup and retry once. The boat MUST always end up joinable.
    nmcli connection modify "$PROFILE" 802-11-wireless.band bg 802-11-wireless.channel 0 || true
    nmcli connection up "$PROFILE" || true
fi
