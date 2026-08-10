"""The /api/relay/{id} result endpoint (client answers a fetch_request)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.ui.server import create_app


def test_relay_result_unknown_id_reports_unmatched(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    with TestClient(create_app(Runtime(cfg))) as c:
        # No pending request -> matched false (a late/duplicate client answer).
        r = c.post("/api/relay/nope?ok=1", content=b"data")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "matched": False}
        # Error-shaped answer for an unknown id is equally ignored.
        r = c.post("/api/relay/nope?ok=0", content=b"HTTP 429")
        assert r.json() == {"ok": True, "matched": False}


def test_runtime_gets_a_fetch_relay(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    with TestClient(create_app(rt)) as c:  # noqa: F841 - lifespan binds the loop
        assert getattr(rt, "fetch_relay", None) is not None
        assert rt.fetch_relay.offline is False


def test_relay_end_to_end_over_websocket(tmp_path):
    """Full pipeline: an offline server relays a fetch to a WS client, the
    client answers on POST /api/relay/<id>, and the (executor-side) fetch_sync
    caller gets the bytes."""
    import json
    import threading

    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    with TestClient(create_app(rt)) as c:
        relay = rt.fetch_relay

        async def failing_direct(url, **kw):        # the boat has no internet
            raise ConnectionError("network unreachable")
        relay._direct = failing_direct

        result: dict = {}

        def worker():                               # the route planner's thread
            try:
                result["data"] = relay.fetch_sync("http://overpass/api",
                                                  method="POST", body=b"data=q")
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc

        with c.websocket_connect("/ws") as ws:
            t = threading.Thread(target=worker)
            t.start()
            req = None
            for _ in range(50):                     # skip role/telemetry frames
                msg = json.loads(ws.receive_text())
                if msg.get("type") == "fetch_request":
                    req = msg
                    break
            assert req is not None, "no fetch_request arrived on the WS"
            assert req["url"] == "http://overpass/api"
            assert req["method"] == "POST" and "body_b64" in req
            # The client (this test) fetches with ITS internet and answers.
            r = c.post(f"/api/relay/{req['id']}?ok=1", content=b'{"elements":[]}')
            assert r.json() == {"ok": True, "matched": True}
            t.join(timeout=10)
        assert result.get("data") == b'{"elements":[]}'
        assert relay.offline is True                # circuit breaker tripped
