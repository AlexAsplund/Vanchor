#!/bin/bash -e
# Load the baked vanchor docker image from the factory bundle on first boot.
# Zero-network: no internet required; image is pre-baked into the bundle.
# The bundle is the same format as the task-5 sideload bundle:
#   manifest.json  (format=vanchor-bundle, kind=app, image_sha256, ...)
#   image.tar.gz   (docker save output, gzipped)
#
# Also seeds /var/lib/vanchor-supervisor/containers.json with the correct
# image:tag from the manifest if the file does not already exist, so the
# supervisor starts with the right tag on first boot (I3 fix).
STAMP=/var/lib/vanchor/.images-loaded
[ -e "$STAMP" ] && exit 0
mkdir -p /var/lib/vanchor
BUNDLE=/opt/vanchor/factory/factory-bundle.tar
WORK=$(mktemp -d)
tar -xf "$BUNDLE" -C "$WORK"
# Verify sha256, load the image, and seed containers.json.
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

image_repo = man.get("image", "vanchor/vanchor")
image_tag  = man.get("app_version") or man.get("tag", "latest")
print(f"Loaded image: {image_repo}:{image_tag}")

# Seed supervisor containers.json from the manifest so the supervisor starts
# the exact image that was just loaded.  Only written when absent so manual
# edits on a running system are not clobbered.
state_dir = pathlib.Path("/var/lib/vanchor-supervisor")
containers_json = state_dir / "containers.json"
if not containers_json.exists():
    state_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "name": "vanchor",
        "image": image_repo,
        "tag": image_tag,
        "previous_tag": None,
        "network": "host",
        "env": {"VANCHOR_HOST": "0.0.0.0", "VANCHOR_DATA_DIR": "/data"},
        "volumes": [
            {"volume": "vanchor_data", "target": "/data"},
            {"host": "/dev", "target": "/dev", "ro": True},
            {
                "host": "/run/dbus/system_bus_socket",
                "target": "/run/dbus/system_bus_socket",
            },
        ],
        "device_cgroup_rules": [
            "c 166:* rmw",
            "c 188:* rmw",
            "c 204:* rmw",
            "c 89:* rmw",
        ],
        "devices": ["/dev/gpiochip0"],
        "restart": "unless-stopped",
        "logging": {"driver": "local", "options": {"max-size": "5m", "max-file": "2"}},
        "health_url": "http://127.0.0.1:8000/api/state",
        "update_policy": {"channel": "release"},
        "required_devices_from": "devices.json",
    }
    containers_json.write_text(json.dumps([entry], indent=2))
    print(f"Seeded containers.json: {image_repo}:{image_tag}")
else:
    print(f"containers.json already exists — skipping seed")
EOF
rm -rf "$WORK"
touch "$STAMP"
