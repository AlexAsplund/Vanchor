"""water.fetch_overpass's injectable http_post seam (the client-fetch relay
hook) -- no network, no shapely geometry needed."""
from __future__ import annotations

import json

import pytest

from vanchor.nav import water
from vanchor.runtime.fetch_relay import FetchRelayError


def test_fetch_overpass_uses_injected_poster(monkeypatch):
    calls = []

    def post(url, body, headers):
        calls.append((url, bytes(body), dict(headers)))
        return json.dumps({"elements": [{"type": "way", "id": 1}]}).encode()

    out = water.fetch_overpass(59.0, 12.0, 59.1, 12.1, http_post=post)
    assert out == [{"type": "way", "id": 1}]
    assert len(calls) == 1                          # first endpoint succeeded
    url, body, headers = calls[0]
    assert url in water.overpass_endpoints()
    assert b"data=" in body                          # form-encoded query
    assert "User-Agent" in headers


def test_fetch_overpass_tries_next_endpoint_on_generic_failure():
    calls = []

    def post(url, body, headers):
        calls.append(url)
        if len(calls) == 1:
            raise OSError("endpoint down")
        return b'{"elements": []}'

    assert water.fetch_overpass(59.0, 12.0, 59.1, 12.1, http_post=post) == []
    assert len(calls) == 2                           # fell through to endpoint 2


def test_fetch_overpass_relay_error_aborts_immediately():
    # A FetchRelayError means "no internet AND no client could fetch" -- trying
    # the second Overpass endpoint through the same dead relay cannot help.
    calls = []

    def post(url, body, headers):
        calls.append(url)
        raise FetchRelayError("No internet on the boat and no connected device")

    with pytest.raises(FetchRelayError):
        water.fetch_overpass(59.0, 12.0, 59.1, 12.1, http_post=post)
    assert len(calls) == 1                           # no pointless retry


def test_fetch_fail_message_prefers_relay_error():
    from vanchor.runtime.nav_glue import NavGlue
    assert "no connected device" in NavGlue._fetch_fail_message(
        FetchRelayError("No internet on the boat and no connected device")).lower()
    assert "offline chart" in NavGlue._fetch_fail_message(OSError("boom")).lower()


def test_fetch_overpass_target_error_fails_over_to_next_endpoint():
    # The client relayed fine but Overpass endpoint 1 replied 504: that is a
    # TARGET failure -- the second endpoint is a different target, so the
    # failover the direct path has must be preserved through the relay.
    from vanchor.runtime.fetch_relay import FetchRelayTargetError
    calls = []

    def post(url, body, headers):
        calls.append(url)
        if len(calls) == 1:
            raise FetchRelayTargetError("HTTP 504 from overpass-api.de")
        return b'{"elements": [{"type": "way", "id": 2}]}'

    out = water.fetch_overpass(59.0, 12.0, 59.1, 12.1, http_post=post)
    assert out == [{"type": "way", "id": 2}]
    assert len(calls) == 2                       # endpoint 2 tried and won


def test_fetch_overpass_all_targets_fail_keeps_relay_message():
    # Every endpoint 504'd through the client: the raised error keeps the
    # operator-facing relay message (not a generic RuntimeError wrap).
    from vanchor.runtime.fetch_relay import FetchRelayTargetError

    def post(url, body, headers):
        raise FetchRelayTargetError("HTTP 504 from " + url)

    with pytest.raises(FetchRelayTargetError) as ei:
        water.fetch_overpass(59.0, 12.0, 59.1, 12.1, http_post=post)
    assert "504" in str(ei.value)


def test_fetch_fail_message_covers_target_subclass():
    from vanchor.runtime.fetch_relay import FetchRelayTargetError
    from vanchor.runtime.nav_glue import NavGlue
    msg = NavGlue._fetch_fail_message(FetchRelayTargetError("HTTP 504 from overpass-api.de"))
    assert "504" in msg
