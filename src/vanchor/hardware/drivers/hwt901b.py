"""Pluggable compass driver for the WitMotion HWT901B-TTL 9-axis AHRS.

Wraps the external ``hwt901b`` library and adapts it to vanchor's device seam: a
:class:`~vanchor.hardware.interfaces.Sensor` that emits ``HDM`` NMEA onto the bus
(``nmea.in``), exactly like ``SimCompass``/``SerialCompass`` -- so the navigator,
controller and every mode are unchanged. Registers itself as the compass source
``"hwt901b"`` (see :mod:`vanchor.hardware.registry`), so it needs no edit to
``app.py``'s build seam.

Two device-specific features:

* **Auto-declination (+ mount offset).** The magnetometer reads *magnetic*
  heading; true heading needs the local declination, and a real install has a
  fixed mount misalignment. Rather than make the skipper type a number, the
  combined offset is learned by comparing the compass heading to the GPS
  course-over-ground on straight-line runs (:class:`HeadingOffsetEstimator`) --
  no magnetic-model data to ship, and the mount error is corrected for free.
* **Device menu.** :meth:`HWT901BCompass.device_menu` advertises device-specific
  settings + actions (declination mode, magnetometer calibration, profiling) the
  UI renders generically -- the pattern any future smart device follows.

The ``hwt901b`` library is optional and imported lazily, so the core install and
the simulator never need it; the driver itself is fully testable with a fake
sensor (no serial port).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from ...core import events
from ...core.events import EventBus
from ...core.geo import angle_difference, normalize_deg
from ...nav import nmea
from ..interfaces import Sensor
from ..registry import register_driver

logger = logging.getLogger("vanchor.hardware.hwt901b")

# () -> (course_over_ground_deg, speed_over_ground_mps) or None when no fix.
MotionProvider = Callable[[], Optional[tuple]]


class HeadingOffsetEstimator:
    """Learn the fixed offset (declination + compass mount error) between the
    magnetic heading and true north, from GPS course-over-ground.

    While the boat runs roughly straight above ``min_sog_mps``,
    ``course_over_ground - magnetic_heading`` is that offset (plus noise). We
    low-pass it with a long time constant so turns and GPS jitter average out and
    only sustained straight-line agreement moves the estimate."""

    def __init__(
        self, *, min_sog_mps: float = 0.8, time_constant_s: float = 25.0,
        max_turn_dps: float = 8.0,
    ) -> None:
        self.min_sog_mps = min_sog_mps
        self.time_constant_s = time_constant_s
        self.max_turn_dps = max_turn_dps
        self.offset_deg: float = 0.0
        self.settled: bool = False
        self._prev_cog: float | None = None

    def update(self, magnetic_heading_deg: float, cog_deg: float | None,
               sog_mps: float | None, dt: float) -> float:
        if cog_deg is None or sog_mps is None or sog_mps < self.min_sog_mps or dt <= 0:
            self._prev_cog = cog_deg
            return self.offset_deg
        if self._prev_cog is not None:
            if abs(angle_difference(self._prev_cog, cog_deg)) / dt > self.max_turn_dps:
                self._prev_cog = cog_deg  # mid-turn -> skip (COG != heading here)
                return self.offset_deg
        self._prev_cog = cog_deg
        target = angle_difference(magnetic_heading_deg, cog_deg)  # signed cog - mag
        a = min(1.0, dt / self.time_constant_s)
        self.offset_deg = normalize_deg(
            self.offset_deg + a * angle_difference(self.offset_deg, target)
        )
        self.settled = True
        return self.offset_deg


class HWT901BCompass(Sensor):
    """HWT901B AHRS presented as a vanchor NMEA compass sensor.

    ``sensor`` is anything with ``read_true_heading(declination_deg, timeout)``
    and ``close()`` -- the real :class:`hwt901b.HWT901B` or a fake in tests.
    ``motion_provider`` feeds GPS (cog, sog) for auto-declination."""

    def __init__(
        self, sensor: Any, bus: EventBus | None = None, *,
        hz: float = 5.0,
        motion_provider: MotionProvider | None = None,
        declination_mode: str = "auto",           # "auto" | "manual" | "off"
        manual_declination_deg: float = 0.0,
        read_timeout: float = 0.5,
    ) -> None:
        self._sensor = sensor
        self.bus = bus
        self.hz = max(0.5, hz)
        self.motion_provider = motion_provider
        self.declination_mode = declination_mode
        self.manual_declination_deg = manual_declination_deg
        self.read_timeout = read_timeout
        self.estimator = HeadingOffsetEstimator()
        self.last_heading_deg: float | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        close = getattr(self._sensor, "close", None)
        if close is not None:
            await asyncio.get_event_loop().run_in_executor(None, close)

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

    async def sample_once(self, dt: float) -> str | None:
        """Read one heading and return the HDM sentence (or None on timeout). The
        blocking serial read runs in a thread so it never stalls the loop."""
        loop = asyncio.get_event_loop()
        try:
            magnetic = await loop.run_in_executor(
                None, lambda: self._sensor.read_true_heading(0.0, self.read_timeout)
            )
        except Exception as exc:  # noqa: BLE001 - timeout/parse; keep the loop alive
            logger.debug("hwt901b read: %s", exc)
            return None
        heading = normalize_deg(magnetic + self._current_declination(magnetic, dt))
        self.last_heading_deg = heading
        return nmea.encode_hdm(heading)

    async def _loop(self) -> None:
        period = 1.0 / self.hz
        while True:
            try:
                sentence = await self.sample_once(period)
                if sentence and self.bus is not None:
                    await self.bus.publish(events.NMEA_IN, sentence)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                logger.exception("hwt901b compass loop error")
            await asyncio.sleep(period)

    # -- device-specific settings menu (rendered generically by the UI) ---- #
    def device_menu(self) -> dict:
        return {
            "device": "compass",
            "title": "Compass — HWT901B AHRS",
            "settings": [
                {"key": "declination_mode", "label": "Declination", "type": "select",
                 "options": ["auto", "manual", "off"], "value": self.declination_mode,
                 "help": "auto = learn declination + mount offset from GPS course."},
                {"key": "manual_declination_deg", "label": "Manual declination",
                 "type": "number", "min": -30, "max": 30, "step": 0.1, "unit": "°",
                 "value": round(self.manual_declination_deg, 1),
                 "shown_when": {"declination_mode": "manual"}},
                {"key": "hz", "label": "Update rate", "type": "number",
                 "min": 1, "max": 50, "step": 1, "unit": "Hz", "value": self.hz},
            ],
            "actions": [
                {"name": "profile", "label": "Sensor status",
                 "help": "Read the live heading + the learned declination/offset."},
                {"name": "calibrate_mag", "label": "Calibrate magnetometer",
                 "help": "Slowly rotate the boat through a full circle to fit the "
                         "hard/soft-iron correction (interactive; coming next)."},
            ],
        }

    def apply_setting(self, key: str, value: Any) -> dict:
        if key == "declination_mode" and value in ("auto", "manual", "off"):
            self.declination_mode = value
        elif key == "manual_declination_deg":
            self.manual_declination_deg = float(value)
        elif key == "hz":
            self.hz = max(0.5, float(value))
        else:
            return {"ok": False, "message": f"unknown setting {key!r}"}
        return {"ok": True}

    def run_action(self, name: str, params: dict | None = None) -> dict:
        if name == "profile":
            hdg = self.last_heading_deg
            return {"ok": True, "message": "HWT901B live status.", "status": {
                "heading_deg": round(hdg, 1) if hdg is not None else None,
                "declination_mode": self.declination_mode,
                "offset_deg": round(self.estimator.offset_deg, 1),
                "offset_settled": self.estimator.settled,
            }}
        if name == "calibrate_mag":
            return {"ok": False, "message": "Interactive mag calibration is coming next."}
        return {"ok": False, "message": f"unknown action {name!r}"}


def open_hwt901b_compass(
    port: str, baudrate: int, bus: EventBus | None, *,
    hz: float = 5.0, motion_provider: MotionProvider | None = None,
    declination_mode: str = "auto", manual_declination_deg: float = 0.0,
) -> HWT901BCompass:
    """Open the HWT901B on ``port`` and wrap it. Lazily imports the optional
    ``hwt901b`` library (clear error if missing)."""
    try:
        from hwt901b import HWT901B
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "compass_source='hwt901b' needs the hwt901b library: "
            "pip install 'vanchor[hwt901b]' (or `pip install -e ../python-hwt901b-ttl[serial]`)"
        ) from exc
    return HWT901BCompass(
        HWT901B.open(port, baudrate=baudrate), bus, hz=hz,
        motion_provider=motion_provider, declination_mode=declination_mode,
        manual_declination_deg=manual_declination_deg,
    )


def _build(runtime: Any, cfg: Any) -> HWT901BCompass:
    """Registry build hook: create the driver wired to the runtime's GPS motion
    (for auto-declination) and bus."""
    hw = cfg.hardware

    def motion():
        st = getattr(runtime, "state", None)
        if st is None or st.fix is None:
            return None
        return (st.fix.cog_deg, st.sog_knots * 0.514444)  # knots -> m/s

    return open_hwt901b_compass(
        hw.compass_port, hw.baudrate, runtime.bus,
        hz=cfg.sensors.compass_hz, motion_provider=motion,
    )


register_driver("compass", "hwt901b", _build, label="WitMotion HWT901B AHRS")
