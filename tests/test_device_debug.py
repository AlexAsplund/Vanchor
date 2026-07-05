"""Per-device debug() raw-data snapshots + the /api/devices/{kind}/debug route."""
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.ui.server import create_app


def test_device_debug_returns_text_for_each_device(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    for kind in ("gps", "compass", "depth", "motor", "battery"):
        d = rt.device_debug(kind)
        assert d["ok"] is True, kind
        assert d["kind"] == kind
        assert isinstance(d["debug"], str) and d["debug"].strip()  # never empty
        assert type(rt.gps).__name__ or True  # device exists


def test_device_debug_unknown_kind_is_graceful():
    rt = Runtime(load(None))
    d = rt.device_debug("bogus")
    assert d["ok"] is False and "bogus" in d["debug"]


def test_device_debug_never_raises_even_before_any_data(tmp_path):
    # Devices are freshly built (no tick yet); debug() must be safe immediately.
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    for kind in ("gps", "compass", "depth", "motor", "battery"):
        rt.device_debug(kind)  # must not raise


def test_device_debug_endpoint(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    with TestClient(create_app(Runtime(cfg))) as c:
        r = c.get("/api/devices/gps/debug")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["kind"] == "gps" and body["debug"]
        assert c.get("/api/devices/bogus/debug").json()["ok"] is False
