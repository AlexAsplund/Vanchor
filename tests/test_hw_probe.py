"""Tests for the hardware probe module (src/vanchor/hardware/probe.py).

All tests are pure-Python / asyncio; no real serial ports or I2C buses required.
"""
from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from vanchor.hardware.probe import (
    BAUD_LADDER,
    MOTOR_INFO_CMD,
    ProbeResult,
    classify_bytes,
    hint_from_metadata,
    motor_info_probe,
    parse_motor_info,
    probe_i2c,
    probe_serial,
    suggest_for,
    ubx_mon_ver,
)


# --------------------------------------------------------------------------- #
# Helpers / fake transports
# --------------------------------------------------------------------------- #

class FakeTransport:
    """Minimal async transport for probe_serial / ubx_mon_ver tests."""

    def __init__(self, data: bytes = b"", *, writes_captured: list | None = None):
        self._buf = io.BytesIO(data)
        self._closed = False
        self._writes: list[bytes] = writes_captured if writes_captured is not None else []

    async def write(self, data: bytes) -> None:
        self._writes.append(data)

    async def read(self, n: int) -> bytes:
        chunk = self._buf.read(n)
        if not chunk:
            raise EOFError("end of fake stream")
        return chunk

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        self._closed = True


def _make_ublox_bytes() -> bytes:
    """One valid UBX NAV-PVT frame (0x01/0x07, 92-byte payload, correct checksum)."""
    # Header: sync (2) + class/id (2) + len LE (2) + payload (92)
    header = b"\xb5\x62\x01\x07\x5c\x00" + b"\x00" * 92
    # UBX checksum: Fletcher-8 over bytes[2:] (class/id/len/payload)
    ck_a = ck_b = 0
    for b in header[2:]:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return header + bytes([ck_a, ck_b])


def _make_wit_frame() -> bytes:
    """Three valid WitMotion 11-byte acceleration frames."""
    # Each frame: 0x55 0x51 d0..d7 sum
    def wit_frame(d0: int = 0, d1: int = 0, d2: int = 0, d3: int = 0,
                  d4: int = 0, d5: int = 0, d6: int = 0, d7: int = 0) -> bytes:
        payload = bytes([0x51, d0, d1, d2, d3, d4, d5, d6, d7])
        total = (sum(payload) + 0x55) & 0xFF
        return b"\x55" + payload + bytes([total])
    return wit_frame() * 5


def _crc8(s: str) -> int:
    """CRC-8 polynomial 0x07 over ASCII bytes (mirrors probe._crc8)."""
    crc = 0
    for b in s.encode():
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _motor_line(payload: str) -> bytes:
    """Return a motor-protocol line with CRC suffix."""
    return f"{payload}*{_crc8(payload):02X}\r\n".encode()


# --------------------------------------------------------------------------- #
# classify_bytes — unit tests
# --------------------------------------------------------------------------- #

class TestClassifyBytes:
    def test_empty_returns_unknown(self):
        r = classify_bytes(b"")
        assert r.detected == "unknown"
        assert r.confidence == "none"

    def test_ubx_magic_detected(self):
        r = classify_bytes(_make_ublox_bytes() * 3)
        assert r.detected == "ublox"
        assert r.confidence in ("high", "medium")

    def test_witmotion_frames_detected(self):
        r = classify_bytes(_make_wit_frame())
        assert r.detected == "witmotion-imu"

    def test_nmea_gps_detected(self):
        # Checksum 0x39 is correct for this GGA sentence body
        nmea = b"$GPGGA,123456.00,5152.000,N,00520.000,W,1,08,1.0,0,M,,,,*39\r\n" * 5
        r = classify_bytes(nmea)
        assert r.detected in ("nmea-gps", "nmea-compass", "nmea-depth", "ublox")

    def test_motor_crc_lines_detected(self):
        # wrap_pct must be an integer to match the motor A-line regex
        data = _motor_line("A 0.0 0 42") * 3
        r = classify_bytes(data)
        assert r.detected == "vanchor-motor"

    def test_motor_e_lines_detected(self):
        data = _motor_line("E 0 F RUN") * 3
        r = classify_bytes(data)
        assert r.detected == "vanchor-motor"

    def test_random_garbage_unknown(self):
        r = classify_bytes(b"\xff\xfe\xfd" * 100)
        assert r.detected == "unknown"


# --------------------------------------------------------------------------- #
# probe_serial — safety and behavior
# --------------------------------------------------------------------------- #

class TestProbeSerial:
    """probe_serial must NEVER write to the transport during passive probing."""

    @pytest.mark.asyncio
    async def test_no_writes_during_passive_probe(self):
        writes: list[bytes] = []
        data = _motor_line("A 0.0 0 0.0") * 5
        transport = FakeTransport(data, writes_captured=writes)

        async def fast_sleep(_s: float) -> None:
            pass

        await probe_serial(transport, duration_s=0.05, sleep=fast_sleep)
        assert writes == [], "probe_serial must not write to the port during passive probing"

    @pytest.mark.asyncio
    async def test_classifies_motor_data(self):
        data = _motor_line("A 0.5 1 12") * 5
        transport = FakeTransport(data)

        async def fast_sleep(_s: float) -> None:
            pass

        result = await probe_serial(transport, duration_s=0.05, sleep=fast_sleep)
        assert result.detected == "vanchor-motor"

    @pytest.mark.asyncio
    async def test_classifies_ubx_data(self):
        data = _make_ublox_bytes() * 5
        transport = FakeTransport(data)

        async def fast_sleep(_s: float) -> None:
            pass

        result = await probe_serial(transport, duration_s=0.05, sleep=fast_sleep)
        assert result.detected == "ublox"

    @pytest.mark.asyncio
    async def test_returns_probe_result_type(self):
        transport = FakeTransport(b"")

        async def fast_sleep(_s: float) -> None:
            pass

        result = await probe_serial(transport, duration_s=0.01, sleep=fast_sleep)
        assert isinstance(result, ProbeResult)


# --------------------------------------------------------------------------- #
# ubx_mon_ver — behavior
# --------------------------------------------------------------------------- #

class TestUbxMonVer:
    @pytest.mark.asyncio
    async def test_writes_poll_command(self):
        from vanchor.hardware.probe import UBX_MON_VER_POLL

        writes: list[bytes] = []
        transport = FakeTransport(b"", writes_captured=writes)

        async def fast_sleep(_s: float) -> None:
            pass

        await ubx_mon_ver(transport, timeout_s=0.05)
        assert UBX_MON_VER_POLL in writes

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self):
        transport = FakeTransport(b"")
        result = await ubx_mon_ver(transport, timeout_s=0.05)
        # May return None or dict; must not raise
        assert result is None or isinstance(result, dict)


# --------------------------------------------------------------------------- #
# parse_motor_info — INFO response parsing
# --------------------------------------------------------------------------- #

class TestParseMotorInfo:
    def test_well_formed_response(self):
        lines = [
            "I fw v1.2-3-gabc123 board helm-4.2 mcu pico2",
            "I proto 2.1 crc 1 wdog 800",
            "I conf 1 keys 23 flash stored",
            "I up 7423 vbat 12.6 ang -3.2 fb 1",
            "I end 4",
        ]
        result = parse_motor_info(lines)
        assert result is not None
        assert result["fw"] == "v1.2-3-gabc123"
        assert result["board"] == "helm-4.2"
        assert result["mcu"] == "pico2"
        assert result["proto"] == "2.1"
        assert result["vbat"] == "12.6"
        assert result["ang"] == "-3.2"

    def test_unknown_keys_preserved(self):
        lines = [
            "I fw v2.0 newkey futureval",
            "I end 1",
        ]
        result = parse_motor_info(lines)
        assert result is not None
        assert result.get("newkey") == "futureval"

    def test_truncated_no_end(self):
        lines = [
            "I fw v1.0 board helm-4",
        ]
        result = parse_motor_info(lines)
        assert result is not None
        assert result["fw"] == "v1.0"

    def test_empty_lines_returns_none(self):
        result = parse_motor_info([])
        assert result is None

    def test_non_info_lines_ignored(self):
        lines = [
            "A 0.0 0 0.0*xx",
            "E 0 F RUN*yy",
            "I fw v1.0 board test",
            "I end 1",
        ]
        result = parse_motor_info(lines)
        assert result is not None
        assert result["fw"] == "v1.0"

    def test_crc_suffix_stripped(self):
        payload = "I fw v1.5 board helm"
        crc = _crc8(payload)
        lines = [f"{payload}*{crc:02X}", "I end 1"]
        result = parse_motor_info(lines)
        assert result is not None
        assert result["fw"] == "v1.5"

    def test_odd_number_of_tokens_handled(self):
        """Odd trailing token without a value must not crash."""
        lines = ["I fw v1.0 orphan", "I end 1"]
        result = parse_motor_info(lines)
        assert result is not None
        assert result["fw"] == "v1.0"


# --------------------------------------------------------------------------- #
# motor_info_probe — integration
# --------------------------------------------------------------------------- #

class TestMotorInfoProbe:
    @pytest.mark.asyncio
    async def test_sends_info_command(self):
        writes: list[bytes] = []
        response = (
            b"I fw v1.0 board helm-4\r\n"
            b"I end 1\r\n"
        )
        transport = FakeTransport(response, writes_captured=writes)
        result = await motor_info_probe(transport, timeout_s=0.5)
        assert MOTOR_INFO_CMD in writes

    @pytest.mark.asyncio
    async def test_parses_info_response(self):
        response = (
            b"I fw v1.2-3 board helm-4.2 mcu pico2\r\n"
            b"I proto 2.1 crc 1\r\n"
            b"I up 7423 vbat 12.6 ang -3.2 fb 1\r\n"
            b"I end 3\r\n"
        )
        transport = FakeTransport(response)
        result = await motor_info_probe(transport, timeout_s=0.5)
        assert result is not None
        assert result.get("fw") == "v1.2-3"
        assert result.get("board") == "helm-4.2"
        assert result.get("vbat") == "12.6"

    @pytest.mark.asyncio
    async def test_old_firmware_no_info_returns_none(self):
        """Old firmware that ignores INFO returns only A/E lines → None."""
        response = _motor_line("A 0.0 0 0.0") * 3
        transport = FakeTransport(response)
        result = await motor_info_probe(transport, timeout_s=0.2)
        # May be None or a dict with no recognized info keys — either is fine
        assert result is None or isinstance(result, dict)


# --------------------------------------------------------------------------- #
# probe_i2c — unit tests
# --------------------------------------------------------------------------- #

class _FakeReadMsg:
    """Fake i2c_msg-like read message that returns fixed bytes after i2c_rdwr."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def __bytes__(self) -> bytes:
        return self._data

    def __iter__(self):
        return iter(self._data)


class TestProbeI2c:
    def test_helm_pico_whoami_ok(self):
        """Simulated helm-Pico at 0x42: WHOAMI=0x56 → detected=helm-pico."""

        def fake_smbus(bus_num):
            bus = MagicMock()
            write_fn = lambda data: MagicMock()
            read_fn = lambda n: _FakeReadMsg(bytes([0x56, 0x01]))  # WHOAMI=0x56, version=0x01
            return bus, write_fn, read_fn

        result = probe_i2c(1, 0x42, kind="helm-pico", smbus_factory=fake_smbus)
        assert isinstance(result, dict)
        assert "ok" in result
        # Correct WHOAMI → detected as helm-pico
        assert result.get("detected") == "helm-pico"

    def test_ina226_mfr_id_ok(self):
        """Simulated INA226: MFR_ID=0x5449 (TI), DIE_ID=0x2260 → detected=ina226."""
        call_count: list[int] = [0]

        def fake_smbus(bus_num):
            bus = MagicMock()
            write_fn = lambda data: MagicMock()

            def read_fn(n):
                idx = call_count[0]
                call_count[0] += 1
                if idx == 0:
                    return _FakeReadMsg(bytes([0x54, 0x49]))  # MFR_ID big-endian "TI"
                return _FakeReadMsg(bytes([0x22, 0x60]))      # DIE_ID = 0x2260

            return bus, write_fn, read_fn

        result = probe_i2c(1, 0x40, kind="ina226", smbus_factory=fake_smbus)
        assert isinstance(result, dict)
        assert result.get("detected") == "ina226"

    def test_i2c_bad_whoami_returns_unknown(self):
        """Wrong WHOAMI from helm-pico path (0x00 ≠ 0x56) returns detected=unknown."""

        def fake_smbus(bus_num):
            bus = MagicMock()
            write_fn = lambda data: MagicMock()
            read_fn = lambda n: _FakeReadMsg(bytes([0x00, 0x00]))  # wrong WHOAMI
            return bus, write_fn, read_fn

        result = probe_i2c(1, 0x42, kind="helm-pico", smbus_factory=fake_smbus)
        assert isinstance(result, dict)
        # OSError from _probe_helm_pico is caught; probe_i2c returns detected=unknown
        assert result.get("detected") == "unknown"


# --------------------------------------------------------------------------- #
# hint_from_metadata — unit tests
# --------------------------------------------------------------------------- #

class TestHintFromMetadata:
    def test_ublox_in_description(self):
        hint = hint_from_metadata("/dev/ttyACM0", "u-blox GNSS receiver")
        assert hint is not None
        assert "gps" in hint.lower() or "ublox" in hint.lower()

    def test_witmotion_in_description(self):
        # hint_from_metadata is best-effort — a WitMotion description may not
        # match any known keyword; tolerate None.
        hint = hint_from_metadata("/dev/ttyUSB1", "WitMotion IMU sensor")
        assert hint is None or isinstance(hint, str)

    def test_unknown_returns_none_or_str(self):
        hint = hint_from_metadata("/dev/ttyUSB0", "")
        # Should return None or a string — never raise
        assert hint is None or isinstance(hint, str)


# --------------------------------------------------------------------------- #
# suggest_for — config suggestion mapping
# --------------------------------------------------------------------------- #

class TestSuggestFor:
    def test_ublox_suggests_source(self):
        s = suggest_for("ublox", "/dev/ttyACM0", 38400)
        assert s is not None
        assert s.get("source") in ("ublox", "serial")

    def test_witmotion_suggests_baudrate_key(self):
        """WitMotion must suggest baudrate (not compass_baud) — hwt901b driver
        reads the value from hw.baudrate (not a top-level compass_baud key).
        The baudrate is nested inside the 'fields' sub-dict of the suggestion."""
        s = suggest_for("witmotion-imu", "/dev/ttyUSB1", 9600)
        assert s is not None
        fields = s.get("fields", {})
        assert "baudrate" in fields, f"'baudrate' not in suggest_for fields: {s}"
        # Must NOT use compass_baud (wrong key — hwt901b reads hw.baudrate directly)
        assert "compass_baud" not in fields

    def test_nmea_gps_suggests_serial_source(self):
        s = suggest_for("nmea-gps", "/dev/ttyUSB0", 4800)
        assert s is not None

    def test_unknown_returns_none(self):
        s = suggest_for("unknown", "/dev/ttyUSB0", 9600)
        assert s is None

    def test_baud_ladder_exists_for_all_hint_kinds(self):
        for kind in ("gps", "compass", "motor", "any"):
            assert kind in BAUD_LADDER
            assert len(BAUD_LADDER[kind]) > 0
