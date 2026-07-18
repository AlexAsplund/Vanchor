#!/usr/bin/env bash
# Idempotent installer for vanchor-supervisor.
# BENCH-VERIFY only — not run in CI (requires a Pi with docker + systemd).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_ROOT="/opt/vanchor-supervisor"
UNIT_DIR="/etc/systemd/system"
VER="$(python3 -c 'import sys; sys.path.insert(0,"'"$REPO_ROOT/supervisor"'"); from vanchor_supervisor import SUPERVISOR_VERSION; print(SUPERVISOR_VERSION)')"
echo "Installing vanchor-supervisor $VER"
mkdir -p "$INSTALL_ROOT/versions"
cp -r "$REPO_ROOT/supervisor/vanchor_supervisor" "$INSTALL_ROOT/versions/$VER/"
cp "$REPO_ROOT/supervisor/guard.py" "$INSTALL_ROOT/guard.py"
# current symlink
CURRENT="$INSTALL_ROOT/current"
if [ ! -L "$CURRENT" ]; then
  ln -s "$INSTALL_ROOT/versions/$VER" "$CURRENT"
else
  TMP="$INSTALL_ROOT/current.tmp"
  ln -sf "$INSTALL_ROOT/versions/$VER" "$TMP"
  mv -T "$TMP" "$CURRENT"
fi
cp "$REPO_ROOT/supervisor/vanchor-supervisor.service" "$UNIT_DIR/vanchor-supervisor.service"
systemctl daemon-reload
systemctl enable --now vanchor-supervisor
echo "Done. Status:"
systemctl status vanchor-supervisor --no-pager || true
