"""Persistable + API-editable device/hardware config (devices.json).

Covers: the config-level save/load round-trip and field merge; a persisted
override changing what ``Runtime`` builds; and the GET/POST endpoints
(validation + persistence + reflected reads).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime, _TeeMotor
from vanchor.core.config import (
    DEVICES_FILE,
    AppConfig,
    HardwareConfig,
    NmeaTcpConfig,
    apply_device_overrides,
    load,
    load_device_overrides,
    save_device_overrides,
)
from vanchor.ui.server import create_app


class _FakeMotor:
    def apply(self, command):
        pass

    async def flush(self):
        pass


# --- config-level persistence ------------------------------------------- #
def test_save_load_round_trip(tmp_path):
    hw = HardwareConfig(enabled=True, gps_source="nmea", motor_source="both", baudrate=9600)
    nmea = NmeaTcpConfig(enabled=True, port=10111)
    written = save_device_overrides(tmp_path, hw, nmea)

    # File exists with the documented shape.
    p = tmp_path / DEVICES_FILE
    assert p.exists()
    on_disk = json.loads(p.read_text())
    assert on_disk == written
    assert on_disk["hardware"]["gps_source"] == "nmea"
    assert on_disk["nmea_tcp"] == {"enabled": True, "host": "0.0.0.0", "port": 10111}

    # load returns the parsed mapping.
    loaded = load_device_overrides(tmp_path)
    assert loaded["hardware"]["motor_source"] == "both"
    assert loaded["nmea_tcp"]["port"] == 10111


def test_save_is_atomic_no_tmp_left_and_replaces_cleanly(tmp_path):
    # Atomic write: the tmp file must not survive, and an existing (older) file is
    # fully replaced, never left truncated. Guards against a power-loss mid-write
    # silently reverting all hardware config to defaults.
    hw = HardwareConfig(enabled=True, gps_source="nmea")
    nmea = NmeaTcpConfig(enabled=False, port=10110)
    save_device_overrides(tmp_path, hw, nmea)
    # Overwrite with a different config; result must be the new one, clean JSON.
    save_device_overrides(tmp_path, HardwareConfig(enabled=True, gps_source="ublox"), nmea)
    assert not (tmp_path / (DEVICES_FILE + ".tmp")).exists()  # no tmp litter
    on_disk = json.loads((tmp_path / DEVICES_FILE).read_text())  # not truncated
    assert on_disk["hardware"]["gps_source"] == "ublox"
    assert load_device_overrides(tmp_path)["hardware"]["gps_source"] == "ublox"


def test_load_missing_returns_none(tmp_path):
    assert load_device_overrides(tmp_path) is None


def test_load_corrupt_returns_none(tmp_path):
    (tmp_path / DEVICES_FILE).write_text("not json {")
    assert load_device_overrides(tmp_path) is None


def test_apply_overrides_merges_tolerantly(tmp_path):
    # Partial / extra keys are tolerated; absent keys keep their loaded value.
    (tmp_path / DEVICES_FILE).write_text(
        json.dumps(
            {
                "hardware": {"motor_source": "both", "baudrate": 9600, "bogus": 1},
                "nmea_tcp": {"enabled": True},
            }
        )
    )
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.hardware.gps_port = "/dev/keepme"
    out = apply_device_overrides(cfg)
    assert out is cfg
    assert cfg.hardware.motor_source == "both"
    assert cfg.hardware.baudrate == 9600
    assert cfg.hardware.gps_port == "/dev/keepme"  # untouched key preserved
    assert cfg.nmea_tcp.enabled is True


def test_apply_overrides_noop_without_file(tmp_path):
    cfg = AppConfig(data_dir=str(tmp_path))
    apply_device_overrides(cfg)
    assert cfg.hardware == HardwareConfig()
    assert cfg.nmea_tcp == NmeaTcpConfig()


# --- override changes what Runtime builds ------------------------------- #
def test_override_file_changes_runtime_build(tmp_path):
    save_device_overrides(
        tmp_path,
        HardwareConfig(motor_source="both", gps_source="nmea"),
        NmeaTcpConfig(),
    )
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    apply_device_overrides(cfg)

    from unittest.mock import patch

    with patch.object(Runtime, "_build_serial_motor", lambda self, c: _FakeMotor()):
        rt = Runtime(cfg)
    # motor_source "both" -> tee of sim motor + serial servo.
    assert isinstance(rt.controller.motor, _TeeMotor)
    # gps_source "nmea" -> no internal GPS device.
    assert rt.gps is None
    assert rt.simulator is not None


# --- endpoints ----------------------------------------------------------- #
@pytest.fixture()
def client(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    app = create_app(Runtime(cfg))
    with TestClient(app) as c:
        c.tmp_path = tmp_path
        yield c


def test_get_returns_config_and_options(client):
    data = client.get("/api/config/devices").json()
    assert set(data) == {"hardware", "nmea_tcp", "sim_motor", "options", "menus",
                         "driver_menus", "source_transports", "restart_required"}
    assert data["restart_required"] is False
    # Each source's connection transport, so the UI shows the right fields.
    st = data["source_transports"]
    assert st["compass"]["magnetometer"] == "i2c"   # I2C -> I2C target field
    assert st["compass"]["hwt901b"] == "serial"     # serial -> port + baud
    assert st["compass"]["serial"] == "serial"
    assert st["compass"]["sim"] == "none" and st["compass"]["nmea"] == "none"
    assert st["compass"]["phone"] == "none"
    assert st["battery"]["ina226"] == "i2c"
    assert data["options"] == {
        "sensor": ["sim", "serial", "nmea", "none"],  # "none" = Not connected
        "gps": ["sim", "serial", "nmea", "none", "phone", "ublox"],  # + registered drivers
        "compass": ["sim", "serial", "nmea", "none", "hwt901b", "magnetometer", "phone"],  # + registered drivers
        "motor": ["sim", "serial", "both", "none"],
        "battery": ["sim", "none", "ina226"],  # sim/none built-in + registered ina226 driver
        # Split-channel sources: "both" is a combined concept, not per-channel.
        "steering": ["sim", "serial", "none"],
        "thrust": ["sim", "serial", "none"],
    }
    assert isinstance(data["menus"], list)  # active device menus (sim => empty)
    # Driver menu schemas by source, shown on selection (hwt901b ships one):
    assert "hwt901b" in data["driver_menus"]
    assert data["driver_menus"]["hwt901b"]["device"] == "compass"
    # Mirrors the live (default) config.
    assert data["hardware"]["enabled"] is False
    assert data["nmea_tcp"]["enabled"] is False


def test_post_persists_and_reflects(client):
    body = {
        "hardware": {"enabled": True, "gps_source": "nmea", "motor_source": "both", "baudrate": 9600},
        "nmea_tcp": {"enabled": True, "port": 10111},
    }
    r = client.post("/api/config/devices", json=body)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "restart_required": True}

    # Persisted to devices.json.
    on_disk = json.loads((client.tmp_path / DEVICES_FILE).read_text())
    assert on_disk["hardware"]["gps_source"] == "nmea"
    assert on_disk["hardware"]["baudrate"] == 9600
    assert on_disk["nmea_tcp"]["port"] == 10111

    # A subsequent GET reflects the in-memory update.
    after = client.get("/api/config/devices").json()
    assert after["hardware"]["enabled"] is True
    assert after["hardware"]["motor_source"] == "both"
    assert after["nmea_tcp"]["port"] == 10111


def test_post_rejects_bad_sensor_source(client):
    r = client.post("/api/config/devices", json={"hardware": {"gps_source": "bogus"}})
    assert r.status_code == 400
    assert r.json()["ok"] is False
    # Nothing persisted on a rejected edit.
    assert not (client.tmp_path / DEVICES_FILE).exists()


def test_post_rejects_bad_motor_source(client):
    # "nmea" is valid for sensors but NOT for the motor.
    r = client.post("/api/config/devices", json={"hardware": {"motor_source": "nmea"}})
    assert r.status_code == 400


def test_post_rejects_bad_baudrate(client):
    r = client.post("/api/config/devices", json={"hardware": {"baudrate": "fast"}})
    assert r.status_code == 400


def test_post_zero_baud_means_unset_not_error(client):
    """Regression (#114-era field report): saving with a sim GPS failed with
    "gps_baud must be a positive integer" because an empty baud box posted
    gps_baud:0 (JS Number("") is 0). 0 and "" must read as "not set"."""
    before = client.get("/api/config/devices").json()["hardware"].get("gps_baud")
    r = client.post("/api/config/devices",
                    json={"hardware": {"gps_source": "sim", "gps_baud": 0}})
    assert r.status_code == 200, r.text
    after = client.get("/api/config/devices").json()["hardware"]
    assert after["gps_source"] == "sim"
    assert after.get("gps_baud") == before          # kept, not zeroed

    r = client.post("/api/config/devices",
                    json={"hardware": {"compass_baud": ""}})
    assert r.status_code == 200, r.text


def test_post_negative_baud_still_rejected(client):
    r = client.post("/api/config/devices",
                    json={"hardware": {"gps_baud": -9600}})
    assert r.status_code == 400


def test_post_coerces_int_port_from_string(client):
    r = client.post("/api/config/devices", json={"nmea_tcp": {"port": "10120"}})
    assert r.status_code == 200
    assert client.get("/api/config/devices").json()["nmea_tcp"]["port"] == 10120


async def test_reload_applies_live_no_restart(tmp_path):
    """A sim-compatible device change applies LIVE (no process restart): switching
    GPS to external NMEA drops the internal GPS device immediately."""
    from vanchor.app import Runtime
    from vanchor.core.config import load
    cfg = load(None); cfg.data_dir = str(tmp_path)   # isolate: never write the repo's devices.json
    rt = Runtime(cfg)
    assert type(rt.gps).__name__ == "SimGps"
    rt.set_device_config({"hardware": {"gps_source": "nmea"}})
    res = await rt.reload_devices()
    assert res["applied"] is True
    assert rt.gps is None                       # internal GPS removed, live
    assert rt.simulator is not None             # sim boat still present (compass/motor sim)


async def test_reload_bad_serial_keeps_current_devices(tmp_path):
    """If the live rebuild fails (e.g. a serial port that doesn't exist), the
    current devices stay up and the call reports applied=False with an error."""
    from vanchor.app import Runtime
    from vanchor.core.config import load
    cfg = load(None); cfg.data_dir = str(tmp_path)   # isolate: never write the repo's devices.json
    rt = Runtime(cfg)
    before = rt.gps
    rt.set_device_config({"hardware": {"enabled": True}})  # all serial -> ports absent
    res = await rt.reload_devices()
    assert res["applied"] is False and "error" in res
    assert rt.gps is before                      # unchanged; autopilot uninterrupted
