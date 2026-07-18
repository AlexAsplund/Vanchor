"""Tests for the hardware scan/probe Runtime methods and API endpoints.

Covers:
  - Runtime.hw_scan() shape + demo mode
  - Runtime._ports_in_use() correctness
  - Runtime._i2c_addrs_in_use() correctness
  - GET /api/hw/scan response shape
  - POST /api/hw/probe error paths (400, 409)
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.ui.server import create_app


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture()
def rt():
    """A Runtime in default (sim) mode with default config."""
    return Runtime(load(None))


@pytest.fixture()
def client(rt):
    app = create_app(rt)
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- #
# hw_scan() — shape tests
# --------------------------------------------------------------------------- #

class TestHwScanShape:
    def test_hw_scan_returns_dict(self, rt):
        result = rt.hw_scan()
        assert isinstance(result, dict)

    def test_hw_scan_has_required_keys(self, rt):
        result = rt.hw_scan()
        for key in ("ports", "i2c_buses", "known_i2c", "capabilities"):
            assert key in result, f"hw_scan missing key: {key}"

    def test_ports_is_list(self, rt):
        assert isinstance(rt.hw_scan()["ports"], list)

    def test_i2c_buses_is_list(self, rt):
        assert isinstance(rt.hw_scan()["i2c_buses"], list)

    def test_known_i2c_is_list(self, rt):
        assert isinstance(rt.hw_scan()["known_i2c"], list)

    def test_capabilities_is_dict(self, rt):
        caps = rt.hw_scan()["capabilities"]
        assert isinstance(caps, dict)
        assert "serial" in caps
        assert "i2c" in caps


# --------------------------------------------------------------------------- #
# hw_scan() — port entry schema
# --------------------------------------------------------------------------- #

class TestHwScanPortSchema:
    def test_port_entries_have_path_key(self, rt):
        """Serial port entries use 'path' (not 'port') for the device path."""
        for entry in rt.hw_scan()["ports"]:
            assert "path" in entry

    def test_i2c_bus_entries_have_bus_key(self, rt):
        for entry in rt.hw_scan()["i2c_buses"]:
            assert "bus" in entry

    def test_known_i2c_entries_have_addr_and_kind(self, rt):
        """known_i2c entries must have 'addr' and 'kind' keys."""
        for entry in rt.hw_scan()["known_i2c"]:
            assert "addr" in entry
            assert "kind" in entry


# --------------------------------------------------------------------------- #
# _ports_in_use()
# --------------------------------------------------------------------------- #

class TestPortsInUse:
    def test_sim_mode_no_ports_in_use(self, rt):
        """Sim mode has no real drivers running."""
        in_use = rt._ports_in_use()
        assert isinstance(in_use, dict)
        # Sim mode: all drivers are sims, so no real port paths
        assert len(in_use) == 0

    def test_returns_dict_always(self, rt):
        assert isinstance(rt._ports_in_use(), dict)


# --------------------------------------------------------------------------- #
# _i2c_addrs_in_use()
# --------------------------------------------------------------------------- #

class TestI2cAddrsInUse:
    def test_returns_set(self, rt):
        assert isinstance(rt._i2c_addrs_in_use(), set)

    def test_sim_mode_empty(self, rt):
        """In sim mode no I2C address is in use."""
        assert len(rt._i2c_addrs_in_use()) == 0


# --------------------------------------------------------------------------- #
# GET /api/hw/scan endpoint
# --------------------------------------------------------------------------- #

class TestHwScanEndpoint:
    def test_returns_200(self, client):
        r = client.get("/api/hw/scan")
        assert r.status_code == 200

    def test_response_is_json(self, client):
        r = client.get("/api/hw/scan")
        data = r.json()
        assert isinstance(data, dict)

    def test_response_has_ports_key(self, client):
        r = client.get("/api/hw/scan")
        assert "ports" in r.json()

    def test_response_has_capabilities_key(self, client):
        r = client.get("/api/hw/scan")
        assert "capabilities" in r.json()


# --------------------------------------------------------------------------- #
# POST /api/hw/probe — error paths
# --------------------------------------------------------------------------- #

class TestHwProbeEndpoint:
    def test_missing_target_returns_400(self, client):
        r = client.post("/api/hw/probe", json={"port": "/dev/ttyUSB0"})
        assert r.status_code == 400

    def test_unknown_target_returns_400(self, client):
        r = client.post("/api/hw/probe", json={"target": "bluetooth"})
        assert r.status_code == 400

    def test_i2c_missing_bus_returns_400(self, client):
        r = client.post("/api/hw/probe", json={"target": "i2c", "addr": "0x42"})
        assert r.status_code == 400

    def test_i2c_missing_addr_returns_400(self, client):
        r = client.post("/api/hw/probe", json={"target": "i2c", "bus": 1})
        assert r.status_code == 400

    def test_i2c_addr_out_of_range_returns_400(self, client):
        r = client.post("/api/hw/probe", json={"target": "i2c", "bus": 1, "addr": "0x00"})
        assert r.status_code == 400

    def test_serial_missing_port_returns_400(self, client):
        r = client.post("/api/hw/probe", json={"target": "serial"})
        assert r.status_code == 400

    def test_concurrent_probe_returns_409(self, rt, client):
        """When the lock is held a second probe returns 409."""
        lock = rt._hw_probe_lock
        # Manually hold the lock to simulate a running probe
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(lock.acquire())
            r = client.post("/api/hw/probe",
                            json={"target": "serial", "port": "/dev/ttyUSB0"})
            assert r.status_code == 409
        finally:
            if lock.locked():
                lock.release()
            loop.close()


# --------------------------------------------------------------------------- #
# Demo / readonly mode does NOT expose hw endpoints on allowlist
# --------------------------------------------------------------------------- #

class TestDemoModeHwEndpoints:
    def test_hw_probe_blocked_in_demo_readonly(self):
        """In --demo-readonly mode POST /api/hw/probe returns 403 (not on allowlist).

        GET /api/hw/scan is still accessible (GET requests pass the middleware).
        """
        from vanchor.core.config import DemoConfig

        cfg = load(None)
        cfg.demo = DemoConfig(enabled=True, readonly=True)
        rt_demo = Runtime(cfg)
        app_demo = create_app(rt_demo)
        with TestClient(app_demo) as c:
            # GET scan should pass through (GET is not blocked by demo-readonly)
            r = c.get("/api/hw/scan")
            assert r.status_code == 200
            # POST probe should be blocked (demo-readonly blocks non-allowlisted POSTs)
            r2 = c.post("/api/hw/probe", json={"target": "serial", "port": "/dev/ttyUSB0"})
            assert r2.status_code == 403


# --------------------------------------------------------------------------- #
# JS <-> API contract: grep hwwizard.js for correct/incorrect keys
# --------------------------------------------------------------------------- #

class TestHwwizardJsApiContract:
    """Source-level contract: hwwizard.js must reference the correct API keys
    and must NOT reference the wrong ones that were present before the fix.

    Keys that MUST be present:
      .path        — scan port entries use "path" (not "port")
      .description — scan port entries use "description" (not "label")
      .fields      — suggest_for() nests config keys under "fields"
      "POST"       — set_device_config endpoint is @app.post (not PATCH)
      {"hardware": — POST body must be wrapped in the "hardware" envelope

    Keys that must NOT be present (pre-fix bugs):
      p.port       — was incorrectly reading port entry path via "port"
      s.baudrate   — was reading non-existent top-level "baudrate" from suggest
      "PATCH"      — wrong HTTP method for /api/config/devices
    """

    @classmethod
    def _js_source(cls) -> str:
        import pathlib
        here = pathlib.Path(__file__).parent.parent
        return (here / "src" / "vanchor" / "ui" / "static" / "hwwizard.js").read_text()

    def test_uses_path_key(self):
        assert ".path" in self._js_source(), "hwwizard.js must read p.path (not p.port)"

    def test_uses_description_key(self):
        assert ".description" in self._js_source(), \
            "hwwizard.js must read p.description (not p.label)"

    def test_uses_fields_key(self):
        assert ".fields" in self._js_source(), \
            "hwwizard.js must read s.fields from suggest_for() result"

    def test_uses_post_method(self):
        assert '"POST"' in self._js_source(), \
            'hwwizard.js must use method:"POST" for /api/config/devices'

    def test_uses_hardware_envelope(self):
        assert '{"hardware":' in self._js_source() or "{hardware:" in self._js_source(), \
            'hwwizard.js must wrap POST body in {"hardware": ...}'

    def test_no_wrong_port_key(self):
        # p.port was the pre-fix bug (port entries use "path").
        # We allow "port" as a bare word (used in probe payload, i2c logic etc.)
        # but the specific form "p.port" or "hinted.port" must not appear.
        src = self._js_source()
        assert "p.port" not in src, \
            "hwwizard.js must not read scan entries via p.port (use p.path)"
        assert "hinted.port" not in src, \
            "hwwizard.js must not read hinted.port (use hinted.path)"

    def test_no_s_baudrate(self):
        import re
        # "s.baudrate" must not appear as a standalone expression.
        # "fields.baudrate" is fine (correct fix uses it); we check that the
        # variable "s" specifically is not being read with .baudrate directly.
        # Look for "s.baudrate" preceded by whitespace, "(" or "," — not by "s".
        assert not re.search(r'(?<![a-zA-Z_$])s\.baudrate', self._js_source()), \
            "hwwizard.js must not read s.baudrate (baud is in s.fields.xxx)"

    def test_no_patch_method(self):
        assert '"PATCH"' not in self._js_source(), \
            'hwwizard.js must not use method:"PATCH" (endpoint is POST)'


# --------------------------------------------------------------------------- #
# Save path end-to-end: wizard-shaped POST body lands in config
# --------------------------------------------------------------------------- #

class TestWizardSavePath:
    """POST /api/config/devices with the exact body shape hwwizard.js now
    produces (after the C1/C2/C3 fix) must persist the hardware fields."""

    def test_wizard_shaped_post_persists_gps_fields(self, client, tmp_path):
        """Simulate the exact JSON the fixed wizard sends for a u-blox GPS find."""
        # suggest_for("ublox", ...) → fields: {gps_source, gps_port, gps_baud}
        wizard_body = {
            "hardware": {
                "gps_source": "ublox",
                "gps_port": "/dev/ttyACM0",
                "gps_baud": 38400,
            }
        }
        r = client.post("/api/config/devices", json=wizard_body)
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True

        # Verify the fields landed in the in-memory config via a GET.
        after = client.get("/api/config/devices").json()
        hw = after["hardware"]
        assert hw["gps_source"] == "ublox"
        assert hw["gps_port"] == "/dev/ttyACM0"
        assert hw["gps_baud"] == 38400

    def test_wizard_shaped_post_persists_witmotion_fields(self, client, tmp_path):
        """WitMotion uses hardware.baudrate (not compass_baud) — verify the
        wizard's suggest.fields shape passes through the endpoint correctly."""
        # suggest_for("witmotion-imu", ...) → fields: {compass_source, compass_port, baudrate}
        wizard_body = {
            "hardware": {
                "compass_source": "hwt901b",
                "compass_port": "/dev/ttyUSB1",
                "baudrate": 9600,
            }
        }
        r = client.post("/api/config/devices", json=wizard_body)
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True

        after = client.get("/api/config/devices").json()
        hw = after["hardware"]
        assert hw["compass_source"] == "hwt901b"
        assert hw["compass_port"] == "/dev/ttyUSB1"
        assert hw["baudrate"] == 9600
