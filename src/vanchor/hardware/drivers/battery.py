"""Battery-monitor drivers — the reference 4th, non-core device kind (roadmap #42).

Battery state (how much charge is left, and how far the boat can still get on it)
is a first-class safety concern on a trolling-motor autopilot, so the battery is
the first *non-core* device kind wired through the versioned driver/capability
API (roadmap #43). Two sources:

* ``sim`` — :class:`SimBatteryMonitor` presents the simulator's integrated
  :class:`vanchor.sim.battery.Battery` as a :class:`BatteryMonitor`. It is the
  baseline (tightly coupled to the sim, like the sim GPS) and is built inline in
  ``app.py``; its telemetry is byte-for-byte the sim battery's, so nothing about
  the existing behaviour changes.
* ``ina226`` — :class:`INA226BatteryMonitor` is a *proper* driver for a real
  INA226 / Victron-style shunt gauge. It is registered against the capability API
  and built with a narrow :class:`~vanchor.hardware.registry.DriverContext`
  (never the runtime/motor/governor). It reads bus voltage + shunt current from
  an I²C shunt and estimates state-of-charge by coulomb counting (seeded from the
  resting voltage). Because there is **no bench hardware**, the driver is written
  against a tiny ``ShuntReader`` seam so it is fully testable with a
  :class:`FakeShunt` double; the real smbus2 reader is imported lazily and is
  marked as untested-on-hardware.

All monitors report the same telemetry shape as the sim battery
(``soc_pct``/``voltage_v``/``current_a``/``draw_w``/``range_m``/
``time_to_empty_s``) so the UI, the range/time-to-empty estimate and the
Return-to-Launch logic don't care which source is wired in.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional, Protocol

from ...sim.battery import BatteryConfig as SimBatteryConfig
from ..interfaces import BatteryMonitor
from ..registry import DRIVER_API_VERSION, register_context_driver

logger = logging.getLogger("vanchor.hardware.battery")

_SOC_MIN = 0.0
_SOC_MAX = 100.0


# --------------------------------------------------------------------------- #
# sim source: present the simulator's integrated battery as a BatteryMonitor
# --------------------------------------------------------------------------- #
class SimBatteryMonitor(BatteryMonitor):
    """Adapts the simulator's :class:`~vanchor.sim.battery.Battery` to the
    :class:`BatteryMonitor` seam. A pure read-view: the simulator still drains the
    pack from applied thrust each physics step; this just surfaces it, so the
    telemetry is identical to the pre-existing ``simulator.battery.to_dict()``."""

    def __init__(self, battery: Any) -> None:
        self._battery = battery

    def snapshot(self) -> dict:
        return self._battery.to_dict()

    def health(self) -> dict:
        return {"ok": True, "detail": "simulated"}


# --------------------------------------------------------------------------- #
# ina226 source: a real shunt gauge, testable via a fake ShuntReader double
# --------------------------------------------------------------------------- #
class ShuntReader(Protocol):
    """The tiny seam the INA226 driver reads through, so it is fully testable
    without hardware. A real implementation talks I²C; :class:`FakeShunt` returns
    programmed values."""

    def read_bus_voltage_v(self) -> float: ...

    def read_current_a(self) -> float: ...

    def close(self) -> None: ...


class FakeShunt:
    """In-memory :class:`ShuntReader` double for tests / a bench-less bring-up.

    Set :attr:`voltage_v` / :attr:`current_a` to drive the monitor; set
    :attr:`fail` to make the next read raise (to exercise health reporting)."""

    def __init__(self, voltage_v: float = 12.6, current_a: float = 0.0) -> None:
        self.voltage_v = voltage_v
        self.current_a = current_a
        self.fail = False
        self.closed = False

    def read_bus_voltage_v(self) -> float:
        if self.fail:
            raise OSError("fake shunt read failure")
        return self.voltage_v

    def read_current_a(self) -> float:
        if self.fail:
            raise OSError("fake shunt read failure")
        return self.current_a

    def close(self) -> None:
        self.closed = True


# INA226 register map (subset) + fixed LSB weights from the datasheet.
_REG_SHUNT_VOLTAGE = 0x01  # LSB = 2.5 µV
_REG_BUS_VOLTAGE = 0x02    # LSB = 1.25 mV
_SHUNT_LSB_V = 2.5e-6
_BUS_LSB_V = 1.25e-3


class INA226Shunt:
    """Real INA226 shunt reader over smbus2 (untested on hardware — no bench).

    Reads the bus-voltage and shunt-voltage registers directly and derives
    current as ``shunt_voltage / shunt_ohms`` (so it needs no on-chip calibration
    register). ``smbus2`` is imported lazily so the core install / the simulator
    never need it. Registers are 16-bit big-endian; the shunt-voltage register is
    signed two's-complement."""

    def __init__(self, bus: int, address: int, shunt_ohms: float) -> None:  # pragma: no cover - needs hardware
        try:
            from smbus2 import SMBus
        except ImportError as exc:
            raise RuntimeError(
                "battery_source='ina226' needs smbus2: pip install 'vanchor[serial]' "
                "(or pip install smbus2) on the Pi."
            ) from exc
        self._smbus = SMBus(bus)
        self._addr = address
        self._shunt_ohms = shunt_ohms if shunt_ohms > 0 else 0.001

    def _read_u16(self, reg: int) -> int:  # pragma: no cover - needs hardware
        raw = self._smbus.read_word_data(self._addr, reg)
        return ((raw & 0xFF) << 8) | (raw >> 8)  # swap to big-endian

    def read_bus_voltage_v(self) -> float:  # pragma: no cover - needs hardware
        return self._read_u16(_REG_BUS_VOLTAGE) * _BUS_LSB_V

    def read_current_a(self) -> float:  # pragma: no cover - needs hardware
        raw = self._read_u16(_REG_SHUNT_VOLTAGE)
        if raw & 0x8000:  # signed two's-complement
            raw -= 0x10000
        return raw * _SHUNT_LSB_V / self._shunt_ohms

    def close(self) -> None:  # pragma: no cover - needs hardware
        try:
            self._smbus.close()
        except Exception:  # noqa: BLE001
            pass


class INA226BatteryMonitor(BatteryMonitor):
    """A battery monitor driven by an INA226 / Victron-style shunt gauge.

    Reads measured voltage + current from a :class:`ShuntReader`; estimates
    state-of-charge by **coulomb counting** — integrate current out of the pack
    over time — seeded from the resting terminal voltage on the first read. The
    range/time-to-empty estimate mirrors the sim battery: a recent-average draw
    (and speed-over-ground, from the capability object's motion) against the
    usable amp-hours above the reserve floor, so the UI + RTL logic are identical
    regardless of source.

    Pure/synchronous at heart (``read_once`` advances it); :meth:`start` runs a
    background poll loop at ``poll_hz``. A shunt read that raises is caught, the
    monitor is flagged unhealthy, and the loop keeps running (a flaky gauge must
    never crash the autopilot)."""

    def __init__(
        self,
        shunt: ShuntReader,
        config: SimBatteryConfig | None = None,
        *,
        poll_hz: float = 1.0,
        now: Callable[[], float] = time.monotonic,
        motion: Optional[Callable[[], Optional[tuple]]] = None,
        soc_pct: float | None = None,
    ) -> None:
        self._shunt = shunt
        self.config = config or SimBatteryConfig()
        self.poll_hz = max(0.1, float(poll_hz))
        self._now = now
        self._motion = motion
        self.voltage_v = self.config.nominal_v
        self.current_a = 0.0
        self._avg_current_a = 0.0
        self._avg_sog_mps = 0.0
        self.soc_pct = soc_pct if soc_pct is not None else None  # seeded on first read
        self._last_t: float | None = None
        self._ok = True
        self._detail = ""
        self._task: asyncio.Task | None = None

    # -- SoC seeding from resting voltage (inverse of the sim voltage curve) -- #
    def _soc_from_voltage(self, v: float) -> float:
        full = self.config.nominal_v + 0.7
        empty = self.config.nominal_v - 0.2
        if full <= empty:
            return 100.0
        return max(_SOC_MIN, min(_SOC_MAX, 100.0 * (v - empty) / (full - empty)))

    def read_once(self, dt: float | None = None, sog_mps: float | None = None) -> None:
        """Read one shunt sample and advance the SoC + smoothed estimates.

        ``dt`` defaults to the elapsed time since the last read (from the injected
        clock); ``sog_mps`` defaults to the capability object's motion (for the
        range estimate). A read failure flags the monitor unhealthy and returns
        without advancing the integrator."""
        try:
            v = float(self._shunt.read_bus_voltage_v())
            i = float(self._shunt.read_current_a())
        except Exception as exc:  # noqa: BLE001 - a flaky gauge must not crash us
            self._ok = False
            self._detail = f"read failed: {exc}"
            logger.debug("ina226 read failed: %s", exc)
            return
        self._ok = True
        self._detail = ""
        self.voltage_v = v
        self.current_a = i
        if self.soc_pct is None:
            self.soc_pct = self._soc_from_voltage(v)

        t = self._now()
        if dt is None:
            dt = 0.0 if self._last_t is None else max(0.0, t - self._last_t)
        self._last_t = t
        if dt <= 0.0:
            return

        # Coulomb count: Ah drawn = A * (dt/3600); positive current = discharge.
        cfg = self.config
        if cfg.capacity_ah > 0.0:
            ah_drawn = i * (dt / 3600.0)
            self.soc_pct = max(_SOC_MIN, min(_SOC_MAX, self.soc_pct - 100.0 * ah_drawn / cfg.capacity_ah))

        if sog_mps is None:
            mv = self._motion() if self._motion is not None else None
            sog_mps = mv[1] if mv is not None else 0.0

        # Smooth recent draw + speed for a stable range/time estimate.
        alpha = dt / (cfg.draw_tau_s + dt)
        self._avg_current_a += (max(0.0, i) - self._avg_current_a) * alpha
        self._avg_sog_mps += (max(0.0, sog_mps) - self._avg_sog_mps) * alpha

    # -- derived telemetry (mirrors sim.battery) ------------------------------ #
    @property
    def draw_w(self) -> float:
        return self.current_a * self.voltage_v

    @property
    def _usable_ah(self) -> float:
        cfg = self.config
        usable_pct = max(0.0, (self.soc_pct or 0.0) - cfg.reserve_pct)
        return cfg.capacity_ah * usable_pct / 100.0

    @property
    def time_to_empty_s(self) -> float:
        if self._avg_current_a <= 1e-6:
            return float("inf")
        return self._usable_ah / self._avg_current_a * 3600.0

    @property
    def range_m(self) -> float:
        tte = self.time_to_empty_s
        if self._avg_sog_mps <= 1e-3 or tte == float("inf"):
            return 0.0
        return self._avg_sog_mps * tte

    def snapshot(self) -> dict:
        tte = self.time_to_empty_s
        return {
            # SoC is UNKNOWN (None) until a real shunt read seeds it -- never
            # report 0.0, which the #49 ladder would read as a critically-empty
            # pack and force a startup derate + RTL. None makes the ladder's
            # soc-is-None guard fire and apply no cap while uninitialized.
            "soc_pct": None if self.soc_pct is None else round(self.soc_pct, 1),
            "voltage_v": round(self.voltage_v, 2),
            "current_a": round(self.current_a, 2),
            "draw_w": round(self.draw_w, 1),
            "range_m": round(self.range_m, 1),
            "time_to_empty_s": None if tte == float("inf") else round(tte, 1),
        }

    def health(self) -> dict:
        return {"ok": self._ok, "detail": self._detail}

    async def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        close = getattr(self._shunt, "close", None)
        if callable(close):
            close()

    async def _loop(self) -> None:
        while True:
            period = 1.0 / self.poll_hz
            try:
                self.read_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive; read_once already guards
                logger.exception("ina226 poll loop error")
            await asyncio.sleep(period)


def _config_from(ctx_config: Any) -> SimBatteryConfig:
    """Map whatever config slice the context carries onto the fields the monitor
    needs. Accepts the app :class:`vanchor.core.config.BatteryConfig` (or any
    object exposing the same attributes); missing attrs fall back to defaults."""
    def g(name: str, default):
        return getattr(ctx_config, name, default) if ctx_config is not None else default

    return SimBatteryConfig(
        capacity_ah=float(g("capacity_ah", 100.0)),
        nominal_v=float(g("nominal_v", 12.0)),
        reserve_pct=float(g("reserve_pct", 15.0)),
        draw_tau_s=float(g("draw_tau_s", 20.0)),
    )


def build_ina226(ctx: Any) -> INA226BatteryMonitor:
    """Capability-API build hook (#43): construct the INA226 monitor from the
    narrow :class:`~vanchor.hardware.registry.DriverContext`. Opens the real shunt
    (lazily importing smbus2); tests register a driver that injects a
    :class:`FakeShunt` instead."""
    cfg = ctx.config
    bus = int(getattr(cfg, "i2c_bus", 1))
    addr = int(getattr(cfg, "i2c_addr", 0x40))
    shunt_ohms = float(getattr(cfg, "shunt_ohms", 0.001))
    shunt = INA226Shunt(bus, addr, shunt_ohms)
    return INA226BatteryMonitor(
        shunt, _config_from(cfg), now=ctx.now, motion=ctx.motion,
    )


register_context_driver(
    "battery", "ina226", build_ina226,
    api_version=DRIVER_API_VERSION,
    label="INA226 / shunt battery monitor",
)
