"""Standalone u-blox (UBX) toolbox.

Poke a receiver's config over ANY serial port, independent of whether a u-blox
GPS *device* is configured (``gps_source`` may be sim/serial/none). Handy for a
combo module -- e.g. a Beitian BE-880 -- that still speaks the UBX protocol even
when it isn't the selected GPS: turn NMEA output on/off, set the update rate or
UART baud, and read live stats (fix, sat count, which protocols are streaming,
firmware version).

Serial I/O goes through an injected async ``transport`` (duck-typed
``open()``/``close()``/``write(bytes)``/``read(n)->bytes``), so the logic is
unit-testable with a fake; the API layer supplies a real
:class:`~vanchor.hardware.serial_link.PySerialTransport` opened exclusively for
the short duration of a call.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from ..nav import ubx


def _count_nmea(chunk: bytes) -> tuple[int, set[str]]:
    """Count `$..` NMEA sentences in a raw chunk and the sentence types seen."""
    n = 0
    types: set[str] = set()
    for line in chunk.replace(b"\r", b"").split(b"\n"):
        if line[:1] == b"$" and len(line) >= 6:
            n += 1
            types.add(line[:6].decode("ascii", "replace"))
    return n, types


async def _read_for(transport: Any, *, duration_s: float, clock: Callable[[], float],
                    sleep: Callable[[float], Any]) -> tuple[bytes, int, set[str]]:
    """Drain the port for ``duration_s``. Returns (raw_bytes, nmea_count, types).
    Each read is bounded so a silent port can't block the whole window."""
    raw = b""
    nmea = 0
    types: set[str] = set()
    end = clock() + duration_s
    while clock() < end:
        try:
            data = await asyncio.wait_for(transport.read(4096), timeout=0.3)
        except (asyncio.TimeoutError, EOFError):
            data = b""
        if not data:
            await sleep(0.05)
            continue
        raw += data
        c, t = _count_nmea(data)
        nmea += c
        types |= t
    return raw, nmea, types


async def read_stats(transport: Any, *, duration_s: float = 2.0,
                     clock: Callable[[], float] = time.monotonic,
                     sleep: Callable[[float], Any] = asyncio.sleep) -> dict:
    """Poll MON-VER, sample the stream, and summarise what the receiver is doing.

    Returns ``{ok, fix, protocols, version, counters}`` -- never raises for a
    quiet receiver (you just get zero counters)."""
    await transport.open()
    try:
        try:
            await transport.write(ubx.poll(*ubx.MON_VER))
        except Exception:  # noqa: BLE001 - a write failure shouldn't kill stats
            pass
        raw, nmea, nmea_types = await _read_for(
            transport, duration_s=duration_s, clock=clock, sleep=sleep)
        frames, _ = ubx.parse_stream(raw)
        ubx_count = len(frames)
        last_pvt = None
        version: dict = {}
        for cls, mid, payload in frames:
            if ubx.is_nav_pvt(cls, mid):
                pvt = ubx.decode_nav_pvt(payload)
                if pvt is not None:
                    last_pvt = pvt
            elif (cls, mid) == ubx.MON_VER:
                version = ubx.decode_mon_ver(payload)
        fix = None
        if last_pvt is not None:
            fix = {
                "valid": last_pvt.valid, "fix_type": last_pvt.fix_type,
                "num_sv": last_pvt.num_sv,
                "lat": round(last_pvt.lat, 7), "lon": round(last_pvt.lon, 7),
                "sog_knots": round(last_pvt.sog_knots, 2),
                "h_acc_m": round(last_pvt.h_acc_m, 2),
            }
        return {
            "ok": True,
            "protocols": {"nmea": nmea > 0, "ubx": ubx_count > 0},
            "nmea_types": sorted(nmea_types),
            "fix": fix,
            "version": version,
            "counters": {"nmea_sentences": nmea, "ubx_frames": ubx_count,
                         "bytes": len(raw), "seconds": duration_s},
        }
    finally:
        await transport.close()


async def _await_ack(transport: Any, *, timeout: float, clock: Callable[[], float],
                     sleep: Callable[[float], Any]) -> bool | None:
    """Read frames until a UBX-ACK/NAK arrives or ``timeout`` elapses."""
    buf = b""
    end = clock() + timeout
    while clock() < end:
        try:
            data = await asyncio.wait_for(transport.read(4096), timeout=0.3)
        except (asyncio.TimeoutError, EOFError):
            data = b""
        if not data:
            await sleep(0.02)
            continue
        buf += data
        frames, buf = ubx.parse_stream(buf)
        ack = ubx.find_ack(frames)
        if ack is not None:
            return ack
    return None


async def _await_frame(transport: Any, want: tuple[int, int], *, timeout: float,
                       clock: Callable[[], float],
                       sleep: Callable[[float], Any]) -> bytes | None:
    """Read until a frame of class/id ``want`` arrives; return its payload."""
    buf = b""
    end = clock() + timeout
    while clock() < end:
        try:
            data = await asyncio.wait_for(transport.read(4096), timeout=0.3)
        except (asyncio.TimeoutError, EOFError):
            data = b""
        if not data:
            await sleep(0.02)
            continue
        buf += data
        frames, buf = ubx.parse_stream(buf)
        for cls, mid, payload in frames:
            if (cls, mid) == want:
                return payload
    return None


async def read_nmea_messages(transport: Any, *, timeout: float = 2.0,
                             clock: Callable[[], float] = time.monotonic,
                             sleep: Callable[[float], Any] = asyncio.sleep) -> dict:
    """Read which standard NMEA sentences the receiver currently emits.

    CFG-VALGET on the RAM layer for GGA/GLL/GSA/GSV/RMC/VTG on UART1 + USB.
    Returns ``{ok, messages: {RMC: {uart1: rate, usb: rate}, ...}}`` -- rate 0
    means off. ``ok: False`` with an error when the receiver doesn't answer
    (not a u-blox / UBX input disabled)."""
    keys = [k for m in ubx.NMEA_MSGOUT_KEYS.values() for k in m.values()]
    await transport.open()
    try:
        await transport.write(ubx.cfg_valget_request(keys))
        payload = await _await_frame(transport, ubx.CFG_VALGET,
                                     timeout=timeout, clock=clock, sleep=sleep)
    finally:
        await transport.close()
    if payload is None:
        return {"ok": False, "error": "no VALGET answer (not a u-blox, or UBX "
                "input disabled on this port)"}
    values = ubx.parse_valget_response(payload)
    messages = {
        name: {port: values.get(key) for port, key in ports.items()}
        for name, ports in ubx.NMEA_MSGOUT_KEYS.items()
    }
    return {"ok": True, "messages": messages}


async def set_nmea_messages(transport: Any, rates: dict, *, persist: bool = False,
                            ack_timeout: float = 1.0,
                            clock: Callable[[], float] = time.monotonic,
                            sleep: Callable[[float], Any] = asyncio.sleep) -> dict:
    """Set per-sentence NMEA output rates (0 = off) on UART1 + USB.

    ``persist`` writes RAM+BBR+Flash. Raises ValueError for unknown sentences /
    out-of-range rates BEFORE anything is sent."""
    layers = 0x07 if persist else 0x01
    frame = ubx.cfg_set_nmea_messages(rates, layers=layers)  # may raise
    await transport.open()
    try:
        await transport.write(frame)
        ack = await _await_ack(transport, timeout=ack_timeout, clock=clock,
                               sleep=sleep)
    finally:
        await transport.close()
    return {"ok": ack is True, "ack": ack}


# What each setting maps to (name -> frame builder taking the value + layers).
def _frames_for(nmea: bool | None, rate_hz: float | None, baud: int | None,
                layers: int) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    if nmea is not None:
        out.append(("nmea", ubx.cfg_set_output_protocols(nmea=nmea, layers=layers)))
    if rate_hz is not None:
        out.append(("rate", ubx.cfg_set_rate_hz(rate_hz, layers=layers)))
    if baud is not None:
        out.append(("baud", ubx.cfg_set_uart_baud(baud, layers=layers)))
    return out


async def apply_settings(transport: Any, *, nmea: bool | None = None,
                         rate_hz: float | None = None, baud: int | None = None,
                         persist: bool = False, ack_timeout: float = 1.0,
                         clock: Callable[[], float] = time.monotonic,
                         sleep: Callable[[float], Any] = asyncio.sleep) -> dict:
    """Send the requested VALSET(s) and collect each ACK.

    ``persist`` writes RAM+BBR+Flash (survives a power cycle); otherwise RAM only
    (immediate, reverts on reboot). Returns ``{ok, acks: {setting: true|false|
    null}}`` where null means no ACK was seen within ``ack_timeout``. Building an
    out-of-range value raises ValueError before anything is sent."""
    layers = 0x07 if persist else 0x01  # RAM | BBR | Flash  vs  RAM
    plan = _frames_for(nmea, rate_hz, baud, layers)  # may raise ValueError (bad value)
    if not plan:
        return {"ok": False, "error": "nothing to set (pass nmea, rate_hz or baud)"}
    await transport.open()
    try:
        acks: dict[str, bool | None] = {}
        for name, frame in plan:
            await transport.write(frame)
            acks[name] = await _await_ack(
                transport, timeout=ack_timeout, clock=clock, sleep=sleep)
        return {"ok": all(v is True for v in acks.values()), "acks": acks}
    finally:
        await transport.close()
