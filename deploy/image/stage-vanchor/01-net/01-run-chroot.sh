#!/bin/bash -e
# Set WiFi country (Bookworm blocks wifi via rfkill until this is set).
raspi-config nonint do_wifi_country "${WPA_COUNTRY:-SE}" || true
# Purge ModemManager — it probes /dev/ttyUSB* and /dev/ttyACM* on hotplug
# and can seize the GPS and motor serial adapters.
apt-get purge -y modemmanager || true
# Enable the hotspot fallback service.
systemctl enable vanchor-hotspot.service
