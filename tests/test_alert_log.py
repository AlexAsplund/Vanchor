"""Tests for vanchor.core.alertlog.AlertLog (Task 1 D8).

Covers the public API (record, snapshot, clear), JSON persistence,
corrupt-file tolerance, and the max_entries cap.
Tests use a tmp_path fixture so disk writes are isolated.
"""
import json
import time

import pytest

from vanchor.core.alertlog import AlertLog


# ---- helpers ----------------------------------------------------------------

def _log(path, **kw):
    """Create an AlertLog with a short debounce for tests."""
    al = AlertLog(path, max_entries=10)
    return al


# ---- basic record / snapshot ------------------------------------------------

def test_record_returns_entry():
    al = AlertLog(None)
    e = al.record("alarm", "test message")
    assert e["severity"] == "alarm"
    assert e["message"] == "test message"
    assert isinstance(e["ts"], int)


def test_snapshot_empty_on_init():
    al = AlertLog(None)
    assert al.snapshot() == []


def test_snapshot_reflects_records():
    al = AlertLog(None)
    al.record("warn", "first")
    al.record("info", "second")
    snap = al.snapshot()
    assert len(snap) == 2
    assert snap[0]["message"] == "first"
    assert snap[1]["message"] == "second"


def test_clear_empties_log():
    al = AlertLog(None)
    al.record("alarm", "boom")
    al.clear()
    assert al.snapshot() == []


# ---- severity normalisation ------------------------------------------------

def test_known_severities():
    al = AlertLog(None)
    for sev in ("info", "warn", "alarm"):
        e = al.record(sev, "msg")
        assert e["severity"] == sev


def test_unknown_severity_defaults_to_info():
    al = AlertLog(None)
    e = al.record("critical", "msg")
    assert e["severity"] == "info"


# ---- optional fields -------------------------------------------------------

def test_kind_included_when_given():
    al = AlertLog(None)
    e = al.record("warn", "sonar", kind="depth")
    assert e["kind"] == "depth"


def test_kind_absent_when_empty():
    al = AlertLog(None)
    e = al.record("info", "msg")
    assert "kind" not in e


def test_lat_lon_included():
    al = AlertLog(None)
    e = al.record("alarm", "drag", lat=60.1, lon=24.9)
    assert abs(e["lat"] - 60.1) < 1e-9
    assert abs(e["lon"] - 24.9) < 1e-9


# ---- max_entries cap -------------------------------------------------------

def test_cap_drops_oldest():
    al = AlertLog(None, max_entries=3)
    for i in range(5):
        al.record("info", f"msg{i}")
    snap = al.snapshot()
    assert len(snap) == 3
    assert snap[0]["message"] == "msg2"
    assert snap[-1]["message"] == "msg4"


# ---- JSON persistence ------------------------------------------------------

def test_persist_and_reload(tmp_path):
    al = AlertLog(tmp_path)
    al.record("alarm", "drag alarm", kind="anchor", lat=60.0, lon=25.0)
    # Force a flush.
    al._last_write = 0.0
    al.record("warn", "low battery")

    # Reload from disk.
    al2 = AlertLog(tmp_path)
    snap = al2.snapshot()
    assert len(snap) == 2
    assert snap[0]["message"] == "drag alarm"
    assert snap[0]["kind"] == "anchor"
    assert abs(snap[0]["lat"] - 60.0) < 1e-9
    assert snap[1]["severity"] == "warn"


def test_clear_flushes_empty_file(tmp_path):
    al = AlertLog(tmp_path)
    al.record("info", "something")
    al._last_write = 0.0
    al.record("info", "something else")
    al.clear()

    # File should exist and contain an empty alerts list.
    f = tmp_path / "alerts.json"
    assert f.exists()
    data = json.loads(f.read_text())
    assert data["alerts"] == []

    # Reload should be empty.
    al2 = AlertLog(tmp_path)
    assert al2.snapshot() == []


# ---- corrupt-file tolerance ------------------------------------------------

def test_corrupt_file_starts_fresh(tmp_path):
    f = tmp_path / "alerts.json"
    f.write_text("not valid json", encoding="utf-8")
    al = AlertLog(tmp_path)  # should not raise
    assert al.snapshot() == []


def test_wrong_structure_starts_fresh(tmp_path):
    f = tmp_path / "alerts.json"
    f.write_text('{"alerts": "not a list"}', encoding="utf-8")
    al = AlertLog(tmp_path)
    assert al.snapshot() == []


def test_malformed_entry_filtered(tmp_path):
    f = tmp_path / "alerts.json"
    f.write_text(
        json.dumps({"alerts": [{"severity": "info"}, {"ts": 1, "severity": "warn", "message": "ok"}]}),
        encoding="utf-8",
    )
    al = AlertLog(tmp_path)
    snap = al.snapshot()
    # Only the entry with a message string should survive.
    assert len(snap) == 1
    assert snap[0]["message"] == "ok"


# ---- in-memory (path=None) -------------------------------------------------

def test_in_memory_no_disk_write():
    al = AlertLog(None)
    al.record("alarm", "test")
    al.clear()
    # No error should occur; path is None so no file ops attempted.
    assert al.snapshot() == []


# ---- REST endpoints + Runtime rehydration ----------------------------------
# Endpoints are exercised for snapshot/clear ONLY -- never to drive telemetry
# loops (TestClient(Runtime()) can spin on depth data; see project memory).

def _client(tmp_path):
    from fastapi.testclient import TestClient

    from vanchor.app import Runtime
    from vanchor.core.config import load
    from vanchor.ui.server import create_app

    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    return rt, TestClient(create_app(rt))


def test_get_alerts_endpoint(tmp_path):
    rt, c = _client(tmp_path)
    with c:
        r = c.get("/api/alerts")
        assert r.status_code == 200
        assert r.json() == {"alerts": []}
        rt.alert_log.record("alarm", "drag alarm", kind="drag", lat=59.0, lon=18.0)
        r = c.get("/api/alerts")
        alerts = r.json()["alerts"]
        assert len(alerts) == 1
        assert alerts[0]["message"] == "drag alarm"
        assert alerts[0]["kind"] == "drag"


def test_clear_alerts_endpoint(tmp_path):
    rt, c = _client(tmp_path)
    with c:
        rt.alert_log.record("warn", "low battery")
        r = c.post("/api/alerts/clear")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert rt.alert_log.snapshot() == []
        assert c.get("/api/alerts").json() == {"alerts": []}


def test_runtime_restart_rehydrates(tmp_path):
    """A new Runtime on the same data_dir loads the persisted alerts.json."""
    from vanchor.app import Runtime
    from vanchor.core.config import load

    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    rt.alert_log.record("alarm", "anchor drag", kind="drag")
    rt.alert_log._last_write = 0.0
    rt.alert_log.record("warn", "battery low", kind="battery")

    cfg2 = load(None)
    cfg2.data_dir = str(tmp_path)
    rt2 = Runtime(cfg2)
    snap = rt2.alert_log.snapshot()
    assert [e["message"] for e in snap] == ["anchor drag", "battery low"]
