"""The pure I2C-magnetometer support: multi-chip autodetection, register decode
(byte order + axis order per chip), heading convention, and hard/soft-iron
calibration. All driven by a fake bus -- no smbus2, no hardware."""

import math

import pytest

from vanchor.nav import magnetometer as m


class FakeBus:
    """Maps (addr, reg) -> bytes for id/data reads; records writes. Unknown
    (addr, reg) raises OSError, standing in for an I2C NAK (no device)."""

    def __init__(self, reads: dict) -> None:
        self.reads = reads          # {(addr, reg): bytes}
        self.writes: list = []

    def write_byte_data(self, addr, reg, value):
        self.writes.append((addr, reg, value))

    def read_byte_data(self, addr, reg):
        return self.reads[(addr, reg)][0]

    def read_block_data(self, addr, reg, length):
        if (addr, reg) not in self.reads:
            raise OSError("nak")
        return self.reads[(addr, reg)][:length]


def _be16(v):    # signed -> big-endian 2 bytes (MSB first)
    return bytes([(v >> 8) & 0xFF, v & 0xFF]) if v >= 0 else _be16(v + 0x10000)


def _le16(v):    # signed -> little-endian 2 bytes (LSB first)
    b = _be16(v)
    return bytes([b[1], b[0]])


# --- detection ------------------------------------------------------------- #

def test_detect_qmc5883l():
    bus = FakeBus({(0x0D, 0x0D): b"\xff", (0x0D, 0x00): b"\x00" * 6})
    spec, addr = m.detect(bus)
    assert spec.name == "QMC5883L" and addr == 0x0D


def test_detect_hmc5883l():
    bus = FakeBus({(0x1E, 0x0A): b"H43", (0x1E, 0x03): b"\x00" * 6})
    spec, addr = m.detect(bus)
    assert spec.name == "HMC5883L" and addr == 0x1E


def test_detect_ist8310():
    bus = FakeBus({(0x0E, 0x00): b"\x10", (0x0E, 0x03): b"\x00" * 6})
    spec, addr = m.detect(bus)
    assert spec.name == "IST8310" and addr == 0x0E


def test_detect_none_when_nothing_answers():
    assert m.detect(FakeBus({})) is None


def test_detect_respects_address_filter():
    # A QMC is present, but we only allow the HMC address -> not found.
    bus = FakeBus({(0x0D, 0x0D): b"\xff"})
    assert m.detect(bus, addresses=(0x1E,)) is None


def test_detect_explicit_address_finds_chip_off_its_default_address():
    # An IST8310 strapped to a NON-default address (0x0C via its CAD pins). An
    # explicit address must probe EVERY chip's id there -- and the weak 0xFF
    # (QMC) signature must not win over IST's specific 0x10, even though reg 0x0D
    # here reads 0xFF.
    bus = FakeBus({(0x0C, 0x00): b"\x10", (0x0C, 0x0D): b"\xff",
                   (0x0C, 0x03): b"\x00" * 6})
    spec, addr = m.detect(bus, addresses=(0x0C,))
    assert spec.name == "IST8310" and addr == 0x0C
    # ...but at that chip's DEFAULT addresses autodetect finds nothing.
    assert m.detect(bus) is None


# --- register decode (byte order + axis order) ----------------------------- #

def test_qmc_decode_little_endian_xyz():
    # QMC data at 0x00 is little-endian in X, Y, Z order.
    data = _le16(100) + _le16(200) + _le16(-50)
    bus = FakeBus({(0x0D, 0x0D): b"\xff", (0x0D, 0x00): data})
    spec, addr = m.detect(bus)
    mag = m.Magnetometer(bus, spec, addr)
    mag.configure()
    assert mag.read_raw() == (100, 200, -50)
    assert (0x0D, 0x09) in [(w[0], w[1]) for w in bus.writes]   # control reg written


def test_hmc_decode_big_endian_x_z_y_order():
    # HMC streams big-endian in X, Z, Y order; read_raw must return (X, Y, Z).
    data = _be16(100) + _be16(300) + _be16(-200)   # X, Z, Y on the wire
    bus = FakeBus({(0x1E, 0x0A): b"H43", (0x1E, 0x03): data})
    spec, addr = m.detect(bus)
    mag = m.Magnetometer(bus, spec, addr)
    assert mag.read_raw() == (100, -200, 300)      # -> (X, Y, Z)


# --- heading convention (CW from north; X=bow, Y=starboard) ---------------- #

@pytest.mark.parametrize("xyz,expected", [
    ((100.0, 0.0, 0.0), 0.0),      # field along bow -> north
    ((0.0, -100.0, 0.0), 90.0),    # -> east
    ((-100.0, 0.0, 0.0), 180.0),   # -> south
    ((0.0, 100.0, 0.0), 270.0),    # -> west
])
def test_heading_convention(xyz, expected):
    assert m.heading_deg(xyz) == pytest.approx(expected, abs=0.01)


def test_heading_axis_remap_and_invert():
    # Swap so Y is bow: field along +Y should now read north.
    assert m.heading_deg((0.0, 100.0, 0.0), forward_axis="y", right_axis="-x") \
        == pytest.approx(0.0, abs=0.01)
    # invert flips the rotation sense.
    base = m.heading_deg((70.0, -70.0, 0.0))
    inv = m.heading_deg((70.0, -70.0, 0.0), invert=True)
    assert (base + inv) % 360 == pytest.approx(0.0, abs=0.01)


# --- calibration ----------------------------------------------------------- #

def test_calibration_fits_hard_iron_offset():
    """A boat spinning about vertical traces a circle in X/Y (Z ~flat) with a
    hard-iron bias; the collector recovers the centre and re-centres the data."""
    col = m.CalibrationCollector()
    ox, oy = 30.0, -20.0
    for k in range(72):                       # a full slow circle
        t = math.radians(k * 5)
        col.add((ox + 100 * math.cos(t), oy + 100 * math.sin(t), 5.0))
    cal = col.result()
    assert cal is not None
    assert cal.offset[0] == pytest.approx(ox, abs=1.0)
    assert cal.offset[1] == pytest.approx(oy, abs=1.0)
    # Applying the fit centres the ring on the origin.
    cx, cy, _ = cal.apply((ox + 100, oy, 5.0))
    assert cx == pytest.approx(100, abs=2) and cy == pytest.approx(0, abs=2)


def test_calibration_rejects_insufficient_rotation():
    col = m.CalibrationCollector()
    for _ in range(50):                       # lots of samples, but no motion
        col.add((10.0, 20.0, 5.0))
    assert col.result() is None


def test_dump_i2c_reports_matches_window_and_no_response():
    window = b"\x64\x00\xc8\x00\xce\xff" + b"\x00" * 10   # 16 bytes from 0x00
    bus = FakeBus({(0x0D, 0x0D): b"\xff", (0x0D, 0x00): window})   # only QMC present
    rep = m.dump_i2c(bus, samples=2, delay_s=0, bus_num=1)
    assert "bus /dev/i2c-1" in rep
    assert "QMC5883L" in rep and "MATCH" in rep      # id matched
    assert "0x0d: responds" in rep
    assert window.hex() in rep                       # raw window shown for decoding
    assert "0x1e: NO RESPONSE" in rep and "0x0e: NO RESPONSE" in rep


def test_calibration_roundtrips_through_dict():
    cal = m.Calibration(offset=(1.0, 2.0, 3.0), scale=(1.1, 0.9, 1.0))
    assert m.Calibration.from_dict(cal.to_dict()).offset == (1.0, 2.0, 3.0)
    assert m.Calibration.from_dict(None).offset == (0.0, 0.0, 0.0)   # identity
