"""Protocol v2 line integrity: CRC-8 *HH suffix on the motor/steering link.

The same golden vectors (firmware/common/protocol_vectors.txt) are exercised
by the firmware's host-compiled parser test, so the two CRC implementations
can never drift apart silently.
"""
from pathlib import Path

from vanchor.hardware.serial_devices import parse_engine_status, parse_steering_feedback
from vanchor.hardware.serial_link import append_crc, crc8, strip_verify_crc

VECTORS = Path(__file__).resolve().parent.parent / "firmware" / "common" / "protocol_vectors.txt"


def _vectors():
    out = []
    for raw in VECTORS.read_text().splitlines():
        raw = raw.rstrip()
        if not raw or raw.startswith("#"):
            continue
        verdict, line = raw.split(None, 1)
        out.append((verdict, line))
    return out


def test_vector_file_exists_and_parses():
    vs = _vectors()
    assert len(vs) >= 10
    assert {v for v, _ in vs} == {"OK", "BAD", "NOCRC"}


def test_golden_vectors_roundtrip():
    for verdict, line in _vectors():
        payload, ok = strip_verify_crc(line)
        if verdict == "OK":
            assert ok is True, line
            assert append_crc(payload) == line     # regenerate == golden
        elif verdict == "BAD":
            assert ok is False, line
        else:  # NOCRC
            assert ok is None, line
            assert payload == line


def test_crc8_known_values():
    assert crc8("CMD 0 F 0") == 0xDC
    assert crc8("") == 0x00


def test_feedback_parser_rejects_bad_crc_accepts_good_and_absent():
    good = append_crc("A -12.4 1 -7 42")
    fb = parse_steering_feedback(good)
    assert fb is not None and fb.angle_deg == -12.4 and fb.seq == 42
    assert parse_steering_feedback("A -12.4 1 -7 42*C9") is None      # corrupted
    legacy = parse_steering_feedback("A -12.4 1 -7 42")               # old firmware
    assert legacy is not None and legacy.angle_deg == -12.4


def test_engine_parser_same_rules():
    good = append_crc("E 128 F RUN 42")
    st = parse_engine_status(good)
    assert st is not None and st.pwm == 128 and st.state == "RUN" and st.seq == 42
    assert parse_engine_status("E 128 F RUN 42*00") is None
    assert parse_engine_status("E 128 F RUN 42") is not None


def test_star_in_payload_is_not_mistaken_for_crc():
    # '*' not followed by exactly two hex chars is payload, not a suffix
    assert strip_verify_crc("A 1.0 1 0 *x7")[1] is None
    assert strip_verify_crc("CMD 0 F 0*7")[1] is None
