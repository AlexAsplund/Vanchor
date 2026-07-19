#!/bin/bash -e
# Copy the vanchor stack into the rootfs.
# pi-gen runs this INSIDE a build container where build.sh's exported REPO_DIR
# is NOT visible, so build.sh stages the repo files it needs into this stage's
# files/ dir (docker-compose.yml, supervisor/, factory-bundle.tar) and we
# install from files/ — never from REPO_DIR.
install -d "${ROOTFS_DIR}/opt/vanchor/compose" \
           "${ROOTFS_DIR}/opt/vanchor/factory" \
           "${ROOTFS_DIR}/opt/vanchor/data"

# Compose file (owns the container contract: host network, /data, devices)
install -m 644 files/docker-compose.yml "${ROOTFS_DIR}/opt/vanchor/compose/"

# Supervisor package (host-side python daemon + systemd unit + guard).
# Wipe the target first so the copy is idempotent: on a pi-gen re-run (or when
# a prior partial run left the tree hardlinked into the rootfs) `cp -a` onto an
# existing same-inode file aborts with "are the same file". Removing the dest
# breaks any such link and guarantees a clean copy from the staged files/.
rm -rf "${ROOTFS_DIR}/opt/vanchor-supervisor"
install -d "${ROOTFS_DIR}/opt/vanchor-supervisor"
cp -a --no-preserve=links files/supervisor/. "${ROOTFS_DIR}/opt/vanchor-supervisor/"

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
