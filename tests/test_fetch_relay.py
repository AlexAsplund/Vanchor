"""FetchRelay: direct-first, sticky-offline fallback to a client relay."""
from __future__ import annotations

import asyncio

import pytest

from vanchor.runtime.fetch_relay import FetchRelay, FetchRelayError


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t


def _relay(*, direct=None, clients=True, clock=None, ids=None, **kw):
    sent: list[dict] = []

    async def broadcast(msg):
        sent.append(msg)

    r = FetchRelay(
        broadcast=broadcast,
        has_clients=lambda: clients,
        direct_fetch=direct,
        clock=(clock.now if clock else None),
        new_id=(lambda: ids.pop(0)) if ids else (lambda: "rid"),
        offline_ttl_s=100.0,
        relay_timeout_s=0.2,
        **kw,
    )
    r.sent = sent  # type: ignore[attr-defined]
    return r


async def test_direct_fetch_wins_when_online():
    async def direct(url, **kw):
        return b"from-server"
    r = _relay(direct=direct)
    assert await r.fetch("http://x") == b"from-server"
    assert r.sent == []          # never relayed
    assert r.offline is False


async def test_direct_failure_trips_offline_then_relays():
    calls = {"direct": 0}
    clk = Clock()

    async def direct(url, **kw):
        calls["direct"] += 1
        raise ConnectionError("no route to host")

    r = _relay(direct=direct, clients=True, clock=clk, ids=["a", "b"])
    # First fetch: direct fails -> trips offline -> relays; a client answers.
    task = asyncio.ensure_future(r.fetch("http://tiles/1"))
    await asyncio.sleep(0.02)
    assert r.offline is True
    assert r.sent[-1]["type"] == "fetch_request" and r.sent[-1]["id"] == "a"
    assert r.resolve("a", ok=True, data=b"tile1")
    assert await task == b"tile1"

    # Second fetch (same batch): still offline -> goes STRAIGHT to relay, does
    # NOT re-try the direct path.
    task2 = asyncio.ensure_future(r.fetch("http://tiles/2"))
    await asyncio.sleep(0.02)
    assert calls["direct"] == 1          # direct not attempted again
    assert r.sent[-1]["id"] == "b"
    r.resolve("b", ok=True, data=b"tile2")
    assert await task2 == b"tile2"


async def test_relay_with_no_client_raises_clear_error():
    async def direct(url, **kw):
        raise ConnectionError("offline")
    r = _relay(direct=direct, clients=False)
    with pytest.raises(FetchRelayError) as ei:
        await r.fetch("http://tiles/x")
    assert "no connected device" in str(ei.value).lower()


async def test_relay_timeout_raises_clear_error():
    async def direct(url, **kw):
        raise ConnectionError("offline")
    r = _relay(direct=direct, clients=True)  # relay_timeout_s=0.2, never resolved
    with pytest.raises(FetchRelayError) as ei:
        await r.fetch("http://tiles/x")
    assert "answer" in str(ei.value).lower()


async def test_client_error_result_propagates_message():
    async def direct(url, **kw):
        raise ConnectionError("offline")
    r = _relay(direct=direct, clients=True)
    task = asyncio.ensure_future(r.fetch("http://tiles/x"))
    await asyncio.sleep(0.02)
    r.resolve("rid", ok=False, error="429 Too Many Requests")
    with pytest.raises(FetchRelayError) as ei:
        await task
    assert "429" in str(ei.value)


async def test_resolve_unknown_or_duplicate_is_ignored():
    async def direct(url, **kw):
        raise ConnectionError("offline")
    r = _relay(direct=direct, clients=True)
    assert r.resolve("nope", ok=True, data=b"x") is False   # unknown id
    task = asyncio.ensure_future(r.fetch("http://tiles/x"))
    await asyncio.sleep(0.02)
    assert r.resolve("rid", ok=True, data=b"first") is True
    assert r.resolve("rid", ok=True, data=b"second") is False  # late 2nd client
    assert await task == b"first"


async def test_relay_message_carries_method_and_body():
    async def direct(url, **kw):
        raise ConnectionError("offline")
    r = _relay(direct=direct, clients=True)
    task = asyncio.ensure_future(
        r.fetch("http://overpass/api", method="POST", body=b"data=xyz"))
    await asyncio.sleep(0.02)
    msg = r.sent[-1]
    assert msg["method"] == "POST" and "body_b64" in msg
    r.resolve("rid", ok=True, data=b"ok")
    await task


async def test_fetch_sync_from_worker_thread():
    # The route planner runs sync in an executor; fetch_sync must marshal onto
    # the bound loop and return the relayed bytes.
    async def direct(url, **kw):
        raise ConnectionError("offline")
    r = _relay(direct=direct, clients=True)
    r.bind_loop(asyncio.get_running_loop())

    def worker():
        return r.fetch_sync("http://overpass/api", method="POST", body=b"q")

    task = asyncio.ensure_future(asyncio.to_thread(worker))
    await asyncio.sleep(0.05)                 # let the relay broadcast
    assert r.sent[-1]["type"] == "fetch_request"
    r.resolve("rid", ok=True, data=b"elements")
    assert await task == b"elements"


async def test_relay_http_post_adapter():
    from vanchor.runtime.fetch_relay import relay_http_post
    assert relay_http_post(None) is None      # no relay -> caller keeps direct

    async def direct(url, **kw):
        return b'{"elements": []}'
    r = _relay(direct=direct)
    r.bind_loop(asyncio.get_running_loop())
    post = relay_http_post(r)
    out = await asyncio.to_thread(post, "http://overpass", b"data=q", {"User-Agent": "t"})
    assert out == b'{"elements": []}'
