#!/usr/bin/env bash
# Idempotent installer for vanchor-supervisor.
# Works in two contexts:
#   1. Developer bench: run from the repo root as supervisor/install.sh
#   2. Pi image chroot: run from /opt/vanchor-supervisor/install.sh
#      (00-run.sh copies supervisor/. there; SCRIPT_DIR resolves correctly)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_ROOT="/opt/vanchor-supervisor"
UNIT_DIR="/etc/systemd/system"
VER="$(python3 -c 'import sys; sys.path.insert(0,"'"$SCRIPT_DIR"'"); from vanchor_supervisor import SUPERVISOR_VERSION; print(SUPERVISOR_VERSION)')"
echo "Installing vanchor-supervisor $VER"
mkdir -p "$INSTALL_ROOT/versions"
# Copy the package into versions/$VER/ so selfupdate.py's sanity-check
# (sys.path.insert(0, target_dir); import vanchor_supervisor) works:
#   target_dir/vanchor_supervisor/__init__.py  ← required layout
# In the Pi-image chroot this script runs FROM $INSTALL_ROOT (00-run.sh copied
# supervisor/. there), so $SCRIPT_DIR == $INSTALL_ROOT and guard.py is already
# in place — copying it onto itself makes `cp` abort ("are the same file").
# Guard every copy on source != destination so both contexts work.
rm -rf "$INSTALL_ROOT/versions/$VER"
mkdir -p "$INSTALL_ROOT/versions/$VER"
cp -r "$SCRIPT_DIR/vanchor_supervisor" "$INSTALL_ROOT/versions/$VER/"
if [ "$SCRIPT_DIR/guard.py" != "$INSTALL_ROOT/guard.py" ]; then
  cp "$SCRIPT_DIR/guard.py" "$INSTALL_ROOT/guard.py"
fi
# current symlink (atomic update)
CURRENT="$INSTALL_ROOT/current"
if [ ! -L "$CURRENT" ]; then
  ln -s "$INSTALL_ROOT/versions/$VER" "$CURRENT"
else
  TMP="$INSTALL_ROOT/current.tmp"
  ln -sf "$INSTALL_ROOT/versions/$VER" "$TMP"
  mv -T "$TMP" "$CURRENT"
fi
# Install the systemd unit.
cp "$SCRIPT_DIR/vanchor-supervisor.service" "$UNIT_DIR/vanchor-supervisor.service"
# daemon-reload and enable are run by the caller (01-run-chroot.sh or the user)
# so that this script works in chroot environments where systemd is absent.
echo "Done — call 'systemctl daemon-reload && systemctl enable --now vanchor-supervisor' to activate."
