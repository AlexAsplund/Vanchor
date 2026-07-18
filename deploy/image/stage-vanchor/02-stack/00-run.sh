#!/bin/bash -e
# Copy the vanchor stack into the rootfs.
# REPO_DIR must be exported by build.sh before calling pi-gen.
# Task-5 reconciliation: artifacts are at the repo root, not deploy/docker/:
#   Dockerfile   → REPO_DIR/Dockerfile
#   compose      → REPO_DIR/docker-compose.yml
#   supervisor   → REPO_DIR/supervisor/
#
install -d "${ROOTFS_DIR}/opt/vanchor/compose" \
           "${ROOTFS_DIR}/opt/vanchor/factory" \
           "${ROOTFS_DIR}/opt/vanchor/data"

# Compose file (owns the container contract: host network, /data, devices)
install -m 644 "${REPO_DIR}/docker-compose.yml" "${ROOTFS_DIR}/opt/vanchor/compose/"

# Supervisor package (host-side python daemon + systemd unit + guard)
cp -a "${REPO_DIR}/supervisor/." "${ROOTFS_DIR}/opt/vanchor-supervisor/"

# Factory bundle: the docker image pre-baked at CI time.
# build.sh copies the bundle to files/factory-bundle.tar (fixed staging name).
install -m 644 files/factory-bundle.tar "${ROOTFS_DIR}/opt/vanchor/factory/factory-bundle.tar"

# Systemd units and scripts
install -m 644 files/vanchor-load-images.service "${ROOTFS_DIR}/etc/systemd/system/"
install -m 755 files/vanchor-load-images.sh "${ROOTFS_DIR}/usr/local/sbin/vanchor-load-images"

# MOTD (console note; SSH is off by default)
install -m 644 files/motd "${ROOTFS_DIR}/etc/motd"

# SD-wear: volatile journald (logs lost on power cycle; flip Storage=persistent
# and remove this drop-in to debug — see docs/deploy-pi.md Appendix B).
install -d "${ROOTFS_DIR}/etc/systemd/journald.conf.d"
install -m 644 files/vanchor-journald.conf \
    "${ROOTFS_DIR}/etc/systemd/journald.conf.d/50-vanchor.conf"

# tmpfs for /tmp and /var/log (size-capped; SD-wear reduction)
install -d "${ROOTFS_DIR}/etc/systemd/system"
install -m 644 files/var-log.mount "${ROOTFS_DIR}/etc/systemd/system/var-log.mount"
