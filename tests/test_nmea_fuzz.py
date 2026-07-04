"""Property / fuzz tests for the NMEA parser (:mod:`vanchor.nav.nmea`).

The parser sits directly on the wire: it is fed whatever bytes a GPS, compass
or depth sounder happens to spit out, including noise, partial lines and
garbage during power-up. Its hard contract is therefore *robustness*:

* On **any** input it must fail in exactly one controlled way -- by returning a
  parsed sentence / ``None``, or by raising :class:`nmea.NmeaError`. It must
  never leak a raw ``IndexError``/``ValueError``/``UnicodeError`` or otherwise
  blow up, since the read loops only guard against ``NmeaError``.
* It must **respect the checksum**: a present-but-wrong ``*XX`` is always
  rejected, and a correct one is accepted.
* Every sentence the *encoder* emits must **round-trip** back through the parser
  to (approximately) the same values.

These invariants are exactly what property-based testing with Hypothesis is
good at: we throw thousands of random / adversarial inputs at the parser and
assert the invariants hold, rather than hand-picking a few examples.
"""

from __future__ import annotations

import pytest

pytest.importorskip("hypothesis")  # property/fuzz lib (a dev/test dependency)

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from vanchor.core.models import GeoPoint
from vanchor.nav import nmea

# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #
# Characters that are safe to place inside a real NMEA body: 7-bit printable
# ASCII (NMEA 0183 is a 7-bit protocol, so the XOR checksum always fits in two
# hex digits), minus the framing sentinels '$'/'!' (sentence start) and '*'
# (checksum). CR/LF fall below 0x20 and are excluded by the codepoint range.
_BODY_CHARS = st.characters(
    min_codepoint=0x20,
    max_codepoint=0x7E,
    blacklist_characters="$!*",
)
_bodies = st.text(alphabet=_BODY_CHARS, min_size=1, max_size=60)

# Arbitrary text, including the empty string and control characters.
_arbitrary_text = st.text(max_size=80)

# Arbitrary bytes decoded as latin-1 so every byte value 0..255 maps to a char;
# this is the closest a ``str``-typed API gets to "raw garbage bytes".
_garbage_from_bytes = st.binary(max_size=80).map(lambda b: b.decode("latin-1"))

_HEX = "0123456789ABCDEF"


def _wrong_checksum(correct: str) -> str:
    """Return a 2-hex-digit checksum string guaranteed to differ from *correct*."""
    correct = correct.upper()
    alt = "00" if correct != "00" else "01"
    assert alt != correct
    return alt


# --------------------------------------------------------------------------- #
# 1. The parser never raises anything other than NmeaError
# --------------------------------------------------------------------------- #
@settings(max_examples=800)
@given(st.one_of(_arbitrary_text, _garbage_from_bytes))
def test_parse_never_raises_unexpected(text: str) -> None:
    """On truly arbitrary input, ``parse`` either returns or raises NmeaError."""
    for require in (False, True):
        try:
            result = nmea.parse(text, require_checksum=require)
        except nmea.NmeaError:
            pass  # the one sanctioned failure mode
        else:
            assert result is None or isinstance(result, nmea.Sentence.__args__)


@settings(max_examples=800)
@given(st.one_of(_arbitrary_text, _garbage_from_bytes))
def test_has_valid_checksum_never_raises(text: str) -> None:
    """The checksum probe is total: it returns a bool for any input."""
    assert isinstance(nmea.has_valid_checksum(text), bool)


@settings(max_examples=400)
@given(_bodies)
def test_dollar_prefixed_bodies_never_raise_unexpected(body: str) -> None:
    """A '$'-prefixed line (no checksum) must never raise a non-NmeaError.

    This exercises the field-splitting / sentence-dispatch path with fuzzed
    but structurally plausible content.
    """
    try:
        nmea.parse("$" + body)
    except nmea.NmeaError:
        pass


# --------------------------------------------------------------------------- #
# 2. Checksums are respected
# --------------------------------------------------------------------------- #
@given(_bodies)
def test_correct_checksum_accepted(body: str) -> None:
    """A body wrapped with its own correct checksum passes ``has_valid_checksum``
    and never trips the *checksum* branch of ``parse``."""
    sentence = f"${body}*{nmea.checksum(body)}"
    assert nmea.has_valid_checksum(sentence) is True
    # parse may still reject the *content* (unknown/short fields) with NmeaError,
    # but if it does, the message must not be about the checksum.
    try:
        nmea.parse(sentence)
    except nmea.NmeaError as exc:
        assert "checksum" not in str(exc)


@given(_bodies)
def test_wrong_checksum_always_rejected(body: str) -> None:
    """A present-but-wrong checksum is rejected by both the probe and parse."""
    correct = nmea.checksum(body)
    sentence = f"${body}*{_wrong_checksum(correct)}"
    assert nmea.has_valid_checksum(sentence) is False
    with pytest.raises(nmea.NmeaError):
        nmea.parse(sentence)


@given(_bodies, st.integers(min_value=0, max_value=59))
def test_body_corruption_breaks_checksum(body: str, pos: int) -> None:
    """Corrupting a body character while keeping the old checksum is detected."""
    correct = nmea.checksum(body)
    pos %= len(body)
    orig = body[pos]
    # Flip to a different safe char so the checksum genuinely changes.
    swapped = "X" if orig != "X" else "Y"
    corrupted = body[:pos] + swapped + body[pos + 1 :]
    assume(nmea.checksum(corrupted) != correct)  # avoid the rare XOR collision
    sentence = f"${corrupted}*{correct}"
    assert nmea.has_valid_checksum(sentence) is False
    with pytest.raises(nmea.NmeaError):
        nmea.parse(sentence)


@given(_bodies)
def test_no_checksum_requires_flag(body: str) -> None:
    """A checksum-less sentence is accepted by default but rejected when required."""
    sentence = "$" + body
    assume("*" not in sentence)
    with pytest.raises(nmea.NmeaError):
        nmea.parse(sentence, require_checksum=True)


# --------------------------------------------------------------------------- #
# 3. Round-trip: everything the encoder emits parses back
# --------------------------------------------------------------------------- #
_lats = st.floats(min_value=-89.0, max_value=89.0, allow_nan=False, allow_infinity=False)
_lons = st.floats(min_value=-179.0, max_value=179.0, allow_nan=False, allow_infinity=False)
_headings = st.floats(min_value=0.0, max_value=359.9, allow_nan=False, allow_infinity=False)


@given(_lats, _lons,
       st.floats(min_value=0.0, max_value=99.0, allow_nan=False),
       _headings)
def test_roundtrip_rmc(lat: float, lon: float, sog: float, cog: float) -> None:
    s = nmea.encode_rmc(GeoPoint(lat, lon), sog, cog)
    assert nmea.has_valid_checksum(s)
    p = nmea.parse(s)
    assert isinstance(p, nmea.RMC)
    assert p.valid
    assert p.point.lat == pytest.approx(lat, abs=1e-3)
    assert p.point.lon == pytest.approx(lon, abs=1e-3)
    # Encoders format at 0.1 resolution (":.1f"), so a value on the half-step
    # (e.g. x.x5) rounds with an error of exactly 0.05; tolerate that half-step
    # (plus float slop) so the roundtrip asserts fidelity to the wire resolution.
    assert p.sog_knots == pytest.approx(sog, abs=0.06)
    assert p.cog_deg == pytest.approx(cog, abs=0.06)


@given(_lats, _lons,
       st.integers(min_value=0, max_value=12),
       st.integers(min_value=0, max_value=4))
def test_roundtrip_gga(lat: float, lon: float, sats: int, quality: int) -> None:
    s = nmea.encode_gga(GeoPoint(lat, lon), satellites=sats, quality=quality)
    assert nmea.has_valid_checksum(s)
    p = nmea.parse(s)
    assert isinstance(p, nmea.GGA)
    assert p.point.lat == pytest.approx(lat, abs=1e-3)
    assert p.point.lon == pytest.approx(lon, abs=1e-3)
    assert p.satellites == sats
    assert p.fix_quality == quality


@given(_headings)
def test_roundtrip_hdm(hdg: float) -> None:
    p = nmea.parse(nmea.encode_hdm(hdg))
    assert isinstance(p, nmea.Heading)
    assert p.reference == "M"
    assert p.heading_deg == pytest.approx(hdg, abs=0.06)


@given(_headings)
def test_roundtrip_hdt(hdg: float) -> None:
    p = nmea.parse(nmea.encode_hdt(hdg))
    assert isinstance(p, nmea.Heading)
    assert p.reference == "T"
    assert p.heading_deg == pytest.approx(hdg, abs=0.06)


@given(st.floats(min_value=0.0, max_value=500.0, allow_nan=False, allow_infinity=False))
def test_roundtrip_dpt(depth: float) -> None:
    s = nmea.encode_dpt(depth)
    assert nmea.has_valid_checksum(s)
    p = nmea.parse(s)
    assert isinstance(p, nmea.Depth)
    assert p.depth_m == pytest.approx(depth, abs=0.06)


@given(st.floats(min_value=-500.0, max_value=500.0, allow_nan=False, allow_infinity=False),
       st.sampled_from(["L", "R"]),
       _headings)
def test_roundtrip_apb(xte: float, steer: str, brg: float) -> None:
    s = nmea.encode_apb(xte, steer, brg)
    assert nmea.has_valid_checksum(s)
    p = nmea.parse(s)
    assert isinstance(p, nmea.APB)
    assert p.steer_to == steer
    assert p.cross_track_m == pytest.approx(abs(xte), abs=0.06)
    assert p.bearing_to_dest == pytest.approx(brg, abs=0.06)


# --------------------------------------------------------------------------- #
# 4. Fuzzing a valid sentence with random single-char mutations
# --------------------------------------------------------------------------- #
@settings(max_examples=500)
@given(st.data())
def test_mutated_valid_sentence_never_raises_unexpected(data: st.DataObject) -> None:
    """Take a real sentence, mutate one character to an arbitrary byte, and
    confirm the parser still only ever returns or raises NmeaError."""
    base = nmea.encode_rmc(GeoPoint(48.0, 11.0), 5.0, 90.0)
    idx = data.draw(st.integers(min_value=0, max_value=len(base) - 1))
    ch = data.draw(st.characters(blacklist_categories=("Cs",)))
    mutated = base[:idx] + ch + base[idx + 1 :]
    try:
        nmea.parse(mutated)
    except nmea.NmeaError:
        pass
