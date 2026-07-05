"""Tests for the self-contained UBX protocol module."""

from __future__ import annotations

import random
import struct

from vanchor.nav import ubx


def test_checksum_known_body() -> None:
    # Fletcher-8 over a simple 4-byte body, computed by hand:
    #   bytes = 01 02 03 04
    #   ck_a = 1, 3, 6, 10   -> 10
    #   ck_b = 1, 4, 10, 20  -> 20
    assert ubx.checksum(bytes((1, 2, 3, 4))) == (10, 20)


def test_build_frame_structure() -> None:
    payload = b"\xde\xad\xbe\xef"
    frame = ubx.build_frame(0x06, 0x8A, payload)
    assert frame[0] == ubx.SYNC1
    assert frame[1] == ubx.SYNC2
    assert frame[2] == 0x06  # class
    assert frame[3] == 0x8A  # id
    assert struct.unpack_from("<H", frame, 4)[0] == len(payload)
    body = frame[2:-2]
    assert (frame[-2], frame[-1]) == ubx.checksum(body)
    assert len(frame) == 6 + len(payload) + 2


def test_build_frame_parse_stream_roundtrip() -> None:
    payload = bytes(range(50))
    frame = ubx.build_frame(*ubx.NAV_PVT, payload)
    frames, remainder = ubx.parse_stream(frame)
    assert remainder == b""
    assert frames == [(ubx.NAV_PVT[0], ubx.NAV_PVT[1], payload)]
    cls, mid, out = frames[0]
    assert ubx.is_nav_pvt(cls, mid)
    assert out == payload


def _make_nav_pvt_payload(
    *,
    fix_type: int,
    flags: int,
    num_sv: int,
    lon_1e7: int,
    lat_1e7: int,
    h_acc_mm: int,
    v_acc_mm: int,
    vel_n: int,
    vel_e: int,
    vel_d: int,
    g_speed: int,
    head_mot_1e5: int,
    s_acc_mm: int,
    head_acc_1e5: int,
) -> bytes:
    payload = bytearray(92)
    payload[20] = fix_type
    payload[21] = flags
    payload[23] = num_sv
    struct.pack_into("<i", payload, 24, lon_1e7)
    struct.pack_into("<i", payload, 28, lat_1e7)
    struct.pack_into("<I", payload, 40, h_acc_mm)
    struct.pack_into("<I", payload, 44, v_acc_mm)
    struct.pack_into("<i", payload, 48, vel_n)
    struct.pack_into("<i", payload, 52, vel_e)
    struct.pack_into("<i", payload, 56, vel_d)
    struct.pack_into("<i", payload, 60, g_speed)
    struct.pack_into("<i", payload, 64, head_mot_1e5)
    struct.pack_into("<I", payload, 68, s_acc_mm)
    struct.pack_into("<I", payload, 72, head_acc_1e5)
    return bytes(payload)


def test_decode_nav_pvt_recovers_values() -> None:
    payload = _make_nav_pvt_payload(
        fix_type=3,
        flags=0x01,  # gnssFixOK
        num_sv=11,
        lon_1e7=180_000_000,  # 18.0 deg E
        lat_1e7=590_000_000,  # 59.0 deg N
        h_acc_mm=2500,  # 2.5 m
        v_acc_mm=4000,
        vel_n=1000,  # 1.0 m/s north
        vel_e=-500,  # -0.5 m/s east
        vel_d=250,  # 0.25 m/s down
        g_speed=2000,  # 2.0 m/s ground speed
        head_mot_1e5=9_000_000,  # 90.0 deg
        s_acc_mm=300,  # 0.3 m/s
        head_acc_1e5=5_000_000,
    )
    pvt = ubx.decode_nav_pvt(payload)
    assert pvt is not None
    assert abs(pvt.lon - 18.0) < 1e-9
    assert abs(pvt.lat - 59.0) < 1e-9
    assert abs(pvt.vel_n_mps - 1.0) < 1e-9
    assert abs(pvt.vel_e_mps + 0.5) < 1e-9
    assert abs(pvt.vel_d_mps - 0.25) < 1e-9
    assert abs(pvt.h_acc_m - 2.5) < 1e-9
    assert abs(pvt.s_acc_mps - 0.3) < 1e-9
    assert abs(pvt.sog_knots - 2.0 * 1.9438445) < 1e-6
    assert abs(pvt.cog_deg - 90.0) < 1e-6
    assert pvt.num_sv == 11
    assert pvt.fix_type == 3
    assert pvt.valid is True


def test_decode_nav_pvt_cog_wraps() -> None:
    payload = _make_nav_pvt_payload(
        fix_type=3,
        flags=0x01,
        num_sv=8,
        lon_1e7=0,
        lat_1e7=0,
        h_acc_mm=1000,
        v_acc_mm=1000,
        vel_n=0,
        vel_e=0,
        vel_d=0,
        g_speed=0,
        head_mot_1e5=36_500_000,  # 365 deg -> wraps to 5
        s_acc_mm=0,
        head_acc_1e5=0,
    )
    pvt = ubx.decode_nav_pvt(payload)
    assert pvt is not None
    assert abs(pvt.cog_deg - 5.0) < 1e-6


def test_decode_nav_pvt_invalid_when_no_fix() -> None:
    payload = _make_nav_pvt_payload(
        fix_type=0,
        flags=0x00,  # gnssFixOK clear
        num_sv=0,
        lon_1e7=0,
        lat_1e7=0,
        h_acc_mm=99999,
        v_acc_mm=99999,
        vel_n=0,
        vel_e=0,
        vel_d=0,
        g_speed=0,
        head_mot_1e5=0,
        s_acc_mm=0,
        head_acc_1e5=0,
    )
    pvt = ubx.decode_nav_pvt(payload)
    assert pvt is not None
    assert pvt.valid is False


def test_decode_nav_pvt_wrong_length() -> None:
    assert ubx.decode_nav_pvt(b"\x00" * 91) is None
    assert ubx.decode_nav_pvt(b"\x00" * 93) is None
    assert ubx.decode_nav_pvt(b"") is None


def test_parse_stream_skips_garbage_before_sync() -> None:
    frame = ubx.build_frame(*ubx.NAV_PVT, bytes(range(10)))
    stream = b"\x00\x11\x22garbage" + frame
    frames, remainder = ubx.parse_stream(stream)
    assert remainder == b""
    assert len(frames) == 1
    assert frames[0][2] == bytes(range(10))


def test_parse_stream_partial_trailing_frame_completes_next_chunk() -> None:
    payload = bytes(range(20))
    frame = ubx.build_frame(*ubx.NAV_PVT, payload)
    split = len(frame) - 5
    chunk1, chunk2 = frame[:split], frame[split:]

    frames, remainder = ubx.parse_stream(chunk1)
    assert frames == []
    assert remainder == chunk1  # whole partial frame kept

    frames, remainder = ubx.parse_stream(remainder + chunk2)
    assert remainder == b""
    assert len(frames) == 1
    assert frames[0][2] == payload


def test_parse_stream_drops_bad_checksum_frame() -> None:
    good = ubx.build_frame(*ubx.NAV_PVT, bytes(range(8)))
    bad = bytearray(ubx.build_frame(*ubx.NAV_PVT, bytes(range(8))))
    bad[-1] ^= 0xFF  # corrupt CK_B
    stream = bytes(bad) + good
    frames, remainder = ubx.parse_stream(stream)
    assert remainder == b""
    assert len(frames) == 1  # only the good frame survives
    assert frames[0][2] == bytes(range(8))


def test_parse_stream_never_raises_on_random_bytes() -> None:
    rng = random.Random(1234)
    for _ in range(500):
        n = rng.randint(0, 64)
        blob = bytes(rng.randint(0, 255) for _ in range(n))
        frames, remainder = ubx.parse_stream(blob)
        assert isinstance(frames, list)
        assert isinstance(remainder, bytes)


def test_parse_stream_multiple_frames() -> None:
    f1 = ubx.build_frame(*ubx.NAV_PVT, b"\x01\x02")
    f2 = ubx.build_frame(0x06, 0x8A, b"\x03\x04\x05")
    frames, remainder = ubx.parse_stream(f1 + f2)
    assert remainder == b""
    assert [(c, i) for c, i, _ in frames] == [ubx.NAV_PVT, ubx.CFG_VALSET]


def test_cfg_valset_wellformed() -> None:
    frame = ubx.cfg_valset([(0x30210001, 100)], layers=1)
    frames, remainder = ubx.parse_stream(frame)
    assert remainder == b""
    assert len(frames) == 1
    cls, mid, payload = frames[0]
    assert (cls, mid) == ubx.CFG_VALSET
    # header: version(1) layers(1) reserved(2) = 4 bytes, then key(4) + value(2)
    assert payload[0] == 0  # version
    assert payload[1] == 1  # layers = RAM
    assert struct.unpack_from("<H", payload, 2)[0] == 0  # reserved
    assert struct.unpack_from("<I", payload, 4)[0] == 0x30210001
    assert struct.unpack_from("<H", payload, 8)[0] == 100  # U2 value
    assert len(payload) == 4 + 4 + 2


def test_cfg_valset_infers_widths() -> None:
    # size codes: U1(0x2)->1, U2(0x3)->2, U4(0x4)->4, L(0x1)->1
    items = [
        (0x20110021, 5),  # U1
        (0x30210001, 100),  # U2
        (0x40000000, 7),  # U4
        (0x10740001, 1),  # L (1 bit -> 1 byte)
    ]
    frame = ubx.cfg_valset(items)
    _, _, payload = ubx.parse_stream(frame)[0][0]
    # 4-byte header + per item (4-byte key + value)
    expected = 4 + (4 + 1) + (4 + 2) + (4 + 4) + (4 + 1)
    assert len(payload) == expected


def test_cfg_marine_10hz_wellformed() -> None:
    frame = ubx.cfg_marine_10hz()
    frames, remainder = ubx.parse_stream(frame)
    assert remainder == b""
    assert len(frames) == 1
    cls, mid, payload = frames[0]
    assert (cls, mid) == ubx.CFG_VALSET
    assert payload[1] == 1  # RAM layer
    # 5 items: U2 + U1 + U1 + L + L values -> 2+1+1+1+1 = 6 value bytes,
    # plus 5*4 key bytes, plus 4-byte header
    assert len(payload) == 4 + 5 * 4 + (2 + 1 + 1 + 1 + 1)
