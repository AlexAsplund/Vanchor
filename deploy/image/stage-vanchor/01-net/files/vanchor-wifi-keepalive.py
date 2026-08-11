#!/usr/bin/env python3
"""Vanchor WiFi client keep-alive pinger.

The boat Pi is a WiFi AP; the operator's phone naps its WiFi radio
aggressively (worse in Low Power Mode), which shows up as telemetry
hiccups/timeouts in the UI. A constant trickle of ICMP traffic toward the
phone keeps its radio awake -- a standard trick for latency-sensitive AP
setups. This complements the app's relaxed WS keepalive.

Design:
- Discover connected clients from BOTH sources, merged:
    * `iw dev wlan0 station dump`  -> associated MACs (fast truth)
    * MAC -> IPv4 via the kernel neighbor table (`ip -4 neigh show dev wlan0`)
      and NetworkManager shared-mode dnsmasq leases
      (/var/lib/NetworkManager/dnsmasq-wlan0.leases; glob fallback).
- One long-running `ping -i 0.2 -W 1 -q <ip>` process per client (we run as
  root, so sub-second intervals are permitted). No per-packet forking.
- A supervisor loop every ~1.5 s reconciles the client set against running
  pingers: new client -> pinger started within one loop; departed client ->
  pinger killed. Exited pingers are reaped every loop, so a dead client can
  never wedge the loop.
- Safety: only ever pings IPv4s inside wlan0's own subnet (10.42.0.0/24 by
  default), never the Pi itself, never external addresses.
- Quiet: one journal line per client add/remove, nothing per-ping.

BENCH-VERIFY: the actual radio-keep-awake effect (0.2 s ICMP cadence vs. a
phone's power-save timing, add-latency <= one 1.5 s reconcile loop) is
unverifiable without real Pi + phone hardware.
"""
from __future__ import annotations

import glob
import ipaddress
import subprocess
import time

IFACE = "wlan0"
RECONCILE_S = 1.5       # supervisor loop cadence
MAX_PINGERS = 16        # sane cap; an AP-mode Pi rarely serves more clients
RESTART_BACKOFF_S = 10  # don't respawn a crash-looping ping more often
LEASES_PRIMARY = "/var/lib/NetworkManager/dnsmasq-wlan0.leases"
LEASES_GLOB = "/var/lib/NetworkManager/dnsmasq-*.leases"


def log(msg: str) -> None:
    """One-liner to the journal (stdout). Nothing per-ping, ever."""
    print(msg, flush=True)


def _run(cmd: list[str]) -> str | None:
    """Run a command; None on any failure (absent binary, error, timeout)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def iface_network() -> tuple[ipaddress.IPv4Network, ipaddress.IPv4Address | None]:
    """wlan0's own IPv4 subnet + address. Falls back to NM shared-mode default."""
    out = _run(["ip", "-4", "-o", "addr", "show", "dev", IFACE])
    if out:
        for tok in out.split():
            if "/" in tok:
                try:
                    iface = ipaddress.ip_interface(tok)
                except ValueError:
                    continue
                return iface.network, iface.ip
    return ipaddress.ip_network("10.42.0.0/24"), None


def station_macs() -> set[str] | None:
    """Associated client MACs from `iw dev wlan0 station dump`.

    Returns None (not empty) when `iw` itself is unavailable/failing, so the
    caller can fall back to leases/neighbors alone.
    """
    out = _run(["iw", "dev", IFACE, "station", "dump"])
    if out is None:
        return None
    macs = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Station":
            macs.add(parts[1].lower())
    return macs


def neigh_map() -> dict[str, str]:
    """MAC -> IPv4 from the kernel neighbor table, wlan0 only."""
    out = _run(["ip", "-4", "neigh", "show", "dev", IFACE])
    mapping: dict[str, str] = {}
    if not out:
        return mapping
    for line in out.splitlines():
        parts = line.split()
        # "<ip> lladdr <mac> <STATE>" -- entries without lladdr are useless.
        if "lladdr" in parts:
            idx = parts.index("lladdr")
            if idx >= 1 and idx + 1 < len(parts):
                mapping[parts[idx + 1].lower()] = parts[0]
    return mapping


def lease_map() -> dict[str, str]:
    """MAC -> IPv4 from NetworkManager shared-mode dnsmasq leases.

    Column format: expiry MAC IP hostname [clientid]. Absent files are fine.
    """
    paths = [LEASES_PRIMARY]
    paths += [p for p in sorted(glob.glob(LEASES_GLOB)) if p != LEASES_PRIMARY]
    mapping: dict[str, str] = {}
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                mapping.setdefault(parts[1].lower(), parts[2])
    return mapping


def desired_ips() -> set[str]:
    """Current client IPv4s to keep alive, filtered to wlan0's own subnet."""
    network, own_ip = iface_network()
    # Leases first, neighbor table wins on conflict (it is fresher).
    mac_to_ip = lease_map()
    mac_to_ip.update(neigh_map())

    macs = station_macs()
    if macs is None:
        # iw unavailable: fall back to every known lease/neighbor on wlan0.
        candidates = set(mac_to_ip.values())
    else:
        candidates = {mac_to_ip[m] for m in macs if m in mac_to_ip}

    ips: set[str] = set()
    for cand in candidates:
        try:
            addr = ipaddress.ip_address(cand)
        except ValueError:
            continue
        # Only ever ping the AP's own subnet: no external addresses, not the
        # Pi itself, not network/broadcast.
        if addr not in network:
            continue
        if addr == own_ip or addr == network.network_address:
            continue
        if addr == network.broadcast_address:
            continue
        ips.add(str(addr))
    return set(sorted(ips)[:MAX_PINGERS])


def start_pinger(ip: str) -> subprocess.Popen | None:
    """One long-running quiet pinger per client; output discarded."""
    try:
        return subprocess.Popen(
            ["ping", "-i", "0.2", "-W", "1", "-q", "-I", IFACE, ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None


def stop_pinger(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass  # reaped on our exit; never wedge the loop


def main() -> None:
    log(f"vanchor-wifi-keepalive: watching {IFACE} clients "
        f"(reconcile {RECONCILE_S}s, cap {MAX_PINGERS})")
    pingers: dict[str, subprocess.Popen] = {}
    last_start: dict[str, float] = {}

    while True:
        # Reap pingers that exited on their own (unreachable client, iface
        # bounce, ...). Harmless: if the client is still present it gets a
        # fresh pinger below, backoff-limited so a crash-loop can't spam.
        for ip, proc in list(pingers.items()):
            if proc.poll() is not None:
                del pingers[ip]

        desired = desired_ips()
        now = time.monotonic()

        for ip in desired - pingers.keys():
            if now - last_start.get(ip, -RESTART_BACKOFF_S) < RESTART_BACKOFF_S:
                continue
            proc = start_pinger(ip)
            if proc is not None:
                first_time = ip not in last_start
                pingers[ip] = proc
                last_start[ip] = now
                if first_time:
                    log(f"keepalive + {ip}")

        for ip in pingers.keys() - desired:
            stop_pinger(pingers.pop(ip))
            last_start.pop(ip, None)
            log(f"keepalive - {ip}")

        # Forget backoff entries for clients that are fully gone.
        for ip in list(last_start.keys() - desired - pingers.keys()):
            if now - last_start[ip] > RESTART_BACKOFF_S:
                del last_start[ip]

        time.sleep(RECONCILE_S)


if __name__ == "__main__":
    main()
