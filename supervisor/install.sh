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
cp -r "$SCRIPT_DIR/vanchor_supervisor" "$INSTALL_ROOT/versions/$VER/"
cp "$SCRIPT_DIR/guard.py" "$INSTALL_ROOT/guard.py"
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
