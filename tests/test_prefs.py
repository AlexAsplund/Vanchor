"""Tests for server-persisted safety geometry + the UI-preferences KV store (#23).

The browser is a CACHE, not the source of truth: no-go zones, min-depth and the
fix-failsafe are persisted server-side (``<data_dir>/safety.json``) and applied
to the SafetyGovernor at startup, so a Pi restart with NO client connected keeps
them. UI prefs get a generic ``GET/PUT /api/prefs`` KV store.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.core.prefs import PrefsStore, SafetyGeometryStore, _atomic_write_json
from vanchor.ui.server import create_app

# A simple square ring [[lat, lon], ...] (valid, >= 3 points).
SQUARE = [[59.0, 18.0], [59.0, 18.001], [59.001, 18.001], [59.001, 18.0]]


def _runtime(tmp_path) -> Runtime:
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    return Runtime(cfg)


# --------------------------------------------------------------------------- #
# SafetyGeometryStore -- atomic persistence
# --------------------------------------------------------------------------- #
def test_geometry_store_roundtrip_and_atomic(tmp_path):
    store = SafetyGeometryStore(str(tmp_path))
    assert store.nogo_zones == []
    assert store.min_depth_m is None
    assert store.fix_failsafe_enabled is None

    store.set_nogo_zones([SQUARE])
    store.set_min_depth(1.5)
    store.set_fix_failsafe(True)

    path = tmp_path / "safety.json"
    assert path.exists()
    # Atomic write leaves no lingering temp file.
    assert not (tmp_path / "safety.json.tmp").exists()

    # A fresh store on the same dir sees exactly what was written.
    reloaded = SafetyGeometryStore(str(tmp_path))
    assert reloaded.nogo_zones == [SQUARE]
    assert reloaded.min_depth_m == 1.5
    assert reloaded.fix_failsafe_enabled is True


def test_geometry_store_drops_degenerate_rings(tmp_path):
    store = SafetyGeometryStore(str(tmp_path))
    store.set_nogo_zones([SQUARE, [[1.0, 2.0], [3.0, 4.0]], "junk"])
    assert store.nogo_zones == [SQUARE]


def test_atomic_write_uses_replace(tmp_path, monkeypatch):
    path = str(tmp_path / "x.json")
    calls = {"n": 0}
    real_replace = os.replace

    def spy(src, dst):
        calls["n"] += 1
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    _atomic_write_json(path, {"a": 1})
    assert calls["n"] == 1
    assert json.loads(open(path).read()) == {"a": 1}
    assert not os.path.exists(path + ".tmp")


# --------------------------------------------------------------------------- #
# Persistence across a simulated restart (the core requirement)
# --------------------------------------------------------------------------- #
def test_geometry_survives_restart_without_a_client(tmp_path):
    rt = _runtime(tmp_path)
    # Set geometry via the SAME command path the UI uses.
    rt.handle_command({"type": "set_nogo_zones", "zones": [SQUARE]})
    rt.handle_command({"type": "set_min_depth", "min_depth_m": 2.0})
    rt.handle_command({"type": "set_fix_failsafe", "enabled": True})
    assert rt.controller.safety.nogo_zone_count == 1
    assert rt.controller.safety.config.min_depth_m == 2.0
    assert rt.controller.safety.config.fix_failsafe_enabled is True

    # "Restart": drop the runtime, build a fresh one on the same data_dir, with
    # NO client ever connecting. The governor must come up already carrying the
    # persisted geometry.
    del rt
    rt2 = _runtime(tmp_path)
    assert rt2.controller.safety.nogo_zone_count == 1
    assert rt2.controller.safety.config.min_depth_m == 2.0
    assert rt2.controller.safety.config.fix_failsafe_enabled is True
    # And the raw rings are preserved for re-drawing.
    assert rt2.safety_geometry.nogo_zones == [SQUARE]


def test_fresh_install_leaves_config_defaults(tmp_path):
    # No safety.json -> min-depth / failsafe stay at the config defaults (the
    # store's None values must not clobber them).
    rt = _runtime(tmp_path)
    assert rt.safety_geometry.min_depth_m is None
    assert rt.safety_geometry.fix_failsafe_enabled is None
    assert rt.controller.safety.config.fix_failsafe_enabled is (
        rt.config.safety.fix_failsafe_enabled
    )


# --------------------------------------------------------------------------- #
# Telemetry carries the server geometry
# --------------------------------------------------------------------------- #
def test_telemetry_exposes_safety_geometry(tmp_path):
    rt = _runtime(tmp_path)
    rt.handle_command({"type": "set_nogo_zones", "zones": [SQUARE]})
    rt.handle_command({"type": "set_min_depth", "min_depth_m": 3.5})
    tele = rt.telemetry()
    assert "safety_geometry" in tele
    g = tele["safety_geometry"]
    assert g["nogo_zones"] == [SQUARE]
    assert g["min_depth_m"] == 3.5
    # Governor is the live authority for min-depth/failsafe.
    assert g["fix_failsafe_enabled"] == rt.controller.safety.config.fix_failsafe_enabled


# --------------------------------------------------------------------------- #
# PrefsStore + /api/prefs
# --------------------------------------------------------------------------- #
def test_prefs_store_merge_is_shallow_and_atomic(tmp_path):
    store = PrefsStore(str(tmp_path))
    assert store.get() == {}
    store.merge({"hud": {"battery": True}, "basemap": "osm"})
    store.merge({"basemap": "sat"})  # replaces the top-level key
    assert store.get() == {"hud": {"battery": True}, "basemap": "sat"}
    assert not (tmp_path / "prefs.json.tmp").exists()
    # Durable: a fresh store reads it back.
    assert PrefsStore(str(tmp_path)).get() == {"hud": {"battery": True}, "basemap": "sat"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VANCHOR_ALLOWED_HOSTS", "testserver")
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    with TestClient(create_app(Runtime(cfg))) as c:
        yield c


def test_prefs_endpoint_roundtrip_and_merge(client):
    assert client.get("/api/prefs").json() == {}
    r = client.put("/api/prefs", json={"theme": "dark", "units": "metric"})
    assert r.status_code == 200
    assert r.json() == {"theme": "dark", "units": "metric"}
    # A second PUT merges (adds/overrides keys, keeps the rest).
    r = client.put("/api/prefs", json={"units": "imperial", "zoom": 12})
    assert r.json() == {"theme": "dark", "units": "imperial", "zoom": 12}
    # GET reflects the persisted state.
    assert client.get("/api/prefs").json() == {"theme": "dark", "units": "imperial", "zoom": 12}


def test_geometry_command_over_api_persists(client, tmp_path):
    # The command path used by the WS/HTTP command endpoint persists geometry.
    r = client.post("/api/command", json={"type": "set_nogo_zones", "zones": [SQUARE]})
    assert r.status_code == 200
    # safety.json exists on disk with the ring.
    data = json.loads((tmp_path / "safety.json").read_text())
    assert data["nogo_zones"] == [SQUARE]
    # And the live /api/state telemetry carries it.
    assert client.get("/api/state").json()["safety_geometry"]["nogo_zones"] == [SQUARE]


# --------------------------------------------------------------------------- #
# Echo-loop guard (documented at the logic level)
# --------------------------------------------------------------------------- #
def test_adopting_server_geometry_does_not_re_persist(tmp_path):
    """The server->client->server echo loop is prevented in safety.js:
    ``adoptServerGeometry`` only updates local state + the localStorage cache and
    NEVER calls send(); a value-equality guard (``zonesEqual``) also skips the
    redraw when the server's zones already match. This test documents the
    server-side invariant that makes that safe: re-applying an IDENTICAL geometry
    is idempotent -- the persisted file is byte-stable, so even if a client did
    re-send, no divergence accumulates."""
    rt = _runtime(tmp_path)
    rt.handle_command({"type": "set_nogo_zones", "zones": [SQUARE]})
    first = (tmp_path / "safety.json").read_text()
    # Re-apply the very same geometry (as an echo would): file is unchanged.
    rt.handle_command({"type": "set_nogo_zones", "zones": [SQUARE]})
    assert (tmp_path / "safety.json").read_text() == first
    assert rt.telemetry()["safety_geometry"]["nogo_zones"] == [SQUARE]
