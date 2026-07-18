"""Endpoint tests for /api/push/* (Adoption #7 — Web Push).

Uses TestClient against a real Runtime (no depth-data triggers since we only
call REST endpoints, not the WebSocket stream).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.ui.server import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Standard test client fixture (mirrored from tests/test_api.py)."""
    monkeypatch.setenv("VANCHOR_ALLOWED_HOSTS", "testserver")
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    app = create_app(Runtime(cfg))
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Status shape
# ---------------------------------------------------------------------------

def test_status_shape(client, tmp_path):
    r = client.get("/api/push/status")
    assert r.status_code == 200
    body = r.json()
    for key in ("available", "enabled", "keys_exist", "subscriptions"):
        assert key in body, f"missing key: {key}"
    # Fresh data dir: no keys generated yet.
    assert body["keys_exist"] is False


# ---------------------------------------------------------------------------
# 2. pubkey generates keys
# ---------------------------------------------------------------------------

def test_pubkey_generates(tmp_path, monkeypatch):
    pytest.importorskip("py_vapid")
    monkeypatch.setenv("VANCHOR_ALLOWED_HOSTS", "testserver")
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    app = create_app(Runtime(cfg))
    with TestClient(app) as c:
        r = c.get("/api/push/pubkey")
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert "public_key" in body
    assert (tmp_path / "push" / "vapid_private.pem").exists()

    # Status now shows keys_exist = True.
    app2 = create_app(Runtime(cfg))
    with TestClient(app2) as c2:
        r2 = c2.get("/api/push/status")
    assert r2.json()["keys_exist"] is True


# ---------------------------------------------------------------------------
# 3. Subscribe / unsubscribe round-trip
# ---------------------------------------------------------------------------

def _fake_sub(endpoint: str = "https://push.test/x") -> dict:
    return {
        "endpoint": endpoint,
        "keys": {
            "p256dh": "BQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "auth": "AAAAAAAAAAA",
        },
    }


def test_subscribe_unsubscribe_roundtrip(client):
    # Subscribe.
    r = client.post("/api/push/subscribe", json={"subscription": _fake_sub(), "ua": "pytest"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["count"] == 1

    # Status shows 1 subscription.
    r2 = client.get("/api/push/status")
    assert r2.json()["subscriptions"] == 1

    # Unsubscribe.
    r3 = client.post("/api/push/unsubscribe", json={"endpoint": "https://push.test/x"})
    assert r3.status_code == 200
    assert r3.json()["count"] == 0

    # Malformed subscribe (no endpoint).
    r4 = client.post("/api/push/subscribe", json={"subscription": {"keys": {}}, "ua": "test"})
    assert r4.status_code == 200
    assert r4.json()["ok"] is False


# ---------------------------------------------------------------------------
# 4. Push disabled in config
# ---------------------------------------------------------------------------

def test_push_disabled_config(tmp_path, monkeypatch):
    monkeypatch.setenv("VANCHOR_ALLOWED_HOSTS", "testserver")
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    cfg.push.enabled = False
    app = create_app(Runtime(cfg))
    with TestClient(app) as c:
        # Subscribe returns ok: False.
        r = c.post("/api/push/subscribe",
                   json={"subscription": _fake_sub(), "ua": "test"})
        assert r.status_code == 200
        assert r.json()["ok"] is False

        # Status shows enabled: False.
        r2 = c.get("/api/push/status")
        assert r2.status_code == 200
        assert r2.json()["enabled"] is False


# ---------------------------------------------------------------------------
# 5. Test endpoint with zero subscriptions
# ---------------------------------------------------------------------------

def test_test_endpoint_no_subs(client):
    r = client.post("/api/push/test", json={})
    assert r.status_code == 200
    body = r.json()
    # Must not 500; either ok:False or sent:0.
    assert not body.get("ok") or body.get("sent", 0) == 0
