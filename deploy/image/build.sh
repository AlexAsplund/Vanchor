#!/usr/bin/env bash
# Vanchor-NG SD image builder.
# Clones pi-gen at the pinned commit, installs the stage-vanchor stage, and
# runs pi-gen's build-docker.sh to produce a flashable .img.xz.
#
# Usage:
#   deploy/image/build.sh [--version VER] --bundle PATH [--workdir DIR]
#
# Requirements:
#   - Docker (privileged; pi-gen's build-docker.sh uses --privileged).
#   - On x86 hosts: qemu-user-static + binfmt-misc (install via
#     `apt install qemu-user-static` + `update-binfmts --enable`).
#     Native arm64 (e.g. GH ubuntu-24.04-arm runners): no qemu needed.
#   - ~15 GB free disk in the workdir.
#
# Outputs:
#   <workdir>/pi-gen/deploy/vanchor-<version>-arm64.img.xz  (and .sha256)
#
# BENCH-VERIFY: this script drives an actual pi-gen build and can only be
#   fully validated on a machine with Docker and ~15 GB scratch. The CI
#   workflow (.github/workflows/image.yml) runs it on ubuntu-24.04-arm.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
export REPO_DIR

# ---- pi-gen pinned commit ------------------------------------------------
# arm64 branch of pi-gen (Bookworm 64-bit). Pin an exact commit for
# reproducibility. To upgrade: find the current HEAD of the arm64 branch at
# https://github.com/RPi-Distro/pi-gen and update this SHA.
# Pinned to pi-gen tag 2026-06-18-raspios-bookworm-arm64 (a real, stable
# Bookworm 64-bit release commit). Bump to a newer *-raspios-bookworm-arm64
# tag from https://github.com/RPi-Distro/pi-gen/tags when needed.
PIGEN_SHA="d7a31c6aa09f4b867902c51da2b45807c0a1709e"
PIGEN_URL="https://github.com/RPi-Distro/pi-gen.git"

# ---- argument parsing ----------------------------------------------------
VERSION=""
BUNDLE=""
WORKDIR="${REPO_DIR}/work/pi-gen-build"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --bundle)  BUNDLE="$2";  shift 2 ;;
        --workdir) WORKDIR="$2"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

# Derive version from pyproject.toml if not supplied
if [[ -z "$VERSION" ]]; then
    VERSION=$(python3 -c "
import tomllib, pathlib
p = pathlib.Path('${REPO_DIR}/pyproject.toml')
print(tomllib.loads(p.read_text())['project']['version'])
")
fi

if [[ -z "$BUNDLE" ]]; then
    echo "ERROR: --bundle <path> is required (the app bundle .tar from CI/make_bundle.py)" >&2
    exit 1
fi

BUNDLE="$(realpath "$BUNDLE")"
if [[ ! -f "$BUNDLE" ]]; then
    echo "ERROR: bundle not found: $BUNDLE" >&2
    exit 1
fi

echo "==> Vanchor-NG image build"
echo "    version : $VERSION"
echo "    bundle  : $BUNDLE"
echo "    workdir : $WORKDIR"
echo "    pi-gen  : $PIGEN_SHA"

# ---- clone pi-gen (skip if already at the right sha) --------------------
PIGEN_DIR="${WORKDIR}/pi-gen"
mkdir -p "$WORKDIR"
if [[ -d "$PIGEN_DIR/.git" ]]; then
    CURRENT_SHA=$(git -C "$PIGEN_DIR" rev-parse HEAD 2>/dev/null || echo "")
    if [[ "$CURRENT_SHA" == "$PIGEN_SHA" ]]; then
        echo "==> pi-gen already at $PIGEN_SHA, skipping clone"
    else
        echo "==> pi-gen at wrong sha ($CURRENT_SHA), re-cloning"
        rm -rf "$PIGEN_DIR"
    fi
fi
if [[ ! -d "$PIGEN_DIR/.git" ]]; then
    echo "==> Cloning pi-gen @ $PIGEN_SHA"
    git clone "$PIGEN_URL" "$PIGEN_DIR"
    git -C "$PIGEN_DIR" checkout "$PIGEN_SHA"
fi

# ---- write config -------------------------------------------------------
sed "s/@VERSION@/${VERSION}/" "${SCRIPT_DIR}/config.template" > "${PIGEN_DIR}/config"
echo "==> Config written: $(grep IMG_NAME "${PIGEN_DIR}/config")"

# ---- install stage-vanchor into pi-gen tree -----------------------------
cp -a "${SCRIPT_DIR}/stage-vanchor" "${PIGEN_DIR}/"

# Copy the bundle as the fixed staging name consumed by 02-stack/00-run.sh
install -m 644 "$BUNDLE" "${PIGEN_DIR}/stage-vanchor/02-stack/files/factory-bundle.tar"

# Stage the repo files the stage script installs into the rootfs. pi-gen runs
# its stage scripts INSIDE a build container where our exported REPO_DIR is not
# visible, so the compose file and supervisor package must be baked into the
# stage's files/ dir (like the bundle above) rather than read from REPO_DIR at
# stage-run time.
install -m 644 "${REPO_DIR}/docker-compose.yml" \
    "${PIGEN_DIR}/stage-vanchor/02-stack/files/docker-compose.yml"
rm -rf "${PIGEN_DIR}/stage-vanchor/02-stack/files/supervisor"
cp -a "${REPO_DIR}/supervisor" "${PIGEN_DIR}/stage-vanchor/02-stack/files/supervisor"
# Drop any local build cruft so it can't leak into the image.
find "${PIGEN_DIR}/stage-vanchor/02-stack/files/supervisor" \
    -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ---- build via pi-gen's docker wrapper ----------------------------------
echo "==> Running pi-gen build-docker.sh (this takes 30-60 min on arm64 natively)"
cd "$PIGEN_DIR"
CLEAN=1 ./build-docker.sh

# ---- output -------------------------------------------------------------
DEPLOY="${PIGEN_DIR}/deploy"
echo ""
echo "==> Build complete. Artifacts:"
ls -lh "${DEPLOY}"/*.img.xz 2>/dev/null || echo "  (no .img.xz found -- check pi-gen logs)"
echo "==> Deploy path: ${DEPLOY}"
