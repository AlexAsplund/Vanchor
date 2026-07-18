#!/bin/bash -e
# Light trim: remove apt caches, docs, manpages.
# Do NOT purge configured locales; do NOT remove kernel modules.
apt-get -y autoremove --purge
apt-get clean
rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*
rm -rf /usr/share/doc/* /usr/share/man/*

# Capture disk usage for the size report (uploaded as a CI artifact).
# Bookworm mounts the boot partition at /boot/firmware; fall back to /boot.
df -h / > /boot/firmware/size-report.txt 2>/dev/null || df -h / > /boot/size-report.txt
