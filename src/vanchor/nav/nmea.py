"""A small, self-contained NMEA 0183 parser and encoder.

We deliberately do *not* depend on pynmea2 (as the old project did): a focused
implementation of just the sentences we use (RMC, GGA, HDM, HDT, APB) is more
testable, fully typed, and removes a dependency. Both parsing and encoding live
here so the simulator can emit exactly the sentences the navigator consumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import reduce

from ..core.models import GeoPoint


def checksum(body: str) -> str:
    """XOR checksum of the characters between ``$`` and ``*``, as 2 hex digits."""
    return f"{reduce(lambda acc, ch: acc ^ ord(ch), body, 0):02X}"


def _wrap(body: str) -> str:
    return f"${body}*{checksum(body)}"


# --------------------------------------------------------------------------- #
# Parsed sentence types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RMC:
    point: GeoPoint
    sog_knots: float
    cog_deg: float
    valid: bool
    time: str = ""


@dataclass(frozen=True)
class GGA:
    point: GeoPoint
    fix_quality: int
    satellites: int


@dataclass(frozen=True)
class Heading:
    heading_deg: float
    reference: str  # "M" magnetic or "T" true


@dataclass(frozen=True)
class APB:
    cross_track_m: float
    steer_to: str  # "L" or "R"
    bearing_to_dest: float
    dest_id: str
    arrived: bool


@dataclass(frozen=True)
class Depth:
    depth_m: float  # total water depth (relative to surface)


Sentence = RMC | GGA | Heading | APB | Depth


class NmeaError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# Coordinate helpers (ddmm.mmmm <-> decimal degrees)
# --------------------------------------------------------------------------- #
def _dm_to_dd(value: str, hemisphere: str) -> float:
    if not value:
        return 0.0
    dot = value.index(".")
    deg = int(value[: dot - 2] or "0")
    minutes = float(value[dot - 2 :])
    dd = deg + minutes / 60.0
    if hemisphere in ("S", "W"):
        dd = -dd
    return dd


def _dd_to_dm(dd: float, is_lat: bool) -> tuple[str, str]:
    hemi = ("N" if dd >= 0 else "S") if is_lat else ("E" if dd >= 0 else "W")
    dd = abs(dd)
    deg = int(dd)
    minutes = (dd - deg) * 60.0
    width = 2 if is_lat else 3
    return f"{deg:0{width}d}{minutes:07.4f}", hemi


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
_HEX_CHARS = frozenset("0123456789ABCDEFabcdef")


def has_valid_checksum(sentence: str) -> bool:
    """Return ``True`` iff *sentence* carries a syntactically valid and correct
    ``*XX`` checksum.

    Works for both ``$`` (NMEA 0183) and ``!`` (AIS/VDM) prefixes. Does **not**
    require the caller to strip the sentence first.
    """
    sentence = sentence.strip()
    if "*" not in sentence:
        return False
    star = sentence.rindex("*")
    cs_field = sentence[star + 1 : star + 3]
    if len(cs_field) != 2 or not _HEX_CHARS.issuperset(cs_field):
        return False
    body = sentence[1:star]  # strip the leading $ or !
    return checksum(body) == cs_field.upper()


def parse(sentence: str, *, require_checksum: bool = False) -> Sentence | None:
    """Parse a single NMEA sentence. Returns ``None`` for a well-formed sentence
    type we don't model; raises :class:`NmeaError` for malformed input.

    Args:
        sentence: A raw NMEA sentence, with or without a ``*checksum`` suffix.
        require_checksum: When ``True``, sentences that have **no** ``*`` at all
            are rejected.  Sentences that have ``*`` but an empty, non-hex, or
            wrong checksum are *always* rejected regardless of this flag.
    """
    sentence = sentence.strip()
    if not sentence.startswith("$"):
        raise NmeaError(f"missing '$': {sentence!r}")

    if "*" in sentence:
        body, _, given = sentence[1:].partition("*")
        cs = given[:2]
        if not cs or not _HEX_CHARS.issuperset(cs):
            raise NmeaError(f"empty or malformed checksum on {sentence!r}")
        if checksum(body) != cs.upper():
            raise NmeaError(f"bad checksum on {sentence!r}")
    elif require_checksum:
        raise NmeaError(f"checksum required but missing on {sentence!r}")
    else:
        body = sentence[1:]

    fields = body.split(",")
    if not fields[0]:
        raise NmeaError(f"empty header: {sentence!r}")
    kind = fields[0][-3:]  # last 3 chars, dropping the talker id

    try:
        if kind == "RMC":
            return _parse_rmc(fields)
        if kind == "GGA":
            return _parse_gga(fields)
        if kind in ("HDM", "HDT", "HDG"):
            return _parse_heading(fields, kind)
        if kind == "APB":
            return _parse_apb(fields)
        if kind == "DPT":
            return _parse_dpt(fields)
        if kind == "DBT":
            return _parse_dbt(fields)
    except (IndexError, ValueError) as exc:
        raise NmeaError(f"could not parse {sentence!r}: {exc}") from exc

    return None


def _parse_rmc(f: list[str]) -> RMC:
    return RMC(
        time=f[1],
        valid=f[2] == "A",
        point=GeoPoint(_dm_to_dd(f[3], f[4]), _dm_to_dd(f[5], f[6])),
        sog_knots=float(f[7]) if f[7] else 0.0,
        cog_deg=float(f[8]) if f[8] else 0.0,
    )


def _parse_gga(f: list[str]) -> GGA:
    return GGA(
        point=GeoPoint(_dm_to_dd(f[2], f[3]), _dm_to_dd(f[4], f[5])),
        fix_quality=int(f[6]) if f[6] else 0,
        satellites=int(f[7]) if f[7] else 0,
    )


def _parse_heading(f: list[str], kind: str) -> Heading:
    """Parse HDM (magnetic), HDT (true), or HDG (sensor + deviation + variation).

    Sign convention (NMEA 0183 § 6.7 / §8.3.21):
        magnetic = sensor_heading + deviation(E+, W-)
        true     = magnetic + variation(E+, W-)

    HDG returns ``reference="T"`` iff the **variation** field is present and
    valid — that is the field that carries a sentence back to the true frame, so
    it is honored whether or not the (often-empty) deviation field is supplied.
    Deviation, when present, is folded in to reach magnetic first. With no
    variation the result is at best magnetic, so ``reference="M"`` and the
    navigator applies its own declination on top.
    """
    if kind == "HDT":
        return Heading(heading_deg=float(f[1]) if f[1] else 0.0, reference="T")
    if kind == "HDG":
        heading = float(f[1]) if f[1] else 0.0
        dev_raw = f[2] if len(f) > 2 else ""
        dev_dir = f[3] if len(f) > 3 else ""
        var_raw = f[4] if len(f) > 4 else ""
        var_dir = f[5] if len(f) > 5 else ""
        has_dev = bool(dev_raw) and dev_dir in ("E", "W")
        has_var = bool(var_raw) and var_dir in ("E", "W")
        dev = float(dev_raw) * (1.0 if dev_dir == "E" else -1.0) if has_dev else 0.0
        var = float(var_raw) * (1.0 if var_dir == "E" else -1.0) if has_var else 0.0
        if has_var:
            # Variation present → we can express a true heading. Deviation (if
            # any) first corrects the sensor to magnetic; variation to true.
            return Heading(heading_deg=(heading + dev + var) % 360, reference="T")
        # No variation → still magnetic. Apply deviation (→ magnetic) if given.
        return Heading(heading_deg=(heading + dev) % 360, reference="M")
    # HDM: magnetic heading, no correction applied.
    return Heading(heading_deg=float(f[1]) if f[1] else 0.0, reference="M")


def _parse_dpt(f: list[str]) -> Depth:
    # $..DPT,<depth below transducer>,<transducer offset>,<max range>
    depth = float(f[1]) if f[1] else 0.0
    offset = float(f[2]) if len(f) > 2 and f[2] else 0.0
    return Depth(depth_m=depth + offset)


def _parse_dbt(f: list[str]) -> Depth:
    # $..DBT,<feet>,f,<metres>,M,<fathoms>,F
    return Depth(depth_m=float(f[3]) if len(f) > 3 and f[3] else 0.0)


def _parse_apb(f: list[str]) -> APB:
    # $..APB,A,A,xte,L/R,N,arrived,perp,brg_orig,M,dest_id,brg_dest,M,head_to,M
    return APB(
        cross_track_m=float(f[3]) if f[3] else 0.0,
        steer_to=f[4] or "L",
        arrived=f[6] == "A",
        bearing_to_dest=float(f[11]) if len(f) > 11 and f[11] else 0.0,
        dest_id=f[10] if len(f) > 10 else "",
    )


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #
def encode_rmc(
    point: GeoPoint,
    sog_knots: float,
    cog_deg: float,
    *,
    talker: str = "GP",
    time: str = "000000",
    date: str = "010100",
    valid: bool = True,
) -> str:
    lat, ns = _dd_to_dm(point.lat, True)
    lon, ew = _dd_to_dm(point.lon, False)
    body = (
        f"{talker}RMC,{time},{'A' if valid else 'V'},{lat},{ns},{lon},{ew},"
        f"{sog_knots:.1f},{cog_deg:.1f},{date},,,A"
    )
    return _wrap(body)


def encode_gga(
    point: GeoPoint,
    *,
    talker: str = "GP",
    time: str = "000000",
    satellites: int = 9,
    quality: int = 1,
) -> str:
    lat, ns = _dd_to_dm(point.lat, True)
    lon, ew = _dd_to_dm(point.lon, False)
    body = (
        f"{talker}GGA,{time},{lat},{ns},{lon},{ew},{quality},{satellites:02d},"
        f"0.9,0.0,M,0.0,M,,"
    )
    return _wrap(body)


def encode_hdm(heading_deg: float, *, talker: str = "HC") -> str:
    return _wrap(f"{talker}HDM,{heading_deg % 360:.1f},M")


def encode_hdt(heading_deg: float, *, talker: str = "HC") -> str:
    return _wrap(f"{talker}HDT,{heading_deg % 360:.1f},T")


def encode_hdg(
    heading_deg: float,
    deviation_deg: float | None = None,
    variation_deg: float | None = None,
    *,
    talker: str = "HC",
) -> str:
    """Encode an HDG sentence.

    ``deviation_deg`` and ``variation_deg`` are signed (East-positive,
    West-negative); pass ``None`` to omit that field pair (empty fields).
    Sign convention: True = heading + deviation(E+) + variation(E+).
    """

    def _fmt(val: float | None) -> str:
        if val is None:
            return ","
        return f"{abs(val):.1f},{'E' if val >= 0 else 'W'}"

    return _wrap(
        f"{talker}HDG,{heading_deg % 360:.1f},{_fmt(deviation_deg)},{_fmt(variation_deg)}"
    )


def encode_dpt(depth_m: float, *, talker: str = "SD") -> str:
    return _wrap(f"{talker}DPT,{depth_m:.1f},0.0")


def encode_apb(
    cross_track_m: float,
    steer_to: str,
    bearing_to_dest: float,
    *,
    dest_id: str = "WP",
    arrived: bool = False,
    talker: str = "VA",
) -> str:
    body = (
        f"{talker}APB,A,A,{abs(cross_track_m):.1f},{steer_to},M,"
        f"{'A' if arrived else 'V'},V,{bearing_to_dest % 360:.1f},T,{dest_id},"
        f"{bearing_to_dest % 360:.1f},T,{bearing_to_dest % 360:.1f},T"
    )
    return _wrap(body)
