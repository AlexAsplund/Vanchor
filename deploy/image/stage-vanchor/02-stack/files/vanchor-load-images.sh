#!/bin/bash -e
# Load the baked vanchor docker image from the factory bundle on first boot.
# Zero-network: no internet required; image is pre-baked into the bundle.
# The bundle is the same format as the task-5 sideload bundle:
#   manifest.json  (format=vanchor-bundle, kind=app, image_sha256, ...)
#   image.tar.gz   (docker save output, gzipped)
STAMP=/var/lib/vanchor/.images-loaded
[ -e "$STAMP" ] && exit 0
mkdir -p /var/lib/vanchor
BUNDLE=/opt/vanchor/factory/factory-bundle.tar
WORK=$(mktemp -d)
tar -xf "$BUNDLE" -C "$WORK"
# Verify sha256 and load the image using the task-5 bundle manifest format.
python3 - "$WORK" <<'EOF'
import hashlib, json, subprocess, sys, pathlib
work = pathlib.Path(sys.argv[1])
man = json.loads((work / "manifest.json").read_text())
# Task-5 bundle format: manifest has image_sha256 + image file is "image.tar.gz"
image_file = work / "image.tar.gz"
expected_sha = man.get("image_sha256")
if expected_sha:
    actual_sha = hashlib.sha256(image_file.read_bytes()).hexdigest()
    if actual_sha != expected_sha:
        raise SystemExit(f"checksum mismatch for image.tar.gz: {actual_sha} != {expected_sha}")
subprocess.run(["docker", "load", "-i", str(image_file)], check=True)
print(f"Loaded image: {man.get('image')}:{man.get('tag', 'latest')}")
EOF
rm -rf "$WORK"
touch "$STAMP"
