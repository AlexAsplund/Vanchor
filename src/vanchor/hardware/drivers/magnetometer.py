"""Pluggable compass driver for a bare I2C 3-axis magnetometer.

Targets the magnetometer found on combo GNSS boards (Beitian BN-880 / BE-880 and
many drone/rover modules) and any standalone HMC5883L / QMC5883L / IST8310. The
chip is **autodetected** by its identity register, so the user only picks
"magnetometer" and points it at an I2C bus -- no need to know which part is
fitted (see :mod:`vanchor.nav.magnetometer`).

Like the HWT901B driver it is a :class:`~vanchor.hardware.interfaces.Sensor` that
emits ``HDT`` NMEA onto the bus, learns declination + mount offset from the GPS
course (reusing :class:`~vanchor.hardware.drivers.hwt901b.HeadingOffsetEstimator`),
and offers a device menu -- including an interactive **spin-to-calibrate** action
that fits and persists the hard/soft-iron correction a magnetometer needs.

``smbus2`` is optional and imported lazily, so the core install and the simulator
never need it; the driver is fully testable with a fake I2C bus (no smbus2, no
hardware).

BENCH-VERIFY: the chip register maps + I2C timing have not yet been checked on
real hardware; the logic is unit-tested with a fake bus.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from ...core import events
from ...core.events import EventBus
from ...core.geo import normalize_deg
from ...nav import magnetometer as mag
from ...nav import nmea
from ..interfaces import Sensor
from ..registry import register_driver
from .hwt901b import HeadingOffsetEstimator, MotionProvider

logger = logging.getLogger("vanchor.hardware.magnetometer")

_DEFAULT_BUS = 1
_REOPEN_BACKOFF_S = 3.0
_READ_ERR_LIMIT = 5


# --------------------------------------------------------------------------- #
# Real I2C bus (lazy smbus2). A fake with the same 3 methods is used in tests.
# --------------------------------------------------------------------------- #
class SmbusBus:
    """Thin :class:`~vanchor.nav.magnetometer.I2CBus` over ``smbus2.SMBus``."""

    def __init__(self, bus_num: int) -> None:
        try:
            from smbus2 import SMBus  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise RuntimeError(
                "compass_source='magnetometer' needs the i2c extra: "
                "pip install 'vanchor[i2c]'"
            ) from exc
        self._smbus = SMBus(bus_num)

    def write_byte_data(self, addr: int, reg: int, value: int) -> None:
        self._smbus.write_byte_data(addr, reg, value)

    def read_byte_data(self, addr: int, reg: int) -> int:
        return self._smbus.read_byte_data(addr, reg)

    def read_block_data(self, addr: int, reg: int, length: int) -> bytes:
        return bytes(self._smbus.read_i2c_block_data(addr, reg, length))

    def close(self) -> None:
        try:
            self._smbus.close()
        except Exception:  # pragma: no cover - best effort
            pass


def parse_i2c_target(port: str | None, *, default_bus: int = _DEFAULT_BUS
                     ) -> tuple[int, int | None]:
    """Parse a compass ``port`` into ``(bus, addr|None)`` for the I2C compass.

    Accepts ``i2c:<bus>``, ``i2c:<bus>:<addr>`` (addr hex ``0x0d`` or decimal), a
    bare bus number, or anything else (-> default bus, autodetect address)."""
    s = (port or "").strip()
    if s.lower().startswith("i2c:"):
        parts = s[4:].split(":")
        bus = int(parts[0]) if parts and parts[0] else default_bus
        addr = int(parts[1], 0) if len(parts) > 1 and parts[1] else None
        return bus, addr
    if s.isdigit():
        return int(s), None
    return default_bus, None


# --------------------------------------------------------------------------- #
# Device menu schema
# --------------------------------------------------------------------------- #
def _menu_schema(declination_mode: str, manual_declination_deg: float, hz: float,
                 forward_axis: str, right_axis: str, invert: bool,
                 detected: str | None) -> dict:
    title = "Compass — magnetometer"
    if detected:
        title += f" ({detected})"
    return {
        "device": "compass",
        "title": title,
        "settings": [
            {"key": "declination_mode", "label": "Declination", "type": "select",
             "options": ["auto", "manual", "off"], "value": declination_mode,
             "help": "auto = learn declination + mount offset from GPS course."},
            {"key": "manual_declination_deg", "label": "Manual declination",
             "type": "number", "min": -30, "max": 30, "step": 0.1, "unit": "°",
             "value": round(manual_declination_deg, 1),
             "shown_when": {"declination_mode": "manual"}},
            {"key": "hz", "label": "Update rate", "type": "number",
             "min": 1, "max": 50, "step": 1, "unit": "Hz", "value": hz},
            {"key": "forward_axis", "label": "Bow axis", "type": "select",
             "options": ["x", "-x", "y", "-y"], "value": forward_axis,
             "help": "Which sensor axis points toward the bow (for the mount)."},
            {"key": "right_axis", "label": "Starboard axis", "type": "select",
             "options": ["y", "-y", "x", "-x"], "value": right_axis},
            {"key": "invert", "label": "Mirror correction", "type": "bool",
             "value": invert, "help": "Enable if the heading turns the wrong way."},
        ],
        "actions": [
            {"name": "status", "label": "Sensor status",
             "help": "Show the detected chip, live heading, learned offset."},
            {"name": "calibrate_start", "label": "Start calibration",
             "help": "Then slowly turn the boat through a full circle."},
            {"name": "calibrate_stop", "label": "Finish calibration",
             "help": "Fit + save the hard/soft-iron correction from the spin."},
            {"name": "redetect", "label": "Re-scan I2C",
             "help": "Probe the bus again for the magnetometer."},
        ],
    }


def default_menu() -> dict:
    return _menu_schema("auto", 0.0, 5.0, "x", "y", False, None)


# --------------------------------------------------------------------------- #
# The driver
# --------------------------------------------------------------------------- #
class MagnetometerCompass(Sensor):
    """An I2C magnetometer presented as a vanchor NMEA compass.

    ``open_bus`` is a zero-arg callable returning an object with
    ``write_byte_data`` / ``read_byte_data`` / ``read_block_data`` (+ optional
    ``close``) -- the real :class:`SmbusBus` or a fake in tests. Detection,
    configuration and reconnection happen inside the loop, so a not-yet-ready bus
    or wrong wiring never crashes startup and recovers on its own."""

    def __init__(
        self, open_bus: Callable[[], Any], *,
        addr: int | None = None,
        bus: EventBus | None = None,
        hz: float = 5.0,
        motion_provider: MotionProvider | None = None,
        declination_mode: str = "auto",
        manual_declination_deg: float = 0.0,
        calibration: mag.Calibration | None = None,
        forward_axis: str = "x", right_axis: str = "y", invert: bool = False,
        persist_cal: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._open_bus = open_bus
        self._addr = addr
        self.bus = bus
        self.hz = max(0.5, hz)
        self.motion_provider = motion_provider
        self.declination_mode = declination_mode
        self.manual_declination_deg = manual_declination_deg
        self.calibration = calibration or mag.Calibration()
        self.forward_axis = forward_axis
        self.right_axis = right_axis
        self.invert = invert
        self._persist_cal = persist_cal
        self.estimator = HeadingOffsetEstimator()

        self._task: asyncio.Task | None = None
        self._i2c: Any = None
        self._mag: mag.Magnetometer | None = None
        self.detected: str | None = None
        # Calibration state.
        self._collector: mag.CalibrationCollector | None = None
        # Latest values for the Debug view.
        self.last_heading_deg: float | None = None
        self._last_magnetic_deg: float | None = None
        self._last_declination_deg: float = 0.0
        self._last_raw: tuple[int, int, int] | None = None

    async def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._close_bus()

    def _close_bus(self) -> None:
        close = getattr(self._i2c, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover
                pass
        self._i2c = None
        self._mag = None

    def _open_and_detect(self) -> bool:
        """Open the bus and detect + configure the chip. Blocking (run in a
        thread). Returns True on success."""
        self._i2c = self._open_bus()
        addrs = (self._addr,) if self._addr is not None else None
        found = mag.detect(self._i2c, addresses=addrs)
        if found is None:
            self._close_bus()
            return False
        spec, addr = found
        self._mag = mag.Magnetometer(self._i2c, spec, addr)
        self._mag.configure()
        self.detected = spec.name
        logger.info("magnetometer: detected %s at 0x%02x on the I2C bus",
                    spec.name, addr)
        return True

    def _current_declination(self, magnetic_heading: float, dt: float) -> float:
        if self.declination_mode == "manual":
            return self.manual_declination_deg
        if self.declination_mode == "off":
            return 0.0
        cog = sog = None
        if self.motion_provider is not None:
            mv = self.motion_provider()
            if mv is not None:
                cog, sog = mv
        return self.estimator.update(magnetic_heading, cog, sog, dt)

    def _heading_from_raw(self, raw: tuple[int, int, int], dt: float) -> str:
        """Raw counts -> calibrated -> magnetic heading -> declination -> HDT."""
        if self._collector is not None:                       # feed calibration
            self._collector.add(tuple(float(v) for v in raw))
        corrected = self.calibration.apply(tuple(float(v) for v in raw))
        magnetic = mag.heading_deg(corrected, forward_axis=self.forward_axis,
                                   right_axis=self.right_axis, invert=self.invert)
        declination = self._current_declination(magnetic, dt)
        heading = normalize_deg(magnetic + declination)
        self.last_heading_deg = heading
        self._last_magnetic_deg = magnetic
        self._last_declination_deg = declination
        self._last_raw = raw
        return nmea.encode_hdt(heading)

    async def sample_once(self, dt: float) -> str | None:
        """Read one vector and return the HDT sentence, or None (not ready)."""
        if self._mag is None:
            return None
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, self._mag.read_raw)
        return self._heading_from_raw(raw, dt)

    async def _loop(self) -> None:
        loop = asyncio.get_event_loop()
        errors = 0
        while True:
            try:
                if self._mag is None:
                    ok = await loop.run_in_executor(None, self._open_and_detect)
                    if not ok:
                        await asyncio.sleep(_REOPEN_BACKOFF_S)
                        continue
                    errors = 0
                period = 1.0 / self.hz
                sentence = await self.sample_once(period)
                if sentence and self.bus is not None:
                    await self.bus.publish(events.NMEA_IN, sentence)
                errors = 0
                await asyncio.sleep(period)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive; reopen on repeats
                errors += 1
                logger.debug("magnetometer read error (%d): %s", errors, exc)
                if errors >= _READ_ERR_LIMIT:
                    logger.warning("magnetometer: too many read errors; reopening")
                    self._close_bus()
                    errors = 0
                await asyncio.sleep(min(_REOPEN_BACKOFF_S, 1.0))

    def debug(self) -> str:
        if self.detected is None:
            return f"{type(self).__name__}: scanning I2C for a magnetometer…"
        if self.last_heading_deg is None:
            return f"{type(self).__name__}: {self.detected} found, waiting for data…"
        try:
            r = self._last_raw or (0, 0, 0)
            cal = "on" if self.calibration.offset != (0.0, 0.0, 0.0) else "off"
            return (
                f"{type(self).__name__}\n"
                f"  chip      : {self.detected}\n"
                f"  heading   : {self.last_heading_deg:.1f} ° true "
                f"({self._last_magnetic_deg:.1f} ° magnetic)\n"
                f"  declin.   : {self._last_declination_deg:+.1f} ° "
                f"(mode {self.declination_mode})\n"
                f"  raw x/y/z : {r[0]} / {r[1]} / {r[2]}  (cal {cal})\n"
                f"  offset    : {self.estimator.offset_deg:+.1f} ° "
                f"(settled {self.estimator.settled})"
            )
        except Exception as exc:  # noqa: BLE001 - debug must never raise
            return f"{type(self).__name__}: debug error ({exc})"

    # -- device menu -------------------------------------------------------- #
    def device_menu(self) -> dict:
        return _menu_schema(self.declination_mode, self.manual_declination_deg,
                            self.hz, self.forward_axis, self.right_axis,
                            self.invert, self.detected)

    def apply_setting(self, key: str, value: Any) -> dict:
        if key == "declination_mode" and value in ("auto", "manual", "off"):
            self.declination_mode = value
        elif key == "manual_declination_deg":
            self.manual_declination_deg = float(value)
        elif key == "hz":
            self.hz = max(0.5, float(value))
        elif key == "forward_axis":
            self.forward_axis = str(value)
        elif key == "right_axis":
            self.right_axis = str(value)
        elif key == "invert":
            self.invert = bool(value)
        elif key == "calibration":
            self.calibration = mag.Calibration.from_dict(value)
        else:
            return {"ok": False, "message": f"unknown setting {key!r}"}
        return {"ok": True}

    def run_action(self, name: str, params: dict | None = None) -> dict:
        if name == "status":
            hdg = self.last_heading_deg
            return {"ok": True, "message": f"Magnetometer: {self.detected or 'not detected'}.",
                    "status": {
                        "chip": self.detected,
                        "heading_deg": round(hdg, 1) if hdg is not None else None,
                        "declination_mode": self.declination_mode,
                        "offset_deg": round(self.estimator.offset_deg, 1),
                        "offset_settled": self.estimator.settled,
                        "calibrated": self.calibration.offset != (0.0, 0.0, 0.0),
                    }}
        if name == "calibrate_start":
            if self._mag is None:
                return {"ok": False, "message": "No magnetometer detected yet."}
            self._collector = mag.CalibrationCollector()
            return {"ok": True, "message": "Calibrating. Slowly turn the boat "
                    "through a full circle, then press Finish."}
        if name == "calibrate_stop":
            col = self._collector
            self._collector = None
            if col is None:
                return {"ok": False, "message": "Calibration was not started."}
            result = col.result()
            if result is None:
                return {"ok": False, "message": "Not enough rotation to calibrate. "
                        "Turn the boat through a full slow circle and retry."}
            self.calibration = result
            if self._persist_cal is not None:
                try:
                    self._persist_cal(result.to_dict())
                except Exception as exc:  # noqa: BLE001
                    return {"ok": True, "message": f"Calibrated (not saved: {exc}).",
                            "calibration": result.to_dict()}
            return {"ok": True, "message": "Calibration saved.",
                    "calibration": result.to_dict()}
        if name == "redetect":
            self._close_bus()   # loop reopens + re-detects on the next tick
            return {"ok": True, "message": "Re-scanning the I2C bus…"}
        return {"ok": False, "message": f"unknown action {name!r}"}


def open_magnetometer_compass(
    port: str, bus: EventBus | None, *,
    hz: float = 5.0, motion_provider: MotionProvider | None = None,
    declination_mode: str = "auto", manual_declination_deg: float = 0.0,
    calibration: mag.Calibration | None = None,
    forward_axis: str = "x", right_axis: str = "y", invert: bool = False,
    persist_cal: Optional[Callable[[dict], None]] = None,
) -> MagnetometerCompass:
    """Build the driver for the I2C target in ``port`` (``i2c:<bus>[:<addr>]``)."""
    bus_num, addr = parse_i2c_target(port)
    return MagnetometerCompass(
        lambda: SmbusBus(bus_num), addr=addr, bus=bus, hz=hz,
        motion_provider=motion_provider, declination_mode=declination_mode,
        manual_declination_deg=manual_declination_deg, calibration=calibration,
        forward_axis=forward_axis, right_axis=right_axis, invert=invert,
        persist_cal=persist_cal,
    )


def _build(runtime: Any, cfg: Any) -> MagnetometerCompass:
    """Registry build hook: wire the driver to the runtime GPS motion (for
    auto-declination) + bus, applying persisted device-menu settings, and give it
    a callback that persists a fitted calibration to the config."""
    hw = cfg.hardware

    def motion():
        st = getattr(runtime, "state", None)
        if st is None or st.fix is None:
            return None
        return (st.fix.cog_deg, st.sog_knots * 0.514444)  # knots -> m/s

    def persist_cal(cal_dict: dict) -> None:
        from ...core.config import save_device_overrides  # local: avoid import cycle
        hw.device_settings.setdefault("compass", {})["calibration"] = cal_dict
        save_device_overrides(cfg.data_dir, hw, cfg.nmea_tcp, cfg.sim_motor)

    saved = (getattr(hw, "device_settings", None) or {}).get("compass", {})
    return open_magnetometer_compass(
        hw.compass_port, runtime.bus,
        hz=float(saved.get("hz", cfg.sensors.compass_hz)),
        motion_provider=motion,
        declination_mode=str(saved.get("declination_mode", "auto")),
        manual_declination_deg=float(saved.get("manual_declination_deg", 0.0)),
        calibration=mag.Calibration.from_dict(saved.get("calibration")),
        forward_axis=str(saved.get("forward_axis", "x")),
        right_axis=str(saved.get("right_axis", "y")),
        invert=bool(saved.get("invert", False)),
        persist_cal=persist_cal,
    )


register_driver("compass", "magnetometer", _build,
                label="I2C magnetometer (HMC5883L / QMC5883L / IST8310)",
                menu=default_menu())
