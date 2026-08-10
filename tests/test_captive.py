"""Captive-portal probe responder: exact expected responses per OS + redirect."""
from __future__ import annotations

import asyncio

from vanchor.ui.captive import PROBES, make_captive_app


def _call(path: str, headers=None):
    app = make_captive_app(8000)
    sent = []

    async def run():
        scope = {"type": "http", "path": path, "headers": headers or []}
        async def receive(): return {"type": "http.request"}
        async def send(msg): sent.append(msg)
        await app(scope, receive, send)
    asyncio.run(run())
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body


def test_apple_probe_returns_canonical_success_page():
    status, headers, body = _call("/hotspot-detect.html")
    assert status == 200
    # Byte-exact: anything else makes iOS pop the captive-portal sheet.
    assert body == b"<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"


def test_android_probe_returns_204_no_body():
    status, headers, body = _call("/generate_204")
    assert status == 204 and body == b""


def test_windows_probes():
    assert _call("/connecttest.txt")[2] == b"Microsoft Connect Test"
    assert _call("/ncsi.txt")[2] == b"Microsoft NCSI"


def test_non_probe_redirects_to_the_ui():
    status, headers, body = _call("/", headers=[(b"host", b"10.42.0.1")])
    assert status == 302
    assert headers[b"location"] == b"http://10.42.0.1:8000/"


def test_probe_table_covers_the_dnsmasq_hosts():
    # Every aliased hostname's probe path must be answered (keep the two files
    # in sync: dnsmasq sends the OS here; we must not 302 a probe).
    assert "/hotspot-detect.html" in PROBES      # captive.apple.com
    assert "/generate_204" in PROBES              # gstatic/android
    assert "/connecttest.txt" in PROBES           # msftconnecttest
    assert "/ncsi.txt" in PROBES                  # msftncsi
