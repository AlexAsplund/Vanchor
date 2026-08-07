"""I2C magnetometer support: multi-chip autodetection, raw reads, heading, and
hard/soft-iron calibration.

This module is **pure** with respect to the hardware layer: it talks to an
injected ``bus`` object (any object exposing ``write_byte_data(addr, reg, val)``,
``read_byte_data(addr, reg) -> int`` and ``read_block_data(addr, reg, n) ->
bytes``), so every chip's register map, the autodetection, the heading math and
the calibration are unit-testable with a fake bus and no ``smbus2`` installed.
The real smbus2 wrapper lives in ``hardware/drivers/magnetometer.py``.

Combo GPS modules (Beitian BN-880 / BE-880, and many drone/rover boards) carry a
3-axis magnetometer on I2C next to the GNSS. There is no single part: the same
board may ship an **HMC5883L**, a **QMC5883L** clone, or an **IST8310**, at
different addresses and with different register maps and byte orders. So we key
support off the chip's identity register and autodetect which one is present,
rather than making the user know. Adding another chip is one entry in
``CHIP_TABLE``.

BENCH-VERIFY: the register maps below are transcribed from the datasheets; the
logic (detection, decode, heading, calibration) is unit-tested with a fake bus,
but the real chip timing/config has NOT yet been verified on hardware.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol


class I2CBus(Protocol):
    """The tiny I2C surface the chips need (smbus2 provides all three)."""

    def write_byte_data(self, addr: int, reg: int, value: int) -> None: ...
    def read_byte_data(self, addr: int, reg: int) -> int: ...
    def read_block_data(self, addr: int, reg: int, length: int) -> bytes: ...


def _s16(lo: int, hi: int) -> int:
    """Combine low/high bytes into a signed 16-bit int (hi is the MSB)."""
    v = (hi << 8) | lo
    return v - 0x10000 if v & 0x8000 else v


# --------------------------------------------------------------------------- #
# Chip definitions
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ChipSpec:
    """One magnetometer's register map + identity.

    ``id_reg``/``id_expected`` identify the part (``id_len`` bytes read from
    ``id_reg`` must equal ``id_expected``). ``init`` is the list of
    ``(register, value)`` writes that put it in a usable measuring mode.
    ``data_reg``/``data_len`` locate the 6 output bytes; ``little_endian`` and
    ``axis_pairs`` decode them into ``(x, y, z)`` -- ``axis_pairs`` is the index
    of the 16-bit pair (0,1,2 within the block) that holds X, Y and Z
    respectively (chips differ: HMC5883L streams X,Z,Y; QMC5883L streams X,Y,Z).
    ``single_shot`` chips have no continuous mode: re-run ``init`` and wait
    ``settle_s`` before each read."""

    name: str
    label: str
    addresses: tuple[int, ...]
    id_reg: int
    id_expected: bytes
    init: tuple[tuple[int, int], ...]
    data_reg: int
    little_endian: bool
    axis_pairs: tuple[int, int, int] = (0, 1, 2)   # block-pair index for (X, Y, Z)
    id_len: int = 1
    single_shot: bool = False
    settle_s: float = 0.008
    ut_per_lsb: float = 0.0     # microtesla per LSB (reporting only; 0 = unknown)


# HMC5883L (Honeywell): addr 0x1E. Identity regs 0x0A-0x0C read ASCII "H43".
# Config A 0x00=0x70 (8-avg, 15 Hz), Config B 0x01=0x20 (gain ±1.3 Ga), Mode
# 0x02=0x00 (continuous). Data 0x03..0x08 big-endian in X, Z, Y order.
HMC5883L = ChipSpec(
    name="HMC5883L", label="Honeywell HMC5883L",
    addresses=(0x1E,), id_reg=0x0A, id_expected=b"H43", id_len=3,
    init=((0x00, 0x70), (0x01, 0x20), (0x02, 0x00)),
    data_reg=0x03, little_endian=False, axis_pairs=(0, 2, 1),
    ut_per_lsb=100.0 / 1090.0,
)

# QMC5883L (QST): addr 0x0D. Chip-ID reg 0x0D = 0xFF. SET/RESET period reg
# 0x0B=0x01 (required), Control 1 0x09=0x1D (continuous, 200 Hz, ±8 G, OSR 512).
# Data 0x00..0x05 little-endian in X, Y, Z order.
QMC5883L = ChipSpec(
    name="QMC5883L", label="QST QMC5883L",
    addresses=(0x0D,), id_reg=0x0D, id_expected=b"\xff",
    init=((0x0B, 0x01), (0x09, 0x1D)),
    data_reg=0x00, little_endian=True, axis_pairs=(0, 1, 2),
    ut_per_lsb=100.0 / 3000.0,   # ±8 G range -> 3000 LSB/G
)

# IST8310 (iSentek): addr 0x0E. WHO_AM_I reg 0x00 = 0x10. No continuous mode:
# write CNTL1 0x0A=0x01 to trigger a single measurement, wait, then read
# 0x03..0x08 little-endian X, Y, Z.
IST8310 = ChipSpec(
    name="IST8310", label="iSentek IST8310",
    addresses=(0x0E,), id_reg=0x00, id_expected=b"\x10",
    init=((0x0A, 0x01),),
    data_reg=0x03, little_endian=True, axis_pairs=(0, 1, 2),
    single_shot=True, settle_s=0.008, ut_per_lsb=1.0 / 3.0,
)

# Order matters for autodetect: distinct addresses, so no conflict, but keep the
# common combo-GPS chips first. Add a new chip by appending its ChipSpec here.
CHIP_TABLE: tuple[ChipSpec, ...] = (QMC5883L, HMC5883L, IST8310)


def detect(bus: I2CBus, *, addresses: tuple[int, ...] | None = None
           ) -> tuple[ChipSpec, int] | None:
    """Probe known magnetometer addresses and return ``(spec, addr)`` for the
    first chip whose identity register matches, else ``None``.

    Register READS only (plus, implicitly, none of the init writes) at explicitly
    named addresses -- never a bus-wide sweep. ``addresses`` optionally restricts
    the probe (e.g. to a single configured address)."""
    for spec in CHIP_TABLE:
        for addr in spec.addresses:
            if addresses is not None and addr not in addresses:
                continue
            try:
                got = bytes(bus.read_block_data(addr, spec.id_reg, spec.id_len))
            except Exception:
                continue          # NAK / no device at this address
            if got == spec.id_expected:
                return spec, addr
    return None


class Magnetometer:
    """A configured magnetometer on a bus: ``configure()`` once, then
    ``read_raw()`` returns the signed ``(x, y, z)`` counts."""

    def __init__(self, bus: I2CBus, spec: ChipSpec, addr: int) -> None:
        self.bus = bus
        self.spec = spec
        self.addr = addr

    def configure(self) -> None:
        for reg, val in self.spec.init:
            self.bus.write_byte_data(self.addr, reg, val)

    def read_raw(self) -> tuple[int, int, int]:
        spec = self.spec
        if spec.single_shot:
            # No continuous mode: re-trigger and let the measurement settle.
            for reg, val in spec.init:
                self.bus.write_byte_data(self.addr, reg, val)
            time.sleep(spec.settle_s)
        raw = bytes(self.bus.read_block_data(self.addr, spec.data_reg, 6))
        pairs = []
        for i in range(3):
            lo, hi = raw[2 * i], raw[2 * i + 1]
            pairs.append(_s16(lo, hi) if spec.little_endian else _s16(hi, lo))
        px, py, pz = spec.axis_pairs
        return pairs[px], pairs[py], pairs[pz]


# --------------------------------------------------------------------------- #
# Raw register dump (remote troubleshooting)
# --------------------------------------------------------------------------- #
def dump_i2c(bus: I2CBus, *, addresses: tuple[int, ...] | None = None,
             samples: int = 3, window: int = 16, delay_s: float = 0.1,
             bus_num: int | None = None) -> str:
    """A human-readable hex dump of the known magnetometer addresses -- so a
    remote user can copy it into an issue when a chip won't autodetect and we
    diagnose it (wrong id? swapped bytes? dead sensor?) without the hardware.

    Reads ONLY the named magnetometer addresses (no bus-wide sweep). For each: an
    explicit identity-register check per candidate chip, plus ``samples``
    snapshots of a ``window``-byte register block from 0x00 (which spans the data
    AND id registers of every supported chip), so changing bytes = live data. All
    reads are guarded, so an absent address just reports 'NO RESPONSE'."""
    addrs = addresses if addresses is not None else tuple(
        sorted({a for s in CHIP_TABLE for a in s.addresses}))
    hdr = "I2C magnetometer raw dump"
    if bus_num is not None:
        hdr += f" (bus /dev/i2c-{bus_num})"
    lines = [hdr + ":", f"probing {', '.join(f'0x{a:02x}' for a in addrs)}"]
    for addr in addrs:
        id_notes = []
        for s in CHIP_TABLE:
            if addr not in s.addresses:
                continue
            try:
                got = bytes(bus.read_block_data(addr, s.id_reg, s.id_len))
                verdict = "MATCH" if got == s.id_expected else "differs"
                id_notes.append(f"{s.name}: id@0x{s.id_reg:02x}=0x{got.hex()} "
                                f"(expect 0x{s.id_expected.hex()} -> {verdict})")
            except Exception:
                id_notes.append(f"{s.name}: id@0x{s.id_reg:02x} no response")
        snaps: list[str] = []
        responded = False
        n = max(1, samples)
        for j in range(n):
            try:
                w = bytes(bus.read_block_data(addr, 0x00, window))
                snaps.append(w.hex())
                responded = True
            except Exception:
                snaps.append("(no response)")
                break                       # dead address -> don't retry
            if delay_s > 0 and j < n - 1:
                time.sleep(delay_s)         # spacing between live samples only
        lines.append(f"0x{addr:02x}: {'responds' if responded else 'NO RESPONSE'}")
        lines += [f"    {n}" for n in id_notes]
        if responded:
            lines.append(f"    regs 0x00..0x{window - 1:02x} x{len(snaps)}:")
            lines += [f"      {i}: {s}" for i, s in enumerate(snaps)]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Calibration (hard- + soft-iron) and heading
# --------------------------------------------------------------------------- #
@dataclass
class Calibration:
    """Per-axis hard-iron offset + soft-iron scale. ``corrected = (raw - offset)
    * scale``. Identity by default (raw passed through)."""

    offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)

    def apply(self, xyz: tuple[float, float, float]) -> tuple[float, float, float]:
        return tuple((xyz[i] - self.offset[i]) * self.scale[i] for i in range(3))

    def to_dict(self) -> dict:
        return {"offset": list(self.offset), "scale": list(self.scale)}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Calibration":
        if not d:
            return cls()
        o = d.get("offset") or [0.0, 0.0, 0.0]
        s = d.get("scale") or [1.0, 1.0, 1.0]
        return cls(offset=(float(o[0]), float(o[1]), float(o[2])),
                   scale=(float(s[0]), float(s[1]), float(s[2])))


class CalibrationCollector:
    """Accumulate samples during a full rotation and fit a hard/soft-iron
    correction (min/max per axis). ``spin the boat slowly through 360 deg``:

        offset[i] = (max[i] + min[i]) / 2          # hard-iron centre
        scale[i]  = avg_radius / ((max[i]-min[i])/2)  # soft-iron -> unit sphere
    """

    def __init__(self) -> None:
        self.n = 0
        self._min = [math.inf, math.inf, math.inf]
        self._max = [-math.inf, -math.inf, -math.inf]

    def add(self, xyz: tuple[float, float, float]) -> None:
        for i in range(3):
            self._min[i] = min(self._min[i], xyz[i])
            self._max[i] = max(self._max[i], xyz[i])
        self.n += 1

    def spread(self) -> float:
        """Largest axis range so far -- a readiness signal (needs real rotation)."""
        if self.n == 0:
            return 0.0
        return max(self._max[i] - self._min[i] for i in range(3))

    def result(self) -> Calibration | None:
        """The fitted Calibration, or None if not enough motion to trust it.

        A boat turns about the VERTICAL axis, so only the two horizontal axes
        trace a circle -- the third (vertical) stays ~flat. We therefore require
        at least TWO axes to have real spread (not all three), size the unit
        sphere from the moving axes, and leave a flat axis at scale 1.0. The
        offset (hard-iron centre) is still taken on all three."""
        if self.n < 20:
            return None
        deltas = [(self._max[i] - self._min[i]) / 2.0 for i in range(3)]
        big = max(deltas)
        if big <= 1e-6:
            return None
        thresh = big * 0.15
        moved = [d for d in deltas if d >= thresh]
        if len(moved) < 2:                 # need a real rotation, not jitter
            return None
        target = sum(moved) / len(moved)   # mean radius of the moving axes
        offset = tuple((self._max[i] + self._min[i]) / 2.0 for i in range(3))
        scale = tuple(target / deltas[i] if deltas[i] >= thresh else 1.0
                      for i in range(3))
        return Calibration(offset=offset, scale=scale)


# Axis mapping: which corrected sensor axis points to the boat's bow / starboard,
# and its sign. Default: X = bow (forward), Y = starboard (right). A magnetometer
# mounted at any yaw is handled by the GPS-learned offset downstream; these knobs
# exist for a mirrored/rotated mount the offset alone can't fix.
_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}


def heading_deg(corrected: tuple[float, float, float], *,
                forward_axis: str = "x", right_axis: str = "y",
                invert: bool = False) -> float:
    """Magnetic heading (0..360, CW from magnetic north) from a corrected vector,
    assuming the sensor is roughly level (no tilt compensation without an accel).

    ``forward_axis``/``right_axis`` (e.g. ``"x"``/``"y"``, optionally signed like
    ``"-y"``) select and orient the horizontal axes for the mount; ``invert``
    flips the rotation sense for a mirrored board."""
    def axis(name: str) -> float:
        sign = -1.0 if name.startswith("-") else 1.0
        return sign * corrected[_AXIS_INDEX[name.lstrip("+-")]]

    fwd = axis(forward_axis)
    right = axis(right_axis)
    ang = math.degrees(math.atan2(-right if not invert else right, fwd))
    return ang % 360.0


# () -> (course_over_ground_deg, speed_over_ground_mps) | None
MotionProvider = Callable[[], "tuple | None"]
