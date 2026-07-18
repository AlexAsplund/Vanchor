#!/bin/bash -e
# Enable the vanchor services, add group memberships, and configure
# SD-write-minimisation (noatime, no SD swap, zram).

# Enable docker image loader (runs once on first boot, then stamps itself done)
# and the supervisor daemon.
# Supervisor unit name reconciled from task-5: vanchor-supervisor.service
systemctl enable vanchor-load-images.service
systemctl enable vanchor-supervisor.service || true   # unit lives under /opt/vanchor-supervisor

# Enable the tmpfs /var/log mount
systemctl enable var-log.mount

# Add vanchor user to docker and dialout groups (serial hotplug)
usermod -aG docker,dialout vanchor

# ---- SD-write minimisation -----------------------------------------------

# noatime on root partition: find the rootfs fstab entry and add noatime.
# pi-gen writes PARTUUID-based entries; the defaults field is modified in-place.
# BENCH-VERIFY: fstab PARTUUID lines verified on a built image.
if [ -f /etc/fstab ]; then
    # Add noatime to the ext4 root partition line (only, not boot/firmware vfat)
    sed -i '/ext4/ s/defaults/defaults,noatime/' /etc/fstab
fi

# Disable dphys-swapfile (SD swap wears the card; use zram instead).
# BENCH-VERIFY: addendum override — dphys-swapfile disabled even on Zero 2 W.
if systemctl is-enabled dphys-swapfile 2>/dev/null | grep -q enabled; then
    systemctl disable dphys-swapfile
fi

# Install zram-tools for compressed RAM swap (no SD writes).
apt-get install -y zram-tools || true
# Configure zram: 25% of RAM, lz4 algorithm
if [ -f /etc/default/zramswap ]; then
    sed -i 's/^#\?PERCENT=.*/PERCENT=25/' /etc/default/zramswap
    sed -i 's/^#\?ALGO=.*/ALGO=lz4/' /etc/default/zramswap
fi

# Install the supervisor python package in-place
if [ -f /opt/vanchor-supervisor/install.sh ]; then
    bash /opt/vanchor-supervisor/install.sh || true
fi
# Alternatively, ensure the service unit knows its PYTHONPATH
install -d /etc/systemd/system
if [ -f /opt/vanchor-supervisor/vanchor-supervisor.service ]; then
    install -m 644 /opt/vanchor-supervisor/vanchor-supervisor.service \
        /etc/systemd/system/vanchor-supervisor.service
fi

systemctl enable docker
