"""NMEA 2000 (N2K) pure codec — 29-bit CAN ID pack/unpack and single-frame PGN
decoders/encoders.

This module is **pure stdlib** (``struct`` only) and does **no** I/O. It provides:

* :func:`pack_id` / :func:`unpack_id` — (priority, PGN, source) ↔ 29-bit CAN ID,
  with PDU1/PDU2 detection.
* Decoders for PGNs 129025, 129026, 127250, 128267 and 130306 — single-frame
  format only. Fast-packet PGNs (e.g. 129029 GNSS) are **intentionally out of
  scope**.
* Encoders for 129025 and 129026 (egress back onto the N2K bus).

**CAN ID layout (29-bit extended ID)**::

    bits 28-26  priority (3 bits)
    bit  25     reserved (always 0 for N2K)
    bit  24     data page (DP)
    bits 23-16  PDU format (PF)
    bits 15-8   PDU specific (PS) — dest address (PDU1) or group extension (PDU2)
    bits  7-0   source address (SA)

PDU1 (PF < 240): PGN = (DP << 16) | (PF << 8).  PS is the destination address.
PDU2 (PF ≥ 240): PGN = (DP << 16) | (PF << 8) | PS. All five PGNs decoded here
are PDU2.

**Not-available sentinels** (per NMEA 2000 data-type spec): ``0xFF`` (u8),
``0x7FFF`` (i16), ``0xFFFF`` (u16), ``0x7FFFFFFF`` (i32), ``0xFFFFFFFF`` (u32).
Decoder fields that equal their sentinel are returned as ``None``.

.. note::
    **BENCH-VERIFY** — field offsets and scale factors are transcribed from the
    public NMEA 2000 PGN documentation (canboat / NMEA2000 open source project).
    No real N2K bus was available during development. All single-frame decoders
    round-trip correctly in software (see ``tests/test_n2k.py``) but should be
    verified against a physical receiver before production use.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# CAN ID pack / unpack                                                        #
# --------------------------------------------------------------------------- #

def pack_id(
    priority: int,
    pgn: int,
    src: int,
    *,
    dest: int = 0xFF,
) -> int:
    """Pack (priority, PGN, src) into a 29-bit extended CAN ID.

    For PDU2 PGNs (PF ≥ 240, which covers all N2K data PGNs decoded here) the
    PGN already contains the group-extension byte and is placed directly in
    bits 24-8. For PDU1 (PF < 240) the destination address is embedded in bits
    15-8 and is NOT part of the PGN.
    """
    pf = (pgn >> 8) & 0xFF
    if pf < 240:  # PDU1 — dest in bits 15-8; PGN bits 7-0 are 0
        return (
            ((priority & 0x7) << 26)
            | ((pgn & 0x1_FF00) << 8)
            | ((dest & 0xFF) << 8)
            | (src & 0xFF)
        )
    # PDU2 — PGN includes the group extension (PS byte)
    return ((priority & 0x7) << 26) | ((pgn & 0x1_FFFF) << 8) | (src & 0xFF)


def unpack_id(can_id: int) -> tuple[int, int, int]:
    """Unpack a 29-bit CAN ID into ``(priority, pgn, src)``.

    For PDU1 frames (PF < 240) the embedded destination address is dropped from
    the returned PGN (the caller can extract it via ``(can_id >> 8) & 0xFF``
    when needed).
    """
    priority = (can_id >> 26) & 0x7
    dp = (can_id >> 24) & 0x1
    pf = (can_id >> 16) & 0xFF
    ps = (can_id >> 8) & 0xFF
    src = can_id & 0xFF
    if pf >= 240:  # PDU2 — PS is the group extension, part of the PGN
        pgn = (dp << 16) | (pf << 8) | ps
    else:  # PDU1 — PS is the destination address, NOT part of the PGN
        pgn = (dp << 16) | (pf << 8)
    return priority, pgn, src


# --------------------------------------------------------------------------- #
# Decoded PGN dataclasses                                                     #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Pgn129025:
    """PGN 129025 — Position Rapid Update.

    BENCH-VERIFY: lat/lon are i32 LE * 1e-7 deg per canboat.
    NA sentinel for i32 = 0x7FFFFFFF → field returns None.
    """

    lat: float | None  # degrees, WGS84
    lon: float | None  # degrees, WGS84


@dataclass(frozen=True)
class Pgn129026:
    """PGN 129026 — COG and SOG, Rapid Update.

    BENCH-VERIFY: layout per canboat PGN DB.
    Byte 0: SID. Byte 1 bits 0-1: COG reference (0=True, 1=Magnetic).
    Bytes 2-3: COG u16 * 1e-4 rad. Bytes 4-5: SOG u16 * 0.01 m/s.
    Bytes 6-7: reserved.
    NA: u8=0xFF, u16=0xFFFF, 2-bit ref values 0-2 are valid, 3=N/A (None).
    """

    sid: int | None   # sequence identifier
    ref: int | None   # 0=True, 1=Magnetic, 2=Error, 3=N/A
    cog_rad: float | None  # course over ground, radians
    sog_mps: float | None  # speed over ground, m/s


@dataclass(frozen=True)
class Pgn127250:
    """PGN 127250 — Vessel Heading.

    BENCH-VERIFY: layout per canboat PGN DB.
    Byte 0: SID. Bytes 1-2: heading u16 * 1e-4 rad. Bytes 3-4: deviation i16
    * 1e-4 rad. Bytes 5-6: variation i16 * 1e-4 rad. Byte 7 bits 0-3: ref
    (0=True, 1=Magnetic); bits 4-7 reserved.
    NA: u8=0xFF, u16=0xFFFF, i16=0x7FFF, 4-bit ref 0xF=N/A.
    """

    sid: int | None
    heading_rad: float | None   # radians
    deviation_rad: float | None  # radians (East-positive)
    variation_rad: float | None  # radians (East-positive)
    ref: int | None   # 0=True, 1=Magnetic


@dataclass(frozen=True)
class Pgn128267:
    """PGN 128267 — Water Depth.

    BENCH-VERIFY: layout per canboat PGN DB.
    Byte 0: SID. Bytes 1-4: depth u32 * 0.01 m (below transducer).
    Bytes 5-6: offset i16 * 0.001 m (transducer from waterline; positive =
    transducer below waterline). Byte 7: reserved.
    NA: u8=0xFF, u32=0xFFFFFFFF, i16=0x7FFF.
    """

    sid: int | None
    depth_m: float | None   # depth below transducer, m
    offset_m: float | None  # transducer offset from waterline, m


@dataclass(frozen=True)
class Pgn130306:
    """PGN 130306 — Wind Data.

    BENCH-VERIFY: layout per canboat PGN DB.
    Byte 0: SID. Bytes 1-2: wind speed u16 * 0.01 m/s.
    Bytes 3-4: wind angle u16 * 1e-4 rad.
    Byte 5 bits 0-3: reference (0=True(ground), 1=Magnetic(ground),
    2=Apparent, 3=True(boat), 4=True(water)); bits 4-7 reserved.
    Bytes 6-7: reserved.
    NA: u8=0xFF, u16=0xFFFF, 4-bit ref 0xF=N/A.

    .. note::
        This PGN is decoded and exposed through ``debug()`` only.  There is no
        existing ingest path for wind data in Vanchor-NG's navigator.
    """

    sid: int | None
    speed_mps: float | None  # m/s
    angle_rad: float | None  # radians (direction wind is coming FROM)
    ref: int | None   # reference frame


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

_NA_U8 = 0xFF
_NA_I16 = 0x7FFF
_NA_U16 = 0xFFFF
_NA_I32 = 0x7FFFFFFF
_NA_U32 = 0xFFFFFFFF


def _u8_or_none(v: int) -> int | None:
    return None if v == _NA_U8 else v


def _u16_or_none(v: int) -> int | None:
    return None if v == _NA_U16 else v


def _i16_or_none(v: int) -> int | None:
    return None if v == _NA_I16 else v


def _u32_or_none(v: int) -> int | None:
    return None if v == _NA_U32 else v


def _i32_or_none(v: int) -> int | None:
    return None if v == _NA_I32 else v


# --------------------------------------------------------------------------- #
# Decoders                                                                    #
# --------------------------------------------------------------------------- #

def decode_129025(data: bytes) -> Pgn129025 | None:
    """Decode PGN 129025 – Position Rapid Update.

    Returns ``None`` when ``data`` is shorter than 8 bytes.  Individual fields
    that equal the i32 NA sentinel (0x7FFFFFFF) are returned as ``None``.
    """
    if len(data) < 8:
        return None
    lat_raw, lon_raw = struct.unpack_from("<ii", data, 0)
    lat = None if lat_raw == _NA_I32 else lat_raw * 1e-7
    lon = None if lon_raw == _NA_I32 else lon_raw * 1e-7
    return Pgn129025(lat=lat, lon=lon)


def decode_129026(data: bytes) -> Pgn129026 | None:
    """Decode PGN 129026 – COG and SOG, Rapid Update.

    Returns ``None`` when ``data`` is shorter than 6 bytes.
    """
    if len(data) < 6:
        return None
    sid_raw = data[0]
    ref_raw = data[1] & 0x03  # bits 0-1
    (cog_raw,) = struct.unpack_from("<H", data, 2)
    (sog_raw,) = struct.unpack_from("<H", data, 4)

    sid = _u8_or_none(sid_raw)
    ref = None if ref_raw == 0x03 else ref_raw  # 2-bit: 0b11 = N/A
    cog = None if cog_raw == _NA_U16 else cog_raw * 1e-4
    sog = None if sog_raw == _NA_U16 else sog_raw * 0.01
    return Pgn129026(sid=sid, ref=ref, cog_rad=cog, sog_mps=sog)


def decode_127250(data: bytes) -> Pgn127250 | None:
    """Decode PGN 127250 – Vessel Heading.

    Returns ``None`` when ``data`` is shorter than 8 bytes.
    """
    if len(data) < 8:
        return None
    sid_raw = data[0]
    (hdg_raw,) = struct.unpack_from("<H", data, 1)
    (dev_raw,) = struct.unpack_from("<h", data, 3)
    (var_raw,) = struct.unpack_from("<h", data, 5)
    ref_raw = data[7] & 0x0F  # bits 0-3

    sid = _u8_or_none(sid_raw)
    hdg = None if hdg_raw == _NA_U16 else hdg_raw * 1e-4
    dev = None if dev_raw == _NA_I16 else dev_raw * 1e-4
    var = None if var_raw == _NA_I16 else var_raw * 1e-4
    ref = None if ref_raw == 0x0F else ref_raw  # 4-bit: 0xF = N/A
    return Pgn127250(
        sid=sid,
        heading_rad=hdg,
        deviation_rad=dev,
        variation_rad=var,
        ref=ref,
    )


def decode_128267(data: bytes) -> Pgn128267 | None:
    """Decode PGN 128267 – Water Depth.

    Returns ``None`` when ``data`` is shorter than 7 bytes.
    """
    if len(data) < 7:
        return None
    sid_raw = data[0]
    (depth_raw,) = struct.unpack_from("<I", data, 1)  # u32
    (offset_raw,) = struct.unpack_from("<h", data, 5)  # i16

    sid = _u8_or_none(sid_raw)
    depth = None if depth_raw == _NA_U32 else depth_raw * 0.01
    offset = None if offset_raw == _NA_I16 else offset_raw * 0.001
    return Pgn128267(sid=sid, depth_m=depth, offset_m=offset)


def decode_130306(data: bytes) -> Pgn130306 | None:
    """Decode PGN 130306 – Wind Data.

    Returns ``None`` when ``data`` is shorter than 6 bytes.
    """
    if len(data) < 6:
        return None
    sid_raw = data[0]
    (speed_raw,) = struct.unpack_from("<H", data, 1)
    (angle_raw,) = struct.unpack_from("<H", data, 3)
    ref_raw = data[5] & 0x0F  # bits 0-3

    sid = _u8_or_none(sid_raw)
    speed = None if speed_raw == _NA_U16 else speed_raw * 0.01
    angle = None if angle_raw == _NA_U16 else angle_raw * 1e-4
    ref = None if ref_raw == 0x0F else ref_raw
    return Pgn130306(sid=sid, speed_mps=speed, angle_rad=angle, ref=ref)


# --------------------------------------------------------------------------- #
# Encoders                                                                    #
# --------------------------------------------------------------------------- #

def encode_129025(lat: float | None, lon: float | None) -> bytes:
    """Encode a PGN 129025 – Position Rapid Update frame (8 bytes).

    ``None`` fields are encoded as the i32 NA sentinel (0x7FFFFFFF).
    Round-trips through :func:`decode_129025`.

    BENCH-VERIFY: layout matches decode_129025.
    """
    lat_raw = _NA_I32 if lat is None else int(round(lat / 1e-7))
    lon_raw = _NA_I32 if lon is None else int(round(lon / 1e-7))
    # Clamp to i32 range to avoid struct.pack overflow
    lat_raw = max(-0x80000000, min(0x7FFFFFFF, lat_raw))
    lon_raw = max(-0x80000000, min(0x7FFFFFFF, lon_raw))
    return struct.pack("<ii", lat_raw, lon_raw)


def encode_129026(
    sid: int,
    ref: int,
    cog_rad: float | None,
    sog_mps: float | None,
) -> bytes:
    """Encode a PGN 129026 – COG and SOG, Rapid Update frame (8 bytes).

    ``None`` cog/sog fields are encoded as the u16 NA sentinel (0xFFFF).
    Round-trips through :func:`decode_129026`.

    BENCH-VERIFY: layout matches decode_129026.
    """
    sid_raw = _NA_U8 if sid == _NA_U8 else (sid & 0xFF)
    ref_byte = ref & 0x03
    cog_raw = _NA_U16 if cog_rad is None else min(_NA_U16, max(0, int(round(cog_rad / 1e-4))))
    sog_raw = _NA_U16 if sog_mps is None else min(_NA_U16, max(0, int(round(sog_mps / 0.01))))
    return struct.pack("<BBHHxx", sid_raw, ref_byte, cog_raw, sog_raw)
