"""Tests for src/vanchor/wifi.py (adoption task 6).

All tests run without a real nmcli — a fake runner is injected per-test.
asyncio_mode = "auto" (pyproject.toml line 68) makes plain async defs work.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

import vanchor.wifi as wifi_mod
from vanchor.wifi import (
    HOTSPOT_PROFILE,
    _split_terse,
    join,
    last_join,
    scan,
    status,
)
from vanchor.core.config import load
from vanchor.app import Runtime
from vanchor.ui.server import create_app
from starlette.testclient import TestClient


# ---------------------------------------------------------------------------
# Fake runner helpers
# ---------------------------------------------------------------------------

def make_runner(*responses: tuple[int, str, str]):
    """Return a fake runner that yields each (rc, out, err) in sequence,
    repeating the last entry if more calls are made."""
    calls: list[list[str]] = []
    resp = list(responses)

    async def fake(args: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
        calls.append(list(args))
        if resp:
            r = resp.pop(0)
        else:
            r = (0, "", "")
        return r

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


def always(rc: int, out: str = "", err: str = ""):
    """A runner that always returns the same (rc, out, err)."""
    calls: list[list[str]] = []

    async def fake(args: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
        calls.append(list(args))
        return (rc, out, err)

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


# ---- terse sample data from the brief -----------------------------------

CONNECTIONS_HOTSPOT = "vanchor-setup:802-11-wireless:wlan0\nlo:loopback:lo\n"
CONNECTIONS_WIFI = "HomeNet:802-11-wireless:wlan0\nlo:loopback:lo\n"
CONNECTIONS_ETH = "eth0:802-3-ethernet:eth0\nlo:loopback:lo\n"
CONNECTIONS_EMPTY = "lo:loopback:lo\n"
IP_HOTSPOT = "IP4.ADDRESS[1]:10.42.0.1/24\n"
IP_HOME = "IP4.ADDRESS[1]:192.168.1.50/24\n"

SCAN_OUTPUT = (
    "*:HomeNet:82:WPA2\n"
    " :Neighbor\\: 5G:47:WPA1 WPA2\n"
    " :HomeNet:31:WPA2\n"
    " ::20:\n"
)


# ---------------------------------------------------------------------------
# 1. _split_terse
# ---------------------------------------------------------------------------

class TestSplitTerse:
    def test_escaped_colon(self):
        parts = _split_terse(r"Neighbor\: 5G:47:WPA1 WPA2")
        assert parts == ["Neighbor: 5G", "47", "WPA1 WPA2"]

    def test_escaped_backslash(self):
        parts = _split_terse(r"a\\b:c")
        assert parts == ["a\\b", "c"]

    def test_empty_field(self):
        parts = _split_terse(":20:")
        assert parts == ["", "20", ""]

    def test_plain(self):
        parts = _split_terse("HomeNet:82:WPA2")
        assert parts == ["HomeNet", "82", "WPA2"]


# ---------------------------------------------------------------------------
# 2. scan() parsing
# ---------------------------------------------------------------------------

async def test_scan_parses_sample():
    """3 lines in (1 hidden, 1 dup) -> 2 networks out, sorted desc, in_use set."""
    # First call (wifi list) returns scan output; no second call needed
    r = make_runner((0, SCAN_OUTPUT, ""))
    result = await scan(runner=r)
    assert result["ok"] is True
    assert result["available"] is True
    nets = result["networks"]
    assert len(nets) == 2
    assert nets[0]["ssid"] == "HomeNet"
    assert nets[0]["signal"] == 82
    assert nets[0]["in_use"] is True
    assert nets[1]["ssid"] == "Neighbor: 5G"
    assert nets[1]["signal"] == 47
    assert nets[1]["in_use"] is False


# ---------------------------------------------------------------------------
# 3. status() hotspot sample
# ---------------------------------------------------------------------------

async def test_status_hotspot():
    r = make_runner(
        (0, CONNECTIONS_HOTSPOT, ""),   # connection show
        (0, IP_HOTSPOT, ""),             # device show
    )
    result = await status(runner=r)
    assert result["ok"] is True
    assert result["available"] is True
    assert result["mode"] == "hotspot"
    assert result["hotspot_active"] is True
    assert result["ip"] == "10.42.0.1"


# ---------------------------------------------------------------------------
# 4. status() ethernet / offline
# ---------------------------------------------------------------------------

async def test_status_ethernet():
    r = make_runner(
        (0, CONNECTIONS_ETH, ""),
        (0, "", ""),
    )
    result = await status(runner=r)
    assert result["mode"] == "ethernet"


async def test_status_offline():
    r = make_runner(
        (0, CONNECTIONS_EMPTY, ""),
        (0, "", ""),
    )
    result = await status(runner=r)
    assert result["mode"] == "offline"


# ---------------------------------------------------------------------------
# 5. nmcli missing -> available=False
# ---------------------------------------------------------------------------

async def test_status_nmcli_missing():
    r = always(127, "", "nmcli not found")
    result = await status(runner=r)
    assert result["ok"] is True
    assert result["available"] is False


async def test_scan_nmcli_missing():
    r = always(127, "", "nmcli not found")
    result = await scan(runner=r)
    assert result["ok"] is True
    assert result["available"] is False


async def test_join_nmcli_missing():
    # join calls nmcli --version first
    r = always(127, "", "")
    result = await join("HomeNet", "password1", runner=r)
    assert result.get("available") is False


# ---------------------------------------------------------------------------
# 6. join() validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ssid,psk,expected_error", [
    ("", "password1", "ssid"),           # empty ssid
    ("x" * 33, "password1", "ssid"),     # ssid too long
    ("HomeNet", "short", "psk"),         # psk too short (7 chars)
])
async def test_join_validation(ssid, psk, expected_error):
    r = always(0)
    result = await join(ssid, psk, runner=r)
    assert result["ok"] is False
    assert expected_error in result["error"]


async def test_join_open_network_accepted():
    """Empty psk is valid (open network)."""
    # version check returns 0, then join succeeds
    r = make_runner((0, "nmcli 1.42\n", ""), (0, "Device 'wlan0' successfully activated", ""))
    result = await join("OpenNet", "", runner=r)
    # Should start the join (ok=True with joining key or available=False)
    assert result.get("ok") is True or result.get("available") is False


# ---------------------------------------------------------------------------
# 7. join() success path
# ---------------------------------------------------------------------------

async def test_join_success():
    """join() returns immediately; after task completes last_join ok=True."""
    # Reset module state
    wifi_mod._join_task = None
    wifi_mod._last_join = None

    version_out = "nmcli 1.42\n"
    join_out = "Device 'wlan0' successfully activated\n"

    calls: list[list[str]] = []

    async def fake_runner(args, timeout=20.0):
        calls.append(list(args))
        if "--version" in args:
            return (0, version_out, "")
        return (0, join_out, "")

    result = await join("HomeNet", "password1", runner=fake_runner)
    assert result["ok"] is True
    assert result["joining"] == "HomeNet"
    assert "note" in result

    # Wait for background task
    if wifi_mod._join_task is not None:
        await wifi_mod._join_task

    lj = last_join()
    assert lj is not None
    assert lj["ok"] is True

    # Check runner was called with the expected nmcli command
    join_call = next((c for c in calls if "connect" in c), None)
    assert join_call is not None
    assert "--wait" in join_call
    assert "45" in join_call
    assert "HomeNet" in join_call
    assert "password" in join_call


# ---------------------------------------------------------------------------
# 8. join() failure -> hotspot restore; last_join has no psk in error
# ---------------------------------------------------------------------------

async def test_join_failure_restores_hotspot():
    wifi_mod._join_task = None
    wifi_mod._last_join = None

    psk = "secretpass"
    calls: list[list[str]] = []

    async def fake_runner(args, timeout=20.0):
        calls.append(list(args))
        if "--version" in args:
            return (0, "nmcli 1.x", "")
        if "connect" in args:
            return (1, "", f"Error: no AP with ssid 'HomeNet' found; psk={psk}")
        if "up" in args and HOTSPOT_PROFILE in args:
            return (0, "Connection up", "")
        return (0, "", "")

    result = await join("HomeNet", psk, runner=fake_runner)
    assert result["ok"] is True

    if wifi_mod._join_task is not None:
        await wifi_mod._join_task

    lj = last_join()
    assert lj is not None
    assert lj["ok"] is False
    assert psk not in (lj.get("error") or ""), "PSK must not appear in error"

    # Hotspot restore was called
    restore_call = next(
        (c for c in calls if "connection" in c and "up" in c and HOTSPOT_PROFILE in c),
        None,
    )
    assert restore_call is not None


# ---------------------------------------------------------------------------
# 9. Second join while first in flight
# ---------------------------------------------------------------------------

async def test_join_in_progress():
    wifi_mod._join_task = None
    wifi_mod._last_join = None

    barrier = asyncio.Event()

    async def slow_runner(args, timeout=20.0):
        if "--version" in args:
            return (0, "nmcli 1.x", "")
        await barrier.wait()  # block until test releases
        return (0, "ok", "")

    # Start first join
    result1 = await join("HomeNet", "password1", runner=slow_runner)
    assert result1["ok"] is True

    # Second join should be rejected
    result2 = await join("Other", "password2", runner=slow_runner)
    assert result2["ok"] is False
    assert "already in progress" in result2["error"]

    # Clean up
    barrier.set()
    if wifi_mod._join_task is not None:
        await asyncio.gather(wifi_mod._join_task, return_exceptions=True)


# ---------------------------------------------------------------------------
# 10. Endpoint tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    app = create_app(Runtime(cfg))
    with TestClient(app) as c:
        yield c


def test_endpoint_wifi_status(client, monkeypatch):
    async def fake_status(runner=None):
        return {"ok": True, "available": False}
    monkeypatch.setattr("vanchor.wifi.status", fake_status)
    resp = client.get("/api/system/wifi")
    assert resp.status_code == 200
    data = resp.json()
    assert "available" in data


def test_endpoint_wifi_scan(client, monkeypatch):
    async def fake_scan(runner=None):
        return {"ok": True, "available": True, "networks": []}
    monkeypatch.setattr("vanchor.wifi.scan", fake_scan)
    resp = client.get("/api/system/wifi/scan")
    assert resp.status_code == 200
    data = resp.json()
    assert "networks" in data


def test_endpoint_wifi_join_empty_ssid(client, monkeypatch):
    """POST with empty ssid -> validation failure (200 body, ok=False)."""
    async def fake_join(ssid, psk, *, runner=None):
        return {"ok": False, "error": "ssid must be 1-32 characters"}
    monkeypatch.setattr("vanchor.wifi.join", fake_join)
    resp = client.post("/api/system/wifi/join", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
