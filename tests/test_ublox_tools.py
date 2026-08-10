"""u-blox toolbox service: stats sampling + settings apply, driven by a fake
async transport (no real serial port)."""
from __future__ import annotations

import struct

from vanchor.nav import ubx
from vanchor.runtime import ublox_tools as tools


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, d: float) -> None:  # advances virtual time
        self.t += d


class FakeTransport:
    """Yields queued read chunks in order, then empties. Records writes."""

    def __init__(self, reads: list[bytes]) -> None:
        self._reads = list(reads)
        self.written: list[bytes] = []
        self.opened = False
        self.closed = False

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def write(self, b: bytes) -> None:
        self.written.append(bytes(b))

    async def read(self, n: int = 4096) -> bytes:
        return self._reads.pop(0) if self._reads else b""


def _nav_pvt(*, fix_type: int, valid: bool, num_sv: int, lat: float, lon: float) -> bytes:
    p = bytearray(92)
    p[20] = fix_type
    p[21] = 0x01 if valid else 0x00
    p[23] = num_sv
    struct.pack_into("<i", p, 24, round(lon * 1e7))
    struct.pack_into("<i", p, 28, round(lat * 1e7))
    return ubx.build_frame(*ubx.NAV_PVT, bytes(p))


def _mon_ver() -> bytes:
    payload = (b"ROM 5.10".ljust(30, b"\x00") + b"00080000".ljust(10, b"\x00")
               + b"PROTVER=27.11".ljust(30, b"\x00"))
    return ubx.build_frame(*ubx.MON_VER, payload)


async def test_read_stats_summarises_nmea_ubx_fix_and_version() -> None:
    clk = FakeClock()
    stream = (b"$GNRMC,,A,,,,,,,,,*00\r\n$GNGGA,,,,,,,*00\r\n"
              + _nav_pvt(fix_type=3, valid=True, num_sv=9, lat=59.0, lon=18.0)
              + _mon_ver())
    tr = FakeTransport([stream])
    out = await tools.read_stats(tr, duration_s=1.0, clock=clk.now, sleep=clk.sleep)
    assert tr.opened and tr.closed
    assert tr.written and tr.written[0] == ubx.poll(*ubx.MON_VER)  # polled MON-VER
    assert out["protocols"] == {"nmea": True, "ubx": True}
    assert out["counters"]["nmea_sentences"] == 2
    assert out["fix"]["valid"] is True and out["fix"]["num_sv"] == 9
    assert abs(out["fix"]["lat"] - 59.0) < 1e-6
    assert out["version"]["sw"] == "ROM 5.10"


async def test_read_stats_quiet_port_is_ok_with_zero_counters() -> None:
    clk = FakeClock()
    out = await tools.read_stats(FakeTransport([]), duration_s=0.5,
                                 clock=clk.now, sleep=clk.sleep)
    assert out["ok"] is True
    assert out["protocols"] == {"nmea": False, "ubx": False}
    assert out["fix"] is None and out["counters"]["nmea_sentences"] == 0


async def test_apply_settings_sends_valsets_and_collects_acks() -> None:
    clk = FakeClock()
    # The transport ACKs each write (one ACK frame available per read).
    ack = ubx.build_frame(*ubx.ACK_ACK, b"\x06\x8a")
    tr = FakeTransport([ack, ack])
    out = await tools.apply_settings(tr, nmea=True, rate_hz=5.0,
                                     clock=clk.now, sleep=clk.sleep)
    assert out["ok"] is True
    assert out["acks"] == {"nmea": True, "rate": True}
    # Two VALSET frames were written (NMEA on, rate 5 Hz).
    sent = [ubx.parse_stream(w)[0][0] for w in tr.written]
    assert all((c, i) == ubx.CFG_VALSET for c, i, _ in sent)


async def test_apply_settings_reports_missing_ack_as_null() -> None:
    clk = FakeClock()
    tr = FakeTransport([])  # receiver never ACKs
    out = await tools.apply_settings(tr, nmea=False, ack_timeout=0.2,
                                     clock=clk.now, sleep=clk.sleep)
    assert out["ok"] is False
    assert out["acks"] == {"nmea": None}


async def test_apply_settings_nothing_to_set() -> None:
    out = await tools.apply_settings(FakeTransport([]))
    assert out["ok"] is False and "nothing" in out["error"]


async def test_apply_settings_bad_value_raises_before_sending() -> None:
    import pytest
    tr = FakeTransport([])
    with pytest.raises(ValueError):
        await tools.apply_settings(tr, baud=12345)
    assert tr.written == []  # nothing sent when the plan can't be built
