"""u-blox toolbox HTTP endpoints (the pure/validation paths -- the serial ones
need hardware and are covered at the service layer in test_ublox_tools.py)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.ui.server import create_app


def _client(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    return TestClient(create_app(Runtime(cfg)))


def test_marine_config_endpoint_lists_the_warning(tmp_path):
    with _client(tmp_path) as c:
        data = c.get("/api/tools/ublox/marine-config").json()
        joined = " ".join(data["summary"]).lower()
        assert "nmea" in joined and "off" in joined      # warns NMEA gets turned off
        assert any("10 hz" in s.lower() for s in data["summary"])


def test_apply_requires_a_port(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/tools/ublox/apply", json={"nmea": True})
        assert r.status_code == 400 and r.json()["ok"] is False


def test_nmea_messages_post_validation(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/tools/ublox/nmea-messages", json={"rates": {"RMC": 1}})
        assert r.status_code == 400                      # port required
        r = c.post("/api/tools/ublox/nmea-messages", json={"port": "/dev/x"})
        assert r.status_code == 400                      # rates required
        r = c.post("/api/tools/ublox/nmea-messages",
                   json={"port": "/dev/x", "rates": {"ZDA": 1}})
        assert r.status_code == 400                      # unknown sentence
        assert "ZDA" in r.json()["error"]
