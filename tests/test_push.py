"""Unit tests for src/vanchor/push.py (PushService + watcher).

Uses direct Runtime method calls — NOT TestClient — to avoid depth-data hangs.
"""
from __future__ import annotations

import json
import sys
import time
import types

import pytest

from vanchor.core.config import AppConfig, PushConfig
from vanchor.push import PushService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sub(endpoint: str = "https://push.test/x") -> dict:
    """Return a structurally-valid fake subscription dict."""
    return {
        "endpoint": endpoint,
        "keys": {
            "p256dh": "BQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "auth": "AAAAAAAAAAA",
        },
    }


class FakeSession:
    """requests.Session stand-in captured by pywebpush via requests_session."""

    def __init__(self, status: int = 201):
        self.calls: list[tuple] = []
        self.status = status

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        r = types.SimpleNamespace(
            status_code=self.status,
            ok=self.status < 400,
            text="",
            headers={},
        )
        return r


# ---------------------------------------------------------------------------
# 1. Store round-trip
# ---------------------------------------------------------------------------

def test_store_roundtrip(tmp_path):
    svc = PushService(str(tmp_path), PushConfig())
    sub = _make_sub()
    r = svc.add_subscription(sub, ua="TestBrowser/1.0")
    assert r["ok"] is True
    assert r["count"] == 1

    # Persists: a fresh instance loads it.
    svc2 = PushService(str(tmp_path), PushConfig())
    assert svc2.subscription_count() == 1

    # Re-subscribing with same endpoint is an upsert (count stays 1).
    r2 = svc.add_subscription(sub, ua="TestBrowser/2.0")
    assert r2["ok"] is True
    assert r2["count"] == 1

    # Remove.
    removed = svc.remove_subscription(sub["endpoint"])
    assert removed is True
    assert svc.subscription_count() == 0
    svc3 = PushService(str(tmp_path), PushConfig())
    assert svc3.subscription_count() == 0

    # Malformed sub: no endpoint.
    bad = {"keys": {"p256dh": "abc", "auth": "def"}}
    r3 = svc.add_subscription(bad)
    assert r3["ok"] is False
    assert "malformed" in r3["error"]

    # Malformed sub: http endpoint.
    r4 = svc.add_subscription({"endpoint": "http://push.test/x",
                                "keys": {"p256dh": "a", "auth": "b"}})
    assert r4["ok"] is False

    # Corrupt file loads as empty.
    (tmp_path / "push" / "subscriptions.json").write_text("{")
    svc4 = PushService(str(tmp_path), PushConfig())
    assert svc4.subscription_count() == 0


# ---------------------------------------------------------------------------
# 2. Subscription cap
# ---------------------------------------------------------------------------

def test_subscription_cap(tmp_path):
    svc = PushService(str(tmp_path), PushConfig())
    for i in range(17):
        svc.add_subscription(_make_sub(f"https://push.test/{i}"))
    assert svc.subscription_count() == 16


# ---------------------------------------------------------------------------
# 3. Unavailable without extra
# ---------------------------------------------------------------------------

def test_unavailable_without_extra(monkeypatch, tmp_path):
    # Simulate missing library by setting the module to None.
    monkeypatch.setitem(sys.modules, "pywebpush", None)
    monkeypatch.setitem(sys.modules, "py_vapid", None)

    svc = PushService(str(tmp_path), PushConfig())
    assert svc.available is False
    reason = svc.unavailable_reason
    assert reason is not None
    assert "vanchor-ng[push]" in reason

    # notify() must return False and not raise.
    assert svc.notify("test", "T", "B") is False

    # status() must not raise and must carry reason.
    s = svc.status()
    assert s["available"] is False
    assert s["reason"] is not None
    assert "vanchor-ng[push]" in s["reason"]


# ---------------------------------------------------------------------------
# 4. VAPID key generation
# ---------------------------------------------------------------------------

def test_vapid_generation(tmp_path):
    pytest.importorskip("py_vapid")
    svc = PushService(str(tmp_path), PushConfig())
    key1 = svc.public_key()
    assert isinstance(key1, str)
    # Uncompressed P-256 point: 65 raw bytes -> 87 b64url chars (no padding).
    assert len(key1) == 87
    # Key file created with restrictive perms on POSIX.
    key_path = tmp_path / "push" / "vapid_private.pem"
    assert key_path.exists()
    import stat
    mode = key_path.stat().st_mode & 0o777
    assert mode == 0o600

    # Second call returns the same key.
    svc2 = PushService(str(tmp_path), PushConfig())
    key2 = svc2.public_key()
    assert key1 == key2


# ---------------------------------------------------------------------------
# 5. Send and prune
# ---------------------------------------------------------------------------

def test_send_prune_with_fake_session(tmp_path, monkeypatch):
    """Verify send_now with FakeSession(201) sends, and (410) prunes."""
    pywebpush = pytest.importorskip("pywebpush")
    pytest.importorskip("py_vapid")

    svc = PushService(str(tmp_path), PushConfig(), transport=FakeSession(201))
    svc.add_subscription(_make_sub())
    # Generate keys so _key_path exists.
    svc.public_key()

    # Monkeypatch _send_one to skip the real ECE encryption but exercise prune.
    sent_payloads = []

    def fake_send_one(self_inner, sub, payload):
        sent_payloads.append(payload)
        return True, None

    monkeypatch.setattr(PushService, "_send_one", fake_send_one)

    result = svc.send_now("test", "Title", "Body")
    assert result["ok"] is True
    assert result["sent"] == 1
    assert result["failed"] == 0
    assert len(sent_payloads) == 1

    # Now test 410 prune via the real _send_one path with a fake session
    # returning 410.  Monkeypatch the webpush call to raise WebPushException
    # with status_code 410.
    svc2 = PushService(str(tmp_path), PushConfig())
    svc2.add_subscription(_make_sub())
    svc2.public_key()

    class FakeResponse:
        status_code = 410

    class FakeWebPushException(Exception):
        def __init__(self):
            super().__init__("Gone")
            self.response = FakeResponse()

    def fake_webpush(**kwargs):
        raise FakeWebPushException()

    import importlib
    import unittest.mock as mock

    with mock.patch("pywebpush.webpush", side_effect=FakeWebPushException()):
        result2 = svc2.send_now("test", "Title", "Body")

    assert svc2.subscription_count() == 0
    assert result2["failed"] == 1


# ---------------------------------------------------------------------------
# 6. Rate limit per-kind
# ---------------------------------------------------------------------------

def test_rate_limit_per_kind(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pywebpush", types.ModuleType("pywebpush"))
    monkeypatch.setitem(sys.modules, "py_vapid", types.ModuleType("py_vapid"))

    now = [0.0]
    cfg = PushConfig(min_interval_s=30.0)
    svc = PushService(str(tmp_path), cfg, now_fn=lambda: now[0])
    svc._available = True  # bypass import check

    svc.add_subscription(_make_sub())

    # First notify: accepted but can't actually send (no key).
    # We just check the return value and rate-limit state.
    r1 = svc.notify("depth", "D", "B")
    assert r1 is True

    # 1 second later: rejected (below 30 s interval).
    now[0] = 1.0
    r2 = svc.notify("depth", "D", "B")
    assert r2 is False

    # 31 seconds later: accepted again.
    now[0] = 31.1
    r3 = svc.notify("depth", "D", "B")
    assert r3 is True


# ---------------------------------------------------------------------------
# 7. Burst cap
# ---------------------------------------------------------------------------

def test_burst_cap(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pywebpush", types.ModuleType("pywebpush"))
    monkeypatch.setitem(sys.modules, "py_vapid", types.ModuleType("py_vapid"))

    now = [0.0]
    cfg = PushConfig(burst_limit=10, burst_window_s=60.0, min_interval_s=0.0)
    svc = PushService(str(tmp_path), cfg, now_fn=lambda: now[0])
    svc._available = True

    svc.add_subscription(_make_sub())

    kinds = [f"kind_{i}" for i in range(11)]
    results = []
    for i, k in enumerate(kinds):
        now[0] = float(i)
        results.append(svc.notify(k, "T", "B"))

    assert results[:10] == [True] * 10
    assert results[10] is False


# ---------------------------------------------------------------------------
# 8. Watcher edge trigger
# ---------------------------------------------------------------------------

def test_watchers_edge_trigger(tmp_path):
    from vanchor.app import Runtime

    rt = Runtime(AppConfig(data_dir=str(tmp_path)))
    calls = []
    rt.push.notify = lambda kind, title, body, **kw: calls.append(kind)

    # drag alarm: False -> True -> stays True (only one notify).
    rt.controller.safety_status.drag_alarm = True
    rt.evaluate_push_alerts()
    rt.evaluate_push_alerts()
    anchor_drag_calls = [c for c in calls if c == "anchor_drag"]
    assert len(anchor_drag_calls) == 1

    # Clear and re-arm: second edge triggers a new call.
    calls.clear()
    rt.controller.safety_status.drag_alarm = False
    rt._push_prev["drag"] = False
    rt.evaluate_push_alerts()
    rt.controller.safety_status.drag_alarm = True
    rt.evaluate_push_alerts()
    assert calls.count("anchor_drag") == 1

    # Link failsafe stop action.
    calls.clear()
    rt._link_failsafe_engaged = True
    rt._link_failsafe_action = "stop"
    rt._push_prev["link"] = False
    rt.evaluate_push_alerts()
    assert "link" in calls

    # Battery RTL recommend.
    calls.clear()
    rt.state.rtl_recommended = True
    rt._push_prev["battery_rtl"] = False
    rt.evaluate_push_alerts()
    assert "battery" in calls

    # Depth divergence.
    calls.clear()
    rt.state.depth_divergence_alert = True
    rt._push_prev["diverge"] = False
    rt.evaluate_push_alerts()
    assert "depth" in calls


# ---------------------------------------------------------------------------
# 9. Watcher battery step
# ---------------------------------------------------------------------------

def test_watcher_battery_step(tmp_path):
    from vanchor.app import Runtime

    rt = Runtime(AppConfig(data_dir=str(tmp_path)))
    calls = []
    rt.push.notify = lambda kind, title, body, **kw: calls.append((kind, body))

    # Thrust cap drops -> battery notify.
    rt.controller.safety.set_thrust_cap(0.6)
    rt._push_prev_cap = 1.0
    rt.evaluate_push_alerts()
    battery_calls = [(k, b) for k, b in calls if k == "battery"]
    assert len(battery_calls) >= 1
    assert "60%" in battery_calls[0][1]

    # Same cap: no new notify.
    calls.clear()
    rt._push_prev_cap = 0.6
    rt.evaluate_push_alerts()
    assert not any(k == "battery" for k, _ in calls)

    # Cap recovers -> no notify.
    calls.clear()
    rt.controller.safety.set_thrust_cap(1.0)
    rt._push_prev_cap = 0.6
    rt.evaluate_push_alerts()
    assert not any(k == "battery" for k, _ in calls)

    # Drops again -> notify.
    calls.clear()
    rt.controller.safety.set_thrust_cap(0.6)
    rt._push_prev_cap = 1.0
    rt.evaluate_push_alerts()
    assert any(k == "battery" for k, _ in calls)


# ---------------------------------------------------------------------------
# 10. Supervisor contains push step
# ---------------------------------------------------------------------------

def test_supervisor_contains_push_step(tmp_path):
    from vanchor.app import Runtime

    called = []
    rt = Runtime(AppConfig(data_dir=str(tmp_path)))
    original = rt.evaluate_push_alerts

    def stub():
        called.append(True)
        original()

    rt.evaluate_push_alerts = stub
    rt._supervise_once()
    assert called, "evaluate_push_alerts was not called by _supervise_once"


# ---------------------------------------------------------------------------
# 11. notify is non-blocking; stop() joins cleanly
# ---------------------------------------------------------------------------

def test_notify_nonblocking_and_stop(tmp_path, monkeypatch):
    pytest.importorskip("pywebpush")
    pytest.importorskip("py_vapid")

    import threading

    barrier = threading.Event()

    class SlowSession:
        calls = []

        def post(self, url, **kwargs):
            SlowSession.calls.append(url)
            barrier.wait(timeout=5.0)
            return types.SimpleNamespace(
                status_code=201, ok=True, text="", headers={}
            )

    svc = PushService(str(tmp_path), PushConfig(), transport=SlowSession())
    svc._available = True
    svc.add_subscription(_make_sub())
    svc.public_key()

    # Patch _send_one so no real network occurs.
    real_send = svc._send_one

    def fast_send(sub, payload):
        barrier.wait(timeout=5.0)  # simulate 0.2 s network delay in worker
        return True, None

    svc._send_one = fast_send

    t0 = time.monotonic()
    # notify should return nearly instantly (enqueue only).
    result = svc.notify("test", "T", "B")
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, f"notify took {elapsed:.3f}s — should be sub-50 ms"
    assert result is True

    barrier.set()  # unblock the worker so stop() can join cleanly
    t0 = time.monotonic()
    svc.stop()
    stop_elapsed = time.monotonic() - t0
    assert stop_elapsed < 3.0, f"stop() took {stop_elapsed:.2f}s — exceeded 2 s join timeout"
