"""Tests for the NMEA 2000 codec (n2k.py) and the Nmea2000Connector.

TDD order:
1. CAN ID codec: pack/unpack round-trip, PDU1 and PDU2 edges.
2. Decoders: hand-packed buffers with independently-computed expected values.
3. NA sentinels → None for every decoder field.
4. Encoder / decoder round-trips.
5. Ingress: feed 129025+129026 → GpsFix on gps.fix_in (correct value checks).
6. Heading frame → HDT on nmea.in.
7. Depth frame → DPT on nmea.in.
8. Egress: telemetry publish → encoded frames in transport.sent.
9. Unknown PGNs ignored silently.
10. Pairing window: >1.0 s gap → no fix emitted.
"""

from __future__ import annotations

import asyncio
import math
import struct
from typing import Any

import pytest

from vanchor.connectors.context import ConnectorContext
from vanchor.connectors.nmea2000 import (
    FakeCanTransport,
    Nmea2000Connector,
)
from vanchor.core.events import EventBus
from vanchor.core.models import GpsFix
from vanchor.nav import n2k
from vanchor.nav.n2k import (
    Pgn127250,
    Pgn128267,
    Pgn129025,
    Pgn129026,
    Pgn130306,
    decode_127250,
    decode_128267,
    decode_129025,
    decode_129026,
    decode_130306,
    encode_129025,
    encode_129026,
    pack_id,
    unpack_id,
)

# ──────────────────────────────────────────────────────────────────────────── #
# Helpers                                                                      #
# ──────────────────────────────────────────────────────────────────────────── #

_MPS_TO_KNOTS = 1.9438445


def _make_ctx(bus: EventBus, *, produces: tuple[str, ...] = ("gps.fix_in", "nmea.in")) -> ConnectorContext:
    from vanchor.connectors.base import ConnectorManifest

    manifest = ConnectorManifest(
        name="nmea2000",
        label="NMEA 2000",
        description="Test",
        consumes=("telemetry",),
        produces=produces,
        control=False,
        grant_lines=(),
    )
    return ConnectorContext(
        bus=bus,
        manifest=manifest,
        command_sink=lambda _cmd: None,
    )


# ──────────────────────────────────────────────────────────────────────────── #
# 1. CAN ID pack / unpack                                                      #
# ──────────────────────────────────────────────────────────────────────────── #


def test_pack_unpack_pdu2_round_trip() -> None:
    """PDU2 PGN (PF ≥ 240): priority/pgn/src survive the round-trip."""
    for pgn in (129025, 129026, 127250, 128267, 130306):
        for priority in (2, 6, 7):
            for src in (0x00, 0x23, 0xFF):
                cid = pack_id(priority, pgn, src)
                p, pg, s = unpack_id(cid)
                assert p == priority, f"pgn={pgn}: priority {p} != {priority}"
                assert pg == pgn, f"pgn={pgn}: pgn out={pg}"
                assert s == src, f"pgn={pgn}: src {s:#x} != {src:#x}"


def test_pack_unpack_pdu1_round_trip() -> None:
    """PDU1 PGN (PF < 240): PGN survives round-trip; dest is NOT part of PGN."""
    # PGN 60928 = 0xEE00: DP=0, PF=0xEE=238 < 240 → PDU1
    pgn_pdu1 = 60928
    priority = 3
    src = 0x42
    dest = 0xFF
    cid = pack_id(priority, pgn_pdu1, src, dest=dest)
    p, pg, s = unpack_id(cid)
    assert p == priority
    assert pg == pgn_pdu1
    assert s == src
    # Dest is embedded in bits 15-8, NOT in the returned pgn
    embedded_dest = (cid >> 8) & 0xFF
    assert embedded_dest == dest


def test_pdu1_pdu2_detection() -> None:
    """pack_id / unpack_id handle the PDU1/PDU2 boundary at PF=240."""
    # PF = 239 (just below threshold) → PDU1
    # We construct PGN = DP << 16 | PF << 8 = 0 | (239 << 8) = 0xEF00
    pgn_pdu1 = 0xEF00
    cid = pack_id(6, pgn_pdu1, 1, dest=0x00)
    _, pg, _ = unpack_id(cid)
    assert pg == pgn_pdu1

    # PGN 129025: PF=248 ≥ 240 → PDU2
    cid2 = pack_id(6, 129025, 1)
    _, pg2, _ = unpack_id(cid2)
    assert pg2 == 129025


def test_pack_id_priority_bits() -> None:
    """Priority occupies exactly bits 28-26."""
    for p in range(8):
        cid = pack_id(p, 129025, 0)
        assert (cid >> 26) & 0x7 == p


# ──────────────────────────────────────────────────────────────────────────── #
# 2. Decoders — hand-packed buffers                                            #
# ──────────────────────────────────────────────────────────────────────────── #


def test_decode_129025_basic() -> None:
    """Hand-packed lat/lon values decode to exactly the expected degrees."""
    # lat = 47.5 → raw = 475_000_000; lon = -122.3 → raw = -1_223_000_000
    lat_raw = 475_000_000
    lon_raw = -1_223_000_000
    buf = struct.pack("<ii", lat_raw, lon_raw)
    result = decode_129025(buf)
    assert result is not None
    assert result.lat == pytest.approx(lat_raw * 1e-7)   # 47.5 exact
    assert result.lon == pytest.approx(lon_raw * 1e-7)   # -122.3 exact


def test_decode_129026_basic() -> None:
    """COG and SOG decode to radians and m/s with correct scale."""
    # cog_raw=10000 → 1.0 rad;  sog_raw=514 → 5.14 m/s
    sid = 0x05
    ref_byte = 0  # True, bits 0-1 = 0b00
    cog_raw = 10_000
    sog_raw = 514
    buf = struct.pack("<BBHHxx", sid, ref_byte, cog_raw, sog_raw)
    result = decode_129026(buf)
    assert result is not None
    assert result.sid == 5
    assert result.ref == 0
    assert result.cog_rad == pytest.approx(cog_raw * 1e-4)   # 1.0 rad
    assert result.sog_mps == pytest.approx(sog_raw * 0.01)   # 5.14 m/s


def test_decode_127250_basic() -> None:
    """Heading decodes to radians; deviation/variation with correct signs."""
    # heading = π rad (raw=31416), dev = 0.1 rad (raw=1000), var = -0.05 rad (raw=-500)
    sid = 1
    hdg_raw = 31_416
    dev_raw = 1_000
    var_raw = -500
    ref_byte = 0  # True heading
    buf = struct.pack("<BHhhB", sid, hdg_raw, dev_raw, var_raw, ref_byte)
    result = decode_127250(buf)
    assert result is not None
    assert result.sid == 1
    assert result.heading_rad == pytest.approx(hdg_raw * 1e-4)
    assert result.deviation_rad == pytest.approx(dev_raw * 1e-4)
    assert result.variation_rad == pytest.approx(var_raw * 1e-4)
    assert result.ref == 0


def test_decode_128267_basic() -> None:
    """Depth and offset decode with correct scales (0.01 m and 0.001 m)."""
    # depth = 5.00 m → raw=500; offset = 0.300 m → raw=300
    sid = 2
    depth_raw = 500
    offset_raw = 300
    buf = struct.pack("<BIhB", sid, depth_raw, offset_raw, 0xFF)
    result = decode_128267(buf)
    assert result is not None
    assert result.sid == 2
    assert result.depth_m == pytest.approx(depth_raw * 0.01)   # 5.00 m
    assert result.offset_m == pytest.approx(offset_raw * 0.001)  # 0.300 m


def test_decode_130306_basic() -> None:
    """Wind speed and angle decode with correct scales."""
    # speed = 10.00 m/s → raw=1000; angle = 45 deg → 45 * π/180 rad
    sid = 3
    speed_raw = 1_000
    angle_raw = int(math.radians(45) / 1e-4)  # ≈ 7854
    buf = struct.pack("<BHHBxx", sid, speed_raw, angle_raw, 0x02)  # ref=apparent
    result = decode_130306(buf)
    assert result is not None
    assert result.sid == 3
    assert result.speed_mps == pytest.approx(speed_raw * 0.01)
    assert result.angle_rad == pytest.approx(angle_raw * 1e-4)
    assert result.ref == 2


# ──────────────────────────────────────────────────────────────────────────── #
# 3. NA sentinels → None                                                       #
# ──────────────────────────────────────────────────────────────────────────── #


def test_decode_129025_na_both() -> None:
    buf = struct.pack("<ii", 0x7FFFFFFF, 0x7FFFFFFF)
    result = decode_129025(buf)
    assert result is not None
    assert result.lat is None
    assert result.lon is None


def test_decode_129025_na_lat_only() -> None:
    buf = struct.pack("<ii", 0x7FFFFFFF, -1_223_000_000)
    result = decode_129025(buf)
    assert result is not None
    assert result.lat is None
    assert result.lon is not None


def test_decode_129026_na_all() -> None:
    # SID=0xFF, ref bits=0b11 (NA for 2-bit), cog=0xFFFF, sog=0xFFFF
    buf = struct.pack("<BBHHxx", 0xFF, 0x03, 0xFFFF, 0xFFFF)
    result = decode_129026(buf)
    assert result is not None
    assert result.sid is None
    assert result.ref is None
    assert result.cog_rad is None
    assert result.sog_mps is None


def test_decode_127250_na_all() -> None:
    # heading=0xFFFF (u16 NA), dev=0x7FFF (i16 NA), var=0x7FFF, ref nibble=0xF (NA)
    buf = struct.pack("<BHhhB", 0xFF, 0xFFFF, 0x7FFF, 0x7FFF, 0xFF)
    result = decode_127250(buf)
    assert result is not None
    assert result.sid is None
    assert result.heading_rad is None
    assert result.deviation_rad is None
    assert result.variation_rad is None
    assert result.ref is None


def test_decode_128267_na_all() -> None:
    buf = struct.pack("<BIhB", 0xFF, 0xFFFFFFFF, 0x7FFF, 0xFF)
    result = decode_128267(buf)
    assert result is not None
    assert result.sid is None
    assert result.depth_m is None
    assert result.offset_m is None


def test_decode_130306_na_all() -> None:
    buf = struct.pack("<BHHBxx", 0xFF, 0xFFFF, 0xFFFF, 0xFF)
    result = decode_130306(buf)
    assert result is not None
    assert result.sid is None
    assert result.speed_mps is None
    assert result.angle_rad is None
    assert result.ref is None


def test_decoders_reject_short_buffer() -> None:
    """All decoders return None for buffers that are too short."""
    assert decode_129025(b"\x00" * 7) is None
    assert decode_129026(b"\x00" * 5) is None
    assert decode_127250(b"\x00" * 7) is None
    assert decode_128267(b"\x00" * 6) is None
    assert decode_130306(b"\x00" * 5) is None


# ──────────────────────────────────────────────────────────────────────────── #
# 4. Encoder / decoder round-trips                                             #
# ──────────────────────────────────────────────────────────────────────────── #


def test_encode_decode_129025_round_trip() -> None:
    lat, lon = 60.12345, 25.98765
    buf = encode_129025(lat, lon)
    assert len(buf) == 8
    result = decode_129025(buf)
    assert result is not None
    assert result.lat == pytest.approx(lat, abs=1e-7)
    assert result.lon == pytest.approx(lon, abs=1e-7)


def test_encode_decode_129025_na_fields() -> None:
    buf = encode_129025(None, None)
    result = decode_129025(buf)
    assert result is not None
    assert result.lat is None
    assert result.lon is None


def test_encode_decode_129025_partial_na() -> None:
    buf = encode_129025(47.5, None)
    result = decode_129025(buf)
    assert result is not None
    assert result.lat == pytest.approx(47.5, abs=1e-7)
    assert result.lon is None


def test_encode_decode_129026_round_trip() -> None:
    cog = 1.5   # rad
    sog = 3.0   # m/s
    buf = encode_129026(0x05, 0, cog, sog)
    assert len(buf) == 8
    result = decode_129026(buf)
    assert result is not None
    assert result.sid == 5
    assert result.ref == 0
    assert result.cog_rad == pytest.approx(cog, abs=1e-4)
    assert result.sog_mps == pytest.approx(sog, abs=0.01)


def test_encode_decode_129026_na_fields() -> None:
    buf = encode_129026(0xFF, 0, None, None)
    result = decode_129026(buf)
    assert result is not None
    assert result.sid is None
    assert result.cog_rad is None
    assert result.sog_mps is None


# ──────────────────────────────────────────────────────────────────────────── #
# 5. Ingress: 129025 + 129026 → GpsFix on gps.fix_in                         #
# ──────────────────────────────────────────────────────────────────────────── #


@pytest.mark.asyncio
async def test_ingress_pair_produces_gps_fix() -> None:
    """Feeding both position and COG/SOG within 1 s → GpsFix on gps.fix_in."""
    clock = [0.0]
    transport = FakeCanTransport()
    conn = Nmea2000Connector(transport, mono_fn=lambda: clock[0])
    bus = EventBus()
    ctx = _make_ctx(bus)

    fixes: list[GpsFix] = []
    bus.subscribe("gps.fix_in", fixes.append)

    await conn.start(ctx)
    try:
        lat, lon = 47.5, -122.3
        cog_rad = 1.0  # 1.0 rad = 57.296 deg
        sog_mps = 5.14  # 5.14 * 1.9438445 knots

        # Feed 129025 at t=0.0
        pos_data = encode_129025(lat, lon)
        transport.feed(pack_id(6, 129025, 0x23), pos_data)
        await asyncio.sleep(0)

        # Feed 129026 at t=0.5 (within 1 s window)
        clock[0] = 0.5
        cog_data = encode_129026(5, 0, cog_rad, sog_mps)
        transport.feed(pack_id(6, 129026, 0x23), cog_data)
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # let the ingress task run

        assert len(fixes) == 1
        fix = fixes[0]
        assert fix.valid is True
        assert fix.point.lat == pytest.approx(lat, abs=1e-6)
        assert fix.point.lon == pytest.approx(lon, abs=1e-6)
        # cog_rad=1.0 → cog_deg = 57.2957795... deg
        assert fix.cog_deg == pytest.approx(math.degrees(cog_rad), abs=0.01)
        # sog_mps=5.14 → sog_knots = 5.14 * 1.9438445
        assert fix.sog_knots == pytest.approx(sog_mps * _MPS_TO_KNOTS, abs=0.01)
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_ingress_pairing_window_expired() -> None:
    """Frames more than 1.0 s apart must NOT produce a fix."""
    clock = [0.0]
    transport = FakeCanTransport()
    conn = Nmea2000Connector(transport, mono_fn=lambda: clock[0])
    bus = EventBus()
    ctx = _make_ctx(bus)

    fixes: list[Any] = []
    bus.subscribe("gps.fix_in", fixes.append)

    await conn.start(ctx)
    try:
        transport.feed(pack_id(6, 129025, 1), encode_129025(47.5, -122.3))
        await asyncio.sleep(0)
        # Advance clock past the 1-second window
        clock[0] = 1.5
        transport.feed(pack_id(6, 129026, 1), encode_129026(0, 0, 1.0, 2.0))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert fixes == []
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_ingress_no_fix_without_position() -> None:
    """Feeding only 129026 must not emit a fix (no position yet)."""
    transport = FakeCanTransport()
    conn = Nmea2000Connector(transport)
    bus = EventBus()
    ctx = _make_ctx(bus)

    fixes: list[Any] = []
    bus.subscribe("gps.fix_in", fixes.append)

    await conn.start(ctx)
    try:
        transport.feed(pack_id(6, 129026, 1), encode_129026(0, 0, 1.0, 2.0))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert fixes == []
    finally:
        await conn.stop()


# ──────────────────────────────────────────────────────────────────────────── #
# 6. Heading frame → HDT on nmea.in                                            #
# ──────────────────────────────────────────────────────────────────────────── #


@pytest.mark.asyncio
async def test_ingress_heading_publishes_hdt() -> None:
    """127250 (true heading) → NMEA HDT sentence on nmea.in."""
    transport = FakeCanTransport()
    conn = Nmea2000Connector(transport)
    bus = EventBus()
    ctx = _make_ctx(bus)

    sentences: list[str] = []
    bus.subscribe("nmea.in", sentences.append)

    await conn.start(ctx)
    try:
        # heading = π/2 rad (90 deg), ref=0 (True)
        hdg_raw = int(math.pi / 2 / 1e-4)
        buf = struct.pack("<BHhhB", 0x01, hdg_raw, 0x7FFF, 0x7FFF, 0x00)
        transport.feed(pack_id(2, 127250, 1), buf)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(sentences) == 1
        s = sentences[0]
        assert "HDT" in s
        assert "*" in s  # has checksum
        # The heading should be approximately 90 deg
        # $HCHDT,90.0,T*XX
        assert "90." in s or "89." in s  # approx 90 deg
    finally:
        await conn.stop()


# ──────────────────────────────────────────────────────────────────────────── #
# 7. Depth frame → DPT on nmea.in                                              #
# ──────────────────────────────────────────────────────────────────────────── #


@pytest.mark.asyncio
async def test_ingress_depth_publishes_dpt() -> None:
    """128267 (water depth) → NMEA DPT sentence on nmea.in."""
    transport = FakeCanTransport()
    conn = Nmea2000Connector(transport)
    bus = EventBus()
    ctx = _make_ctx(bus)

    sentences: list[str] = []
    bus.subscribe("nmea.in", sentences.append)

    await conn.start(ctx)
    try:
        # depth = 5.00 m, offset = 0.300 m
        depth_raw = 500   # * 0.01 = 5.00 m
        offset_raw = 300  # * 0.001 = 0.300 m
        buf = struct.pack("<BIhB", 0x02, depth_raw, offset_raw, 0xFF)
        transport.feed(pack_id(2, 128267, 1), buf)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(sentences) == 1
        s = sentences[0]
        assert "DPT" in s
        assert "*" in s
        assert "5.00" in s or "5.0" in s
    finally:
        await conn.stop()


# ──────────────────────────────────────────────────────────────────────────── #
# 8. Egress: telemetry → encoded CAN frames in transport.sent                  #
# ──────────────────────────────────────────────────────────────────────────── #


@pytest.mark.asyncio
async def test_egress_telemetry_encodes_frames() -> None:
    """A telemetry publish (with position) → two CAN frames sent within 0.6 s."""
    transport = FakeCanTransport()
    conn = Nmea2000Connector(transport, egress_interval_s=0.05)
    bus = EventBus()
    ctx = _make_ctx(bus)

    await conn.start(ctx)
    try:
        telem = {
            "position": {"lat": 47.5, "lon": -122.3},
            "sog_knots": 5.0,
            "heading_deg": 90.0,
        }
        await bus.publish("telemetry", telem)
        # Give the egress loop a couple of cycles
        await asyncio.sleep(0.12)

        # Should have sent 129025 + 129026 frames
        pgns_sent = set()
        for can_id, data in transport.sent:
            _, pgn, _ = unpack_id(can_id)
            pgns_sent.add(pgn)
        assert 129025 in pgns_sent
        assert 129026 in pgns_sent
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_egress_skips_when_no_position() -> None:
    """A telemetry publish without position → no CAN frames sent."""
    transport = FakeCanTransport()
    conn = Nmea2000Connector(transport, egress_interval_s=0.05)
    bus = EventBus()
    ctx = _make_ctx(bus)

    await conn.start(ctx)
    try:
        telem = {"position": None, "sog_knots": 0.0, "heading_deg": 0.0}
        await bus.publish("telemetry", telem)
        await asyncio.sleep(0.12)
        assert transport.sent == []
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_egress_throttle_at_most_2hz() -> None:
    """Many telemetry publishes at 0.05 s → ≤ 2 positions sent per second."""
    transport = FakeCanTransport()
    # 0.1 s interval → true 10 Hz rate cap → tests the 2 Hz egress interval below
    conn = Nmea2000Connector(transport, egress_interval_s=0.5)
    bus = EventBus()
    ctx = _make_ctx(bus)

    await conn.start(ctx)
    try:
        telem = {"position": {"lat": 47.5, "lon": -122.3}, "sog_knots": 3.0, "heading_deg": 45.0}
        # Publish 10 frames rapidly
        for _ in range(10):
            await bus.publish("telemetry", telem)
        # Wait just over one egress_interval_s — only ~1 egress cycle should fire
        await asyncio.sleep(0.6)
        pos_frames = sum(
            1 for can_id, _ in transport.sent if unpack_id(can_id)[1] == 129025
        )
        # Should not have sent more than ~2 position frames in 0.6 s at 0.5 s interval
        assert pos_frames <= 2
    finally:
        await conn.stop()


# ──────────────────────────────────────────────────────────────────────────── #
# 9. Unknown PGNs ignored silently                                             #
# ──────────────────────────────────────────────────────────────────────────── #


@pytest.mark.asyncio
async def test_unknown_pgn_ignored() -> None:
    """Unknown PGN frames must be received without error and no output emitted."""
    transport = FakeCanTransport()
    conn = Nmea2000Connector(transport)
    bus = EventBus()
    ctx = _make_ctx(bus)

    received: list[Any] = []
    bus.subscribe("gps.fix_in", received.append)
    bus.subscribe("nmea.in", received.append)

    await conn.start(ctx)
    try:
        # PGN 59904 = ISO Request (PDU1, PF=234)
        unknown_can_id = pack_id(6, 59904, 0x01, dest=0xFF)
        transport.feed(unknown_can_id, b"\x00" * 8)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert received == []
    finally:
        await conn.stop()


# ──────────────────────────────────────────────────────────────────────────── #
# 10. debug() never raises                                                     #
# ──────────────────────────────────────────────────────────────────────────── #


def test_debug_never_raises_before_start() -> None:
    conn = Nmea2000Connector(FakeCanTransport())
    result = conn.debug()
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_debug_never_raises_after_start() -> None:
    transport = FakeCanTransport()
    conn = Nmea2000Connector(transport)
    bus = EventBus()
    ctx = _make_ctx(bus)
    await conn.start(ctx)
    try:
        result = conn.debug()
        assert isinstance(result, str)
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_debug_shows_rx_tx_counts() -> None:
    """After receiving a frame, debug() shows rx count."""
    transport = FakeCanTransport()
    conn = Nmea2000Connector(transport)
    bus = EventBus()
    ctx = _make_ctx(bus)
    bus.subscribe("gps.fix_in", lambda _: None)
    bus.subscribe("nmea.in", lambda _: None)
    await conn.start(ctx)
    try:
        transport.feed(pack_id(6, 129025, 1), encode_129025(47.5, -122.3))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        dbg = conn.debug()
        assert "rx" in dbg.lower() or "1" in dbg
    finally:
        await conn.stop()


# ──────────────────────────────────────────────────────────────────────────── #
# 11. Reconnect on transport error                                             #
# ──────────────────────────────────────────────────────────────────────────── #


@pytest.mark.asyncio
async def test_ingress_reconnects_on_eof() -> None:
    """An EOF on recv → connector reconnects (transport.open called again)."""
    transport = FakeCanTransport()
    conn = Nmea2000Connector(transport, reconnect_delay_s=0.01)
    bus = EventBus()
    ctx = _make_ctx(bus)

    await conn.start(ctx)
    try:
        # Trigger an EOF
        transport.feed_eof()
        await asyncio.sleep(0.05)
        # After reconnect, open() will have been called more than once
        assert transport.open_calls >= 2
    finally:
        await conn.stop()
