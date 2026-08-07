"""The I2C magnetometer compass DRIVER: registration, i2c target parsing, HDT
emission, declination modes, the spin-to-calibrate flow (+ persistence), and the
device menu. Uses a fake bus -- no smbus2, no hardware."""

import math

import pytest

from vanchor.hardware import registry
from vanchor.hardware.drivers import load_drivers
from vanchor.hardware.drivers.magnetometer import (MagnetometerCompass,
                                                   parse_i2c_target)
from vanchor.nav import magnetometer as m
from vanchor.nav import nmea


class FakeBus:
    def __init__(self, reads):
        self.reads = reads
        self.closed = False

    def write_byte_data(self, addr, reg, value):
        pass

    def read_byte_data(self, addr, reg):
        return self.reads[(addr, reg)][0]

    def read_block_data(self, addr, reg, length):
        if (addr, reg) not in self.reads:
            raise OSError("nak")
        return self.reads[(addr, reg)][:length]

    def close(self):
        self.closed = True


def _le16(v):
    v &= 0xFFFF
    return bytes([v & 0xFF, (v >> 8) & 0xFF])


def _qmc_bus(x=100, y=0, z=0):
    return FakeBus({(0x0D, 0x0D): b"\xff",
                    (0x0D, 0x00): _le16(x) + _le16(y) + _le16(z)})


class _RecordBus:
    def __init__(self):
        self.events = []

    async def publish(self, topic, payload):
        self.events.append((topic, payload))


# --- registration + parsing ------------------------------------------------ #

def test_registers_as_a_compass_source():
    load_drivers()
    assert registry.has("compass", "magnetometer")
    assert "magnetometer" in registry.sources("compass")


@pytest.mark.parametrize("port,expected", [
    ("i2c:1:0x0d", (1, 0x0D)),
    ("i2c:2:26", (2, 26)),
    ("i2c:1", (1, None)),
    ("3", (3, None)),
    ("/dev/ttyUSB1", (1, None)),   # non-i2c -> default bus, autodetect addr
    ("", (1, None)),
])
def test_parse_i2c_target(port, expected):
    assert parse_i2c_target(port) == expected


# --- detect + emit HDT ----------------------------------------------------- #

def test_open_and_detect_finds_chip():
    d = MagnetometerCompass(lambda: _qmc_bus())
    assert d._open_and_detect() is True
    assert d.detected == "QMC5883L"


async def test_sample_once_emits_hdt():
    # Field along bow (x=100) -> heading 0 -> HDT 000.0, off-mode (no declination).
    d = MagnetometerCompass(lambda: _qmc_bus(x=100, y=0), declination_mode="off")
    assert d._open_and_detect() is True
    assert await d.sample_once(0.2) == nmea.encode_hdt(0.0)
    # Field to port (y=-100) -> east -> 090.
    d2 = MagnetometerCompass(lambda: _qmc_bus(x=0, y=-100), declination_mode="off")
    d2._open_and_detect()
    assert await d2.sample_once(0.2) == nmea.encode_hdt(90.0)


async def test_manual_declination_is_added():
    d = MagnetometerCompass(lambda: _qmc_bus(x=100, y=0),
                            declination_mode="manual", manual_declination_deg=7.0)
    d._open_and_detect()
    assert await d.sample_once(0.2) == nmea.encode_hdt(7.0)


async def test_loop_publishes_nmea_then_stops():
    bus = _RecordBus()
    d = MagnetometerCompass(lambda: _qmc_bus(x=100, y=0), bus=bus,
                            declination_mode="off", hz=50.0)
    await d.start()
    try:
        for _ in range(50):
            if bus.events:
                break
            await _sleep(0.02)
    finally:
        await d.stop()
    topics = {t for t, _ in bus.events}
    assert "nmea.in" in {str(t) for t in topics} or any(
        "HDT" in str(p) for _, p in bus.events)


async def _sleep(s):
    import asyncio
    await asyncio.sleep(s)


# --- calibration flow + persistence ---------------------------------------- #

def test_calibrate_flow_fits_and_persists():
    saved = {}
    d = MagnetometerCompass(lambda: _qmc_bus(), declination_mode="off",
                            persist_cal=lambda c: saved.update(c))
    d._open_and_detect()
    assert d.run_action("calibrate_start")["ok"] is True
    # Feed a full circle with a hard-iron bias through the read path.
    ox, oy = 40.0, -25.0
    for k in range(72):
        t = math.radians(k * 5)
        d._heading_from_raw((int(ox + 100 * math.cos(t)),
                             int(oy + 100 * math.sin(t)), 5), 0.2)
    res = d.run_action("calibrate_stop")
    assert res["ok"] is True and "calibration" in res
    assert d.calibration.offset[0] == pytest.approx(ox, abs=2.0)
    assert saved.get("offset")[1] == pytest.approx(oy, abs=2.0)   # persisted


def test_calibrate_stop_without_motion_reports_and_does_not_persist():
    saved = {}
    d = MagnetometerCompass(lambda: _qmc_bus(),
                            persist_cal=lambda c: saved.update(c))
    d._open_and_detect()
    d.run_action("calibrate_start")
    for _ in range(30):
        d._heading_from_raw((10, 20, 5), 0.2)          # no rotation
    res = d.run_action("calibrate_stop")
    assert res["ok"] is False and not saved


def test_calibrate_start_requires_a_detected_chip():
    d = MagnetometerCompass(lambda: FakeBus({}))       # nothing on the bus
    assert d.run_action("calibrate_start")["ok"] is False


# --- raw dump (remote troubleshooting) ------------------------------------- #

def test_dump_raw_action_returns_hex_dump():
    d = MagnetometerCompass(lambda: _qmc_bus(x=100, y=0), bus_num=1)
    r = d.run_action("dump_raw")
    assert r["ok"] is True and "dump" in r
    assert "QMC5883L" in r["dump"] and "bus /dev/i2c-1" in r["dump"]


def test_dump_raw_reports_bus_open_failure():
    def boom():
        raise RuntimeError("i2c extra not installed")
    r = MagnetometerCompass(boom).run_action("dump_raw")
    assert r["ok"] is False and "Could not open" in r["message"]


# --- first-run calibration nudge ------------------------------------------- #

def test_menu_notice_nudges_until_calibrated():
    d = MagnetometerCompass(lambda: _qmc_bus())
    d._open_and_detect()                                  # detected, not calibrated
    assert "Not calibrated" in d.device_menu()["notice"]
    d.apply_setting("calibration", {"offset": [5, 5, 5], "scale": [1, 1, 1]})
    assert d.device_menu()["notice"] == ""               # cleared once calibrated


def test_menu_notice_when_nothing_detected():
    d = MagnetometerCompass(lambda: FakeBus({}))
    assert "No magnetometer detected" in d.device_menu()["notice"]


# --- device menu ----------------------------------------------------------- #

def test_device_menu_and_apply_setting():
    d = MagnetometerCompass(lambda: _qmc_bus())
    menu = d.device_menu()
    assert menu["device"] == "compass"
    keys = {s["key"] for s in menu["settings"]}
    assert {"declination_mode", "hz", "forward_axis", "invert"} <= keys
    assert d.apply_setting("hz", 10)["ok"] is True and d.hz == 10
    assert d.apply_setting("declination_mode", "manual")["ok"] is True
    assert d.apply_setting("calibration", {"offset": [1, 2, 3], "scale": [1, 1, 1]})["ok"]
    assert d.calibration.offset == (1.0, 2.0, 3.0)
    assert d.apply_setting("bogus", 1)["ok"] is False
