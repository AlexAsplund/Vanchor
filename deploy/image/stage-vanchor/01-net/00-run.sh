#!/bin/bash -e
# Install NetworkManager connection profile, DNS alias, and hotspot units.
# The NM connection file must be mode 0600 root:root — NM refuses 0644.
install -d "${ROOTFS_DIR}/etc/NetworkManager/system-connections"
install -d "${ROOTFS_DIR}/etc/NetworkManager/dnsmasq-shared.d"
install -d "${ROOTFS_DIR}/etc/systemd/system"
install -d "${ROOTFS_DIR}/usr/local/sbin"

install -m 0600 files/vanchor-setup.nmconnection \
    "${ROOTFS_DIR}/etc/NetworkManager/system-connections/vanchor-setup.nmconnection"
install -m 0644 files/vanchor-dnsmasq.conf \
    "${ROOTFS_DIR}/etc/NetworkManager/dnsmasq-shared.d/vanchor.conf"
install -m 0644 files/vanchor-hotspot.service \
    "${ROOTFS_DIR}/etc/systemd/system/vanchor-hotspot.service"
install -m 0755 files/vanchor-hotspot-check.sh \
    "${ROOTFS_DIR}/usr/local/sbin/vanchor-hotspot-check"
