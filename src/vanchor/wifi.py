"""nmcli-backed WiFi status/scan/join for the SD-image setup flow.

Runs on the Pi where NetworkManager is present; degrades gracefully to
``{"ok": True, "available": False}`` on dev machines / sim-only installs
(mirrors ``discovery.py``'s graceful no-op style).

No new dependencies: nmcli is invoked via ``asyncio.create_subprocess_exec``
(stdlib only).  The PSK is NEVER logged or echoed in any code path.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger("vanchor.wifi")

HOTSPOT_PROFILE = "vanchor-setup"
JOIN_TIMEOUT_S = 45

# Module-level join state
_join_task: asyncio.Task[None] | None = None
_last_join: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _run(args: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr).

    ``FileNotFoundError`` (nmcli not installed) maps to rc=127.
    ``asyncio.TimeoutError`` maps to rc=124.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return (124, "", f"command timed out after {timeout}s")
        return (proc.returncode or 0, stdout_b.decode(errors="replace"),
                stderr_b.decode(errors="replace"))
    except FileNotFoundError:
        return (127, "", "nmcli not found")


def _split_terse(line: str) -> list[str]:
    """Split one ``nmcli -t`` terse line on UNESCAPED colons.

    nmcli terse mode escapes ``':'`` → ``'\\:'`` and ``'\\'`` → ``'\\\\'``
    inside values so the separator colon is unambiguous.
    """
    parts: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            nxt = line[i + 1]
            if nxt == ":":
                current.append(":")
            elif nxt == "\\":
                current.append("\\")
            else:
                current.append(ch)
                current.append(nxt)
            i += 2
        elif ch == ":":
            parts.append("".join(current))
            current = []
            i += 1
        else:
            current.append(ch)
            i += 1
    parts.append("".join(current))
    return parts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def status(runner=_run) -> dict:
    """Current network mode/SSID/IP + hotspot state.

    Returns::

        {
            "ok": True,
            "available": True,          # False when nmcli absent
            "mode": "hotspot"|"wifi"|"ethernet"|"offline",
            "ssid": str | None,
            "ip": str | None,
            "hotspot_active": bool,
            "last_join": {...} | None,
        }
    """
    # Query active connections
    rc, out, err = await runner(
        ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"],
        timeout=10.0,
    )
    if rc in (127, 124) or (rc != 0 and "not found" in err.lower()):
        return {"ok": True, "available": False}

    mode = "offline"
    ssid: str | None = None
    hotspot_active = False

    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = _split_terse(line)
        if len(parts) < 2:
            continue
        name, conn_type = parts[0], parts[1]
        if conn_type == "loopback":
            continue
        if conn_type == "802-11-wireless":
            if name == HOTSPOT_PROFILE:
                hotspot_active = True
                if mode not in ("wifi",):  # wifi outranks hotspot in mode display
                    mode = "hotspot"
                    ssid = HOTSPOT_PROFILE
            else:
                mode = "wifi"
                ssid = name
        elif conn_type == "802-3-ethernet":
            mode = "ethernet"

    # Best-effort IP lookup for wlan0
    ip: str | None = None
    rc2, out2, _ = await runner(
        ["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", "wlan0"],
        timeout=10.0,
    )
    if rc2 == 0:
        for line in out2.splitlines():
            parts = _split_terse(line)
            if len(parts) >= 2 and parts[0].startswith("IP4.ADDRESS"):
                addr = parts[1]
                # Strip prefix length (e.g. "10.42.0.1/24" -> "10.42.0.1")
                ip = addr.split("/")[0] if addr else None
                break

    return {
        "ok": True,
        "available": True,
        "mode": mode,
        "ssid": ssid,
        "ip": ip,
        "hotspot_active": hotspot_active,
        "last_join": _last_join,
    }


async def scan(runner=_run) -> dict:
    """Visible WiFi networks (triggers an nmcli rescan; takes a few seconds).

    Returns::

        {
            "ok": True,
            "available": True,
            "networks": [
                {"ssid": str, "signal": int, "security": str, "in_use": bool},
                ...                          # sorted signal desc, deduped by ssid
            ]
        }
    """
    rc, out, err = await runner(
        ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
         "device", "wifi", "list", "--rescan", "yes"],
        timeout=25.0,
    )
    if rc in (127, 124) or (rc != 0 and "not found" in err.lower()):
        return {"ok": True, "available": False, "networks": []}

    best: dict[str, dict] = {}  # ssid -> best-signal entry
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = _split_terse(line)
        if len(parts) < 4:
            continue
        in_use_marker, ssid_raw, signal_raw, security = (
            parts[0], parts[1], parts[2], parts[3]
        )
        if not ssid_raw:
            continue  # skip hidden networks
        in_use = in_use_marker.strip() == "*"
        try:
            signal = int(signal_raw)
        except ValueError:
            signal = 0
        entry = {"ssid": ssid_raw, "signal": signal, "security": security, "in_use": in_use}
        if ssid_raw not in best or signal > best[ssid_raw]["signal"]:
            best[ssid_raw] = entry

    networks = sorted(best.values(), key=lambda e: e["signal"], reverse=True)
    return {"ok": True, "available": True, "networks": networks}


async def join(ssid: str, psk: str, *, runner=_run) -> dict:
    """Join a WiFi network.  Returns immediately; the join runs in background.

    Validation: ssid 1–32 chars; psk empty (open) or 8–63 chars.
    One join at a time; if one is already in progress returns an error dict.
    The PSK is NEVER logged or included in any returned dict.

    Returns::

        {"ok": True, "joining": ssid, "note": "..."}     # on success (async start)
        {"ok": False, "error": "..."}                     # on validation failure
    """
    global _join_task

    # Validation
    if not ssid or len(ssid) > 32:
        return {"ok": False, "error": "ssid must be 1–32 characters"}
    if psk and (len(psk) < 8 or len(psk) > 63):
        return {"ok": False, "error": "psk must be 8–63 characters (or empty for open networks)"}

    # Check nmcli availability first
    rc, _, err = await runner(["nmcli", "--version"], timeout=5.0)
    if rc == 127:
        return {"ok": True, "available": False, "error": "nmcli not available"}

    # One join at a time
    if _join_task is not None and not _join_task.done():
        return {"ok": False, "error": "join already in progress"}

    _join_task = asyncio.get_running_loop().create_task(
        _join_worker(ssid, psk, runner)
    )
    return {
        "ok": True,
        "joining": ssid,
        "note": "setup hotspot will drop while joining; reconnect your phone to your home WiFi then open http://vanchor.local:8000",
    }


async def _join_worker(ssid: str, psk: str, runner) -> None:
    """Background worker: attempt the join, restore hotspot on failure.
    PSK is never logged.
    """
    global _last_join
    logger.info("WiFi join attempt: ssid=%r", ssid)
    cmd = ["nmcli", "--wait", str(JOIN_TIMEOUT_S), "device", "wifi",
           "connect", ssid]
    if psk:
        # The PSK is passed via argv, not stdin. nmcli's `device wifi connect`
        # does not accept the password on stdin or via a file descriptor — the
        # only API is the `password <psk>` positional argument. This means the
        # PSK is briefly visible in /proc/<pid>/cmdline and `ps` output for the
        # lifetime of the nmcli process (typically < 1 s). This is an nmcli
        # limitation documented in docs/deploy-pi.md (security notes).
        cmd += ["password", psk]
    rc, out, err = await runner(cmd, timeout=float(JOIN_TIMEOUT_S) + 15)

    # Sanitise error: ensure psk does not appear in the log/record
    safe_err = err.strip()
    if psk and psk in safe_err:
        safe_err = safe_err.replace(psk, "***")

    ok = rc == 0
    _last_join = {
        "ssid": ssid,
        "ok": ok,
        "error": safe_err if not ok else "",
        "finished_at": time.time(),
    }

    if ok:
        logger.info("WiFi join succeeded: ssid=%r", ssid)
    else:
        logger.warning("WiFi join failed: ssid=%r rc=%d err=%r", ssid, rc, safe_err)
        # Best-effort: restore the setup hotspot so the phone can reconnect
        rc2, _, _ = await runner(
            ["nmcli", "connection", "up", HOTSPOT_PROFILE], timeout=15.0
        )
        if rc2 == 0:
            logger.info("Setup hotspot restored after failed join")
        else:
            logger.warning("Could not restore hotspot (rc=%d)", rc2)


def last_join() -> dict | None:
    """Return the result of the most recent join attempt, or None."""
    return _last_join
