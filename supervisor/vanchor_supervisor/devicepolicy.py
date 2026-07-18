"""Device policy checker: verifies required hardware devices are available.

Reads the app's devices.json from the data volume and checks each enabled
non-sim device against the host and the running container.

The app's devices.json has a flat "hardware" section:
  {
    "hardware": {
      "enabled": true,
      "gps_port":   "/dev/ttyACM0",  "gps_source": null,
      "motor_port": "i2c:1:0x3f",    "motor_source": "sim",
      "compass_port": "/dev/ttyUSB0", "compass_source": "sim"
    }
  }

"source" values of null/None or the empty string mean "use the real hardware
device at <name>_port".  "sim" or "none" means no hardware device is needed.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Pattern for i2c:BUS:ADDR  e.g. i2c:1:0x77
_I2C_PATTERN = re.compile(r"^i2c:(\d+):0x[0-9a-fA-F]+$")

# Port suffix → source suffix pairs known to the app
_PORT_SOURCE_PAIRS = [
    ("gps_port", "gps_source"),
    ("motor_port", "motor_source"),
    ("compass_port", "compass_source"),
    ("sonar_port", "sonar_source"),
]


# ---------------------------------------------------------------------------
# Module-level helpers (exposed for unit tests)
# ---------------------------------------------------------------------------


def _parse_proc_devices(text: str) -> dict[str, int]:
    """Parse the text of /proc/devices and return {driver_name: major_number}.

    Handles both character and block device sections.  Stops at blank lines
    between sections; ignores non-numeric major numbers.

    Example input line: "166 ttyACM"  → {"ttyACM": 166}
    """
    result: dict[str, int] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.endswith("devices:"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            major = int(parts[0])
        except ValueError:
            continue
        name = parts[1].strip()
        result[name] = major
    return result


def _resolve_i2c_path(bus: str) -> str:
    """Return the host device path for i2c bus number *bus*.

    Default implementation returns /dev/i2c-{bus}.  Tests may monkey-patch
    this function to redirect to a temp path.
    """
    return f"/dev/i2c-{bus}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check(entry: dict, volume_root: Path, backend) -> dict:
    """Check required devices for a container entry.

    Returns:
        {
            "ok": bool,
            "missing": list[str],   # device paths that are absent
            "checked": list[str],   # device paths that were tested
            "warnings": list[str],  # advisory messages
        }

    Reads <volume_root>/<entry["required_devices_from"]> to get the device
    requirements list from the app (devices.json).

    Only checks devices where hardware.enabled == True and source is not
    "sim", "none", or a non-None truthy string (any non-null, non-sim value
    means the device at <name>_port is required).
    """
    missing: list[str] = []
    checked: list[str] = []
    warnings: list[str] = []

    devices_file_rel = entry.get("required_devices_from")
    if not devices_file_rel:
        return {"ok": True, "missing": [], "checked": [], "warnings": []}

    devices_file = volume_root / devices_file_rel
    if not devices_file.exists():
        log.debug("devices.json not found at %s — skipping device check", devices_file)
        return {"ok": True, "missing": [], "checked": [], "warnings": []}

    try:
        devices_data = json.loads(devices_file.read_text())
    except Exception as exc:
        log.warning("Failed to read %s: %s", devices_file, exc)
        return {"ok": True, "missing": [], "checked": [], "warnings": [str(exc)]}

    container_name = entry.get("name", "")
    cgroup_rules = entry.get("device_cgroup_rules") or []

    # Support two devices.json shapes:
    # 1. Flat hardware section: {"hardware": {enabled, gps_port, gps_source, ...}}
    # 2. Generic list: [{"name": ..., "enabled": ..., "source": ..., "port": ...}]
    # 3. Generic dict: {"device_name": {"enabled": ..., "source": ..., "port": ...}}

    if isinstance(devices_data, dict) and "hardware" in devices_data:
        _check_app_hardware(
            devices_data["hardware"], container_name, backend, missing, checked
        )
    elif isinstance(devices_data, list):
        for item in devices_data:
            if not isinstance(item, dict):
                continue
            _check_generic_device(item, container_name, backend, missing, checked)
    elif isinstance(devices_data, dict):
        for dev_name, cfg in devices_data.items():
            if not isinstance(cfg, dict):
                continue
            _check_generic_device(cfg, container_name, backend, missing, checked)
    else:
        log.warning("Unexpected devices.json shape: %s", type(devices_data))

    # Advisory: gpiochip present on host but not in cgroup rules
    if os.path.exists("/dev/gpiochip0"):
        has_gpio_rule = any("gpiochip" in r or "254:" in r for r in cgroup_rules)
        if not has_gpio_rule:
            warnings.append(
                "/dev/gpiochip0 exists on host but no matching device-cgroup-rule; "
                "GPIO will not be accessible in the container."
            )

    return {
        "ok": len(missing) == 0,
        "missing": missing,
        "checked": checked,
        "warnings": warnings,
    }


def _check_app_hardware(
    hw: dict,
    container_name: str,
    backend,
    missing: list[str],
    checked: list[str],
) -> None:
    """Check devices from the app's flat hardware config dict."""
    if not hw.get("enabled", False):
        return

    for port_key, source_key in _PORT_SOURCE_PAIRS:
        source = hw.get(source_key)
        port = hw.get(port_key, "")

        # source=None/null/empty → use real hardware; source="sim"/"none" → skip
        if source is not None and str(source).lower() in ("sim", "none"):
            continue
        if not port:
            continue

        host_path = _resolve_host_path(port, port_key)
        if host_path is None:
            continue

        checked.append(host_path)

        if not os.path.exists(host_path):
            missing.append(host_path)
            log.warning("Device %s: not found on host at %s", port_key, host_path)
            continue

        if container_name:
            try:
                in_container = backend.exec_test_path(container_name, host_path)
                if not in_container:
                    missing.append(host_path)
                    log.warning(
                        "Device %s: not visible inside container %s at %s",
                        port_key, container_name, host_path,
                    )
            except Exception as exc:
                log.debug("exec_test_path failed for %s: %s", host_path, exc)


def _check_generic_device(
    cfg: dict,
    container_name: str,
    backend,
    missing: list[str],
    checked: list[str],
) -> None:
    """Check a single device entry from the generic list/dict format."""
    enabled = cfg.get("enabled", False)
    source = cfg.get("source", "").lower()

    if not enabled:
        return
    if source in ("sim", "none", ""):
        return

    port = cfg.get("port", "")
    host_path = _resolve_host_path(port, cfg.get("name", port))
    if host_path is None:
        return

    checked.append(host_path)

    if not os.path.exists(host_path):
        missing.append(host_path)
        return

    if container_name:
        try:
            in_container = backend.exec_test_path(container_name, host_path)
            if not in_container:
                missing.append(host_path)
        except Exception:
            pass


def _resolve_host_path(port: str, dev_name: str) -> str | None:
    """Convert a port specifier to a host /dev path.

    Handles:
      /dev/ttyACM0  → direct path
      i2c:1:0x77    → _resolve_i2c_path("1") → /dev/i2c-1
      <unknown>     → None (skip)
    """
    if not port:
        return None

    if port.startswith("/"):
        # Any absolute path (including /dev/ and test-only paths outside /dev/)
        return port

    m = _I2C_PATTERN.match(port)
    if m:
        bus = m.group(1)
        return _resolve_i2c_path(bus)

    # Unknown format — log and skip
    log.debug("Unrecognised port format %r for device %r — skipping", port, dev_name)
    return None
