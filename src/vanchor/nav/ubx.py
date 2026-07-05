"""A small, self-contained u-blox UBX protocol parser and config builder.

This module is pure stdlib (``struct`` only) and does *no* serial I/O -- that
belongs in a separate driver.  It provides just what the autopilot needs from a
gen-9 / M9 receiver: framing (Fletcher-8 checksum, stream resync), decoding of
UBX-NAV-PVT into an SI-ish position/velocity fix, and building UBX-CFG-VALSET
frames to configure the receiver (measurement rate, dynamic model, message
output).

A UBX frame is::

    B5 62 | class(1) | id(1) | length(2, LE u16) | payload | CK_A CK_B

where the 2-byte Fletcher-8 checksum is computed over class + id + length +
payload.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Message identifiers
# --------------------------------------------------------------------------- #
SYNC1 = 0xB5
SYNC2 = 0x62

NAV_PVT = (0x01, 0x07)  # UBX-NAV-PVT (class, id)
CFG_VALSET = (0x06, 0x8A)  # UBX-CFG-VALSET (class, id) -- gen-9 / M9 config


def is_nav_pvt(cls: int, mid: int) -> bool:
    """True if ``(cls, mid)`` identifies a UBX-NAV-PVT message."""
    return (cls, mid) == NAV_PVT


# --------------------------------------------------------------------------- #
# Parsed fix
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NavPvt:
    """A decoded UBX-NAV-PVT fix, unit-converted to SI-ish values."""

    lat: float  # degrees
    lon: float  # degrees
    sog_knots: float  # ground speed, knots
    cog_deg: float  # heading of motion, 0-360 deg
    vel_n_mps: float  # NED north velocity, m/s
    vel_e_mps: float  # NED east velocity, m/s
    vel_d_mps: float  # NED down velocity, m/s
    h_acc_m: float  # horizontal accuracy estimate, m
    s_acc_mps: float  # speed accuracy estimate, m/s
    num_sv: int  # satellites used in the solution
    fix_type: int  # 0 none, 2 2D, 3 3D, ...
    valid: bool  # gnssFixOK and fix_type >= 2


# --------------------------------------------------------------------------- #
# Checksum + framing
# --------------------------------------------------------------------------- #
def checksum(body: bytes) -> tuple[int, int]:
    """8-bit Fletcher checksum (CK_A, CK_B) over ``body`` (class+id+len+payload)."""
    ck_a = 0
    ck_b = 0
    for byte in body:
        ck_a = (ck_a + byte) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return ck_a, ck_b


def build_frame(msg_class: int, msg_id: int, payload: bytes) -> bytes:
    """Wrap a payload into a complete UBX frame (sync + length + checksum)."""
    body = struct.pack("<BBH", msg_class, msg_id, len(payload)) + payload
    ck_a, ck_b = checksum(body)
    return bytes((SYNC1, SYNC2)) + body + bytes((ck_a, ck_b))


def parse_stream(buf: bytes) -> tuple[list[tuple[int, int, bytes]], bytes]:
    """Extract all complete, checksum-valid frames from ``buf``.

    Returns ``(frames, remainder)`` where ``frames`` is a list of
    ``(msg_class, msg_id, payload)`` tuples and ``remainder`` is the bytes not
    yet consumed -- either a partial frame at the tail (waiting for more data)
    or nothing.  Garbage before a sync and frames with a bad checksum are
    skipped by resyncing to the next ``0xB5``.  Never raises on arbitrary or
    partial input.
    """
    frames: list[tuple[int, int, bytes]] = []
    i = 0
    n = len(buf)
    while i < n:
        # Seek the first sync byte.
        if buf[i] != SYNC1:
            i += 1
            continue
        # Need at least the 6-byte header (sync x2, class, id, length) to
        # know the payload size.
        if i + 6 > n:
            break  # partial header -> keep as remainder
        if buf[i + 1] != SYNC2:
            i += 1  # lone 0xB5, not a real sync -> resync
            continue
        length = buf[i + 4] | (buf[i + 5] << 8)
        frame_end = i + 6 + length + 2  # header + payload + 2 checksum bytes
        if frame_end > n:
            break  # complete frame not yet in buffer -> remainder
        body = buf[i + 2 : i + 6 + length]
        ck_a, ck_b = checksum(body)
        if ck_a == buf[i + 6 + length] and ck_b == buf[i + 7 + length]:
            msg_class = buf[i + 2]
            msg_id = buf[i + 3]
            payload = bytes(buf[i + 6 : i + 6 + length])
            frames.append((msg_class, msg_id, payload))
            i = frame_end
        else:
            i += 1  # bad checksum -> resync past this sync byte
    return frames, bytes(buf[i:])


# --------------------------------------------------------------------------- #
# UBX-NAV-PVT decode
# --------------------------------------------------------------------------- #
_MPS_TO_KNOTS = 1.9438445


def decode_nav_pvt(payload: bytes) -> NavPvt | None:
    """Decode a UBX-NAV-PVT payload (92 bytes). Return None on wrong length.

    Field offsets (little-endian) per the u-blox interface description::

        fixType U1 @20; flags X1 @21 (bit0 gnssFixOK); numSV U1 @23;
        lon I4 @24 (1e-7 deg); lat I4 @28 (1e-7 deg);
        hAcc U4 @40 (mm); vAcc U4 @44 (mm);
        velN I4 @48 (mm/s); velE I4 @52; velD I4 @56;
        gSpeed I4 @60 (mm/s); headMot I4 @64 (1e-5 deg);
        sAcc U4 @68 (mm/s); headAcc U4 @72 (1e-5 deg).
    """
    if len(payload) != 92:
        return None

    fix_type = payload[20]
    flags = payload[21]
    num_sv = payload[23]
    (lon_raw,) = struct.unpack_from("<i", payload, 24)
    (lat_raw,) = struct.unpack_from("<i", payload, 28)
    (h_acc_mm,) = struct.unpack_from("<I", payload, 40)
    (vel_n,) = struct.unpack_from("<i", payload, 48)
    (vel_e,) = struct.unpack_from("<i", payload, 52)
    (vel_d,) = struct.unpack_from("<i", payload, 56)
    (g_speed,) = struct.unpack_from("<i", payload, 60)
    (head_mot,) = struct.unpack_from("<i", payload, 64)
    (s_acc_mm,) = struct.unpack_from("<I", payload, 68)

    g_speed_mps = g_speed / 1000.0
    return NavPvt(
        lat=lat_raw * 1e-7,
        lon=lon_raw * 1e-7,
        sog_knots=g_speed_mps * _MPS_TO_KNOTS,
        cog_deg=(head_mot * 1e-5) % 360.0,
        vel_n_mps=vel_n / 1000.0,
        vel_e_mps=vel_e / 1000.0,
        vel_d_mps=vel_d / 1000.0,
        h_acc_m=h_acc_mm / 1000.0,
        s_acc_mps=s_acc_mm / 1000.0,
        num_sv=num_sv,
        fix_type=fix_type,
        valid=bool(flags & 0x01) and fix_type >= 2,
    )


# --------------------------------------------------------------------------- #
# UBX-CFG-VALSET builder
# --------------------------------------------------------------------------- #
# A configuration key ID encodes the value's storage size in bits 28-30:
#   0x1 -> 1 bit  (L, bool)   stored as 1 byte
#   0x2 -> U1                 1 byte
#   0x3 -> U2                 2 bytes
#   0x4 -> U4                 4 bytes
#   0x5 -> U8                 8 bytes
_KEY_SIZE_BYTES = {0x1: 1, 0x2: 1, 0x3: 2, 0x4: 4, 0x5: 8}


def _value_bytes(key_id: int, value: int) -> bytes:
    """Little-endian encoding of ``value`` at the width implied by ``key_id``."""
    size_code = (key_id >> 28) & 0x7
    width = _KEY_SIZE_BYTES.get(size_code)
    if width is None:
        raise ValueError(f"unknown CFG key size code {size_code:#x} in {key_id:#010x}")
    return int(value).to_bytes(width, "little")


def cfg_valset(items: list[tuple[int, int]], layers: int = 1) -> bytes:
    """Build a UBX-CFG-VALSET frame setting ``items`` = [(key_id, value), ...].

    ``layers`` is the target layer bitfield (1 = RAM, 2 = BBR, 4 = Flash).  Each
    value's byte width is inferred from the key ID's size bits (28-30).
    """
    payload = bytearray(struct.pack("<BBH", 0, layers, 0))  # version, layers, reserved
    for key_id, value in items:
        payload += struct.pack("<I", key_id)
        payload += _value_bytes(key_id, value)
    return build_frame(CFG_VALSET[0], CFG_VALSET[1], bytes(payload))


def cfg_marine_10hz() -> bytes:
    """Build a VALSET frame configuring an M9N for 10 Hz marine UBX output.

    NOTE: the KEY IDS BELOW SHOULD BE BENCH-VERIFIED against a real M9N -- they
    are transcribed from the u-blox interface description and cannot be tested
    here without hardware.
    """
    items: list[tuple[int, int]] = [
        (0x30210001, 100),  # CFG-RATE-MEAS      U2  = 100 ms (10 Hz)
        (0x20110021, 5),  # CFG-NAVSPG-DYNMODEL  U1  = 5 (sea)
        (0x20910007, 1),  # CFG-MSGOUT-UBX_NAV_PVT_UART1 U1 = 1
        (0x10740002, 0),  # CFG-UART1OUTPROT-NMEA L   = 0 (off)
        (0x10740001, 1),  # CFG-UART1OUTPROT-UBX  L   = 1 (on)
    ]
    return cfg_valset(items, layers=1)
