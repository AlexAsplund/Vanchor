"""The UBX u-blox GPS driver: bytes off the transport -> a rich GpsFix on the bus."""
import asyncio
import struct

from vanchor.core import events
from vanchor.core.events import EventBus
from vanchor.hardware.drivers.ublox import UbloxGps
from vanchor.hardware.serial_link import FakeSerialTransport
from vanchor.nav import ubx


def _nav_pvt(lat, lon, vel_n, vel_e, *, valid=True):
    """A minimal, self-consistent 92-byte UBX-NAV-PVT payload."""
    p = bytearray(92)
    struct.pack_into("<B", p, 20, 3)                       # fixType = 3D
    struct.pack_into("<B", p, 21, 1 if valid else 0)       # flags: gnssFixOK
    struct.pack_into("<B", p, 23, 11)                      # numSV
    struct.pack_into("<i", p, 24, round(lon * 1e7))
    struct.pack_into("<i", p, 28, round(lat * 1e7))
    struct.pack_into("<I", p, 40, 480)                     # hAcc (mm)
    struct.pack_into("<i", p, 48, round(vel_n * 1000))     # velN (mm/s)
    struct.pack_into("<i", p, 52, round(vel_e * 1000))     # velE (mm/s)
    struct.pack_into("<i", p, 60, round((vel_n**2 + vel_e**2) ** 0.5 * 1000))  # gSpeed
    struct.pack_into("<I", p, 68, 120)                     # sAcc (mm/s)
    return bytes(p)


async def _drive(frames: bytes):
    bus = EventBus()
    got: list = []

    async def collect(fix):
        got.append(fix)

    bus.subscribe(events.GPS_FIX_IN, collect)
    t = FakeSerialTransport()
    drv = UbloxGps(t, bus, configure=True)
    t.feed_bytes(frames)
    await drv.start()
    for _ in range(20):          # let the read loop process the fed bytes
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)
    await drv.stop()
    return got, t


async def test_nav_pvt_becomes_a_rich_gps_fix():
    frame = ubx.build_frame(*ubx.NAV_PVT, _nav_pvt(59.0, 18.0, -0.20, 2.05))
    got, t = await _drive(frame)
    assert len(got) == 1
    fix = got[0]
    assert abs(fix.point.lat - 59.0) < 1e-6 and abs(fix.point.lon - 18.0) < 1e-6
    # the velocity vector + accuracy NMEA can't carry are preserved
    assert abs(fix.vel_n_mps - (-0.20)) < 1e-3
    assert abs(fix.vel_e_mps - 2.05) < 1e-3
    assert fix.h_acc_m == 0.48 and abs(fix.s_acc_mps - 0.12) < 1e-6
    assert fix.sog_knots > 3.9  # ~2.06 m/s -> ~4 kn


async def test_driver_sends_the_marine_config_on_open():
    got, t = await _drive(ubx.build_frame(*ubx.NAV_PVT, _nav_pvt(59.0, 18.0, 0.0, 1.0)))
    # a UBX CFG frame was written to the receiver (starts with the sync bytes)
    assert bytes(t.written_bytes[:2]) == b"\xb5\x62"


async def test_invalid_fix_is_dropped():
    frame = ubx.build_frame(*ubx.NAV_PVT, _nav_pvt(59.0, 18.0, 0.0, 0.0, valid=False))
    got, _ = await _drive(frame)
    assert got == []


async def test_split_and_garbage_bytes_are_tolerated():
    frame = ubx.build_frame(*ubx.NAV_PVT, _nav_pvt(59.0, 18.0, 1.0, 1.0))
    # garbage prefix + the frame split across the buffer still yields exactly one fix
    got, _ = await _drive(b"\x00\xff\xb5garbage" + frame)
    assert len(got) == 1
