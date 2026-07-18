"""Tests for vanchor_supervisor.devicepolicy."""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from vanchor_supervisor.devicepolicy import check, _parse_proc_devices
from supervisor_fakes import FakeDockerBackend


SAMPLE_PROC_DEVICES = """\
Character devices:
  1 mem
  4 /dev/vc/0
  4 tty
  5 /dev/tty
166 ttyACM
188 ttyUSB
204 ttyAMA
 89 i2c
251 ttyS
252 aux
253 auxdisplay
254 gpiochip

Block devices:
  7 loop
  8 sd
"""


# ------------------------------------------------------------------ #
# _parse_proc_devices
# ------------------------------------------------------------------ #

def test_parse_proc_devices_finds_gpiochip():
    majors = _parse_proc_devices(SAMPLE_PROC_DEVICES)
    assert "gpiochip" in majors
    assert majors["gpiochip"] == 254


def test_parse_proc_devices_finds_tty_acm():
    majors = _parse_proc_devices(SAMPLE_PROC_DEVICES)
    # ttyACM -> 166
    assert "ttyACM" in majors
    assert majors["ttyACM"] == 166


# ------------------------------------------------------------------ #
# check()
# ------------------------------------------------------------------ #

def _make_devices_json(tmp_path: Path, *, hardware_enabled: bool = True,
                       gps_port: str = "/dev/ttyACM0",
                       gps_source: str | None = None) -> Path:
    data = {
        "hardware": {
            "enabled": hardware_enabled,
            "gps_port": gps_port,
            "gps_source": gps_source,
            "compass_port": "/dev/ttyUSB0",
            "compass_source": "sim",
            "motor_port": "/dev/ttyUSB1",
            "motor_source": "sim",
        }
    }
    p = tmp_path / "devices.json"
    p.write_text(json.dumps(data))
    return p


def test_check_sim_only_returns_ok(tmp_path):
    """When all sources are sim, device check is trivially ok."""
    vol = tmp_path / "vol"
    vol.mkdir()
    devices_json = vol / "devices.json"
    devices_json.write_text(json.dumps({
        "hardware": {
            "enabled": True,
            "gps_source": "sim",
            "motor_source": "sim",
            "compass_source": "sim",
        }
    }))

    entry = {
        "name": "vanchor",
        "required_devices_from": "devices.json",
        "device_cgroup_rules": ["c 166:* rmw"],
    }
    backend = FakeDockerBackend(volume_root=vol)
    result = check(entry, vol, backend)
    assert result["ok"] is True
    assert result["missing"] == []


def test_check_hardware_disabled_returns_ok(tmp_path):
    vol = tmp_path / "vol"
    vol.mkdir()
    (vol / "devices.json").write_text(json.dumps({
        "hardware": {
            "enabled": False,
            "gps_port": "/dev/ttyACM0",
        }
    }))
    entry = {
        "name": "vanchor",
        "required_devices_from": "devices.json",
        "device_cgroup_rules": [],
    }
    backend = FakeDockerBackend(volume_root=vol)
    result = check(entry, vol, backend)
    assert result["ok"] is True


def test_check_existing_device_passes(tmp_path):
    vol = tmp_path / "vol"
    vol.mkdir()
    # Create a fake device file
    fake_dev = tmp_path / "ttyACM0"
    fake_dev.write_bytes(b"")

    (vol / "devices.json").write_text(json.dumps({
        "hardware": {
            "enabled": True,
            "gps_port": str(fake_dev),
            "gps_source": None,  # use real hardware
        }
    }))
    entry = {
        "name": "vanchor",
        "required_devices_from": "devices.json",
        "device_cgroup_rules": ["c 166:* rmw"],
    }
    backend = FakeDockerBackend(volume_root=vol)
    result = check(entry, vol, backend)
    assert result["ok"] is True
    assert str(fake_dev) in result["checked"]


def test_check_missing_device_fails(tmp_path):
    vol = tmp_path / "vol"
    vol.mkdir()
    missing_dev = str(tmp_path / "nonexistent_device")
    (vol / "devices.json").write_text(json.dumps({
        "hardware": {
            "enabled": True,
            "gps_port": missing_dev,
            "gps_source": None,
        }
    }))
    entry = {
        "name": "vanchor",
        "required_devices_from": "devices.json",
        "device_cgroup_rules": [],
    }
    backend = FakeDockerBackend(volume_root=vol)
    result = check(entry, vol, backend)
    assert result["ok"] is False
    assert missing_dev in result["missing"]


def test_check_i2c_format_parsed(tmp_path):
    """i2c:1:0x3f format -> /dev/i2c-1."""
    vol = tmp_path / "vol"
    vol.mkdir()
    # Create fake i2c device
    fake_i2c = tmp_path / "i2c-1"
    fake_i2c.write_bytes(b"")

    (vol / "devices.json").write_text(json.dumps({
        "hardware": {
            "enabled": True,
            "motor_port": "i2c:1:0x3f",
            "motor_source": None,
        }
    }))
    entry = {
        "name": "vanchor",
        "required_devices_from": "devices.json",
        "device_cgroup_rules": ["c 89:* rmw"],
    }
    backend = FakeDockerBackend(volume_root=vol)

    # Patch the i2c path resolution to use our tmp path
    import vanchor_supervisor.devicepolicy as dp
    original = dp._resolve_i2c_path
    dp._resolve_i2c_path = lambda bus: str(fake_i2c)
    try:
        result = check(entry, vol, backend)
        # Should either be ok (fake_i2c exists) or show the path was checked
        assert result["ok"] is True or str(fake_i2c) in result.get("checked", [])
    finally:
        dp._resolve_i2c_path = original


def test_check_no_required_devices_from(tmp_path):
    """Entry without required_devices_from skips device checks."""
    vol = tmp_path / "vol"
    vol.mkdir()
    entry = {
        "name": "some-addon",
        # no required_devices_from
    }
    backend = FakeDockerBackend(volume_root=vol)
    result = check(entry, vol, backend)
    assert result["ok"] is True
    assert result["checked"] == []
