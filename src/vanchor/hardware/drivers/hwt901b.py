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
from ...core.models import ImuSample
from ...nav import nmea
from ..interfaces import Sensor
from ..registry import register_driver

logger = logging.getLogger("vanchor.hardware.hwt901b")

_G = 9.80665  # standard gravity, to convert the HWT901B's g-units to m/s^2


def _imu_from_state(state, source: str = "hwt901b") -> ImuSample | None:
    """Build an :class:`ImuSample` from an ``hwt901b`` State (accel in g -> m/s^2,
    gyro deg/s, roll/pitch deg). None if neither accel nor gyro is present yet."""
    a = getattr(state, "acceleration", None)
    g = getattr(state, "angular_velocity", None)
    ang = getattr(state, "angle", None)
    if a is None and g is None:
        return None
    return ImuSample(
        ax=a.x * _G if a else 0.0, ay=a.y * _G if a else 0.0, az=a.z * _G if a else 0.0,
        gx=g.x if g else 0.0, gy=g.y if g else 0.0, gz=g.z if g else 0.0,
        roll_deg=ang.roll if ang else 0.0, pitch_deg=ang.pitch if ang else 0.0,
        source=source,
    )

# () -> (course_over_ground_deg, speed_over_ground_mps) or None when no fix.
MotionProvider = Callable[[], Optional[tuple]]


def _menu_schema(declination_mode: str, manual_declination_deg: float, hz: float) -> dict:
    """The HWT901B device_menu() schema for the given values -- used both live
    (an instance's values) and as the default the registry advertises so the UI
    can show the menu the moment ``hwt901b`` is selected."""
    return {
        "device": "compass",
        "title": "Compass — HWT901B AHRS",
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
        ],
        "actions": [
            {"name": "profile", "label": "Sensor status",
             "help": "Read the live heading + the learned declination/offset "
                     "(needs the device running)."},
            {"name": "calibrate_mag", "label": "Calibrate magnetometer",
             "help": "Slowly rotate the boat through a full circle to fit the "
                     "hard/soft-iron correction (interactive; coming next)."},
        ],
    }


def default_menu() -> dict:
    """The default (factory) menu schema advertised by the registry."""
    return _menu_schema("auto", 0.0, 5.0)


# --- HeadingOffsetEstimator settling thresholds (named so they're easy to tune) ---
# Require this many straight-run samples before calling the offset "settled"; a
# single coincidental reading should never trigger settled.
_MIN_SETTLE_SAMPLES: int = 10
# Also require this much accumulated straight-run time (seconds); prevents settling
# on a burst of fast readings that happen to agree.
_MIN_SETTLE_TIME_S: float = 30.0
# EMA time constant for the residual-spread tracker (seconds); slower than the
# offset TC so it captures medium-term inconsistency, not just instantaneous noise.
_SPREAD_TC_S: float = 20.0
# If the EMA of |residual| exceeds this, the samples are too inconsistent to trust
# (e.g. reciprocal-course crab shows up as alternating offsets).
_MAX_SPREAD_DEG: float = 5.0


class HeadingOffsetEstimator:
    """Learn the fixed offset (declination + compass mount error) between the
    magnetic heading and true north, from GPS course-over-ground.

    While the boat runs roughly straight above ``min_sog_mps``,
    ``course_over_ground - magnetic_heading`` is that offset (plus noise). We
    low-pass it with a long time constant so turns and GPS jitter average out and
    only sustained straight-line agreement moves the estimate.

    **Settling criteria** — ``settled`` becomes ``True`` only when all of:
    * at least ``_MIN_SETTLE_SAMPLES`` straight-run samples have been accepted,
    * at least ``_MIN_SETTLE_TIME_S`` of straight-run time has accumulated, and
    * the EMA of |residual| (``_spread_ema``) is below ``_MAX_SPREAD_DEG``.

    The spread gate defends against inconsistent offset estimates. If the boat
    repeatedly changes course and the cog−heading differences alternate (e.g. ±5°),
    the spread stays large and the estimator does not settle.

    **Known limitation** — a *constant* crab angle (beam current/wind, always on
    the same tack through every learning run) is indistinguishable from declination
    and will still bias the offset estimate. Varying conditions (different headings,
    different crab directions) are needed to expose it via spread."""

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
        self._n_samples: int = 0
        self._run_time_s: float = 0.0
        self._spread_ema: float = 0.0

    def update(self, magnetic_heading_deg: float, cog_deg: float | None,
               sog_mps: float | None, dt: float,
               yaw_rate_dps: float | None = None) -> float:
        """Update the offset estimate and return the current offset.

        ``yaw_rate_dps`` — if supplied (from the IMU gyro z-axis), it is used as
        the turn-rate gate instead of the COG-difference fallback, giving a more
        immediate and accurate straight-line check."""
        if cog_deg is None or sog_mps is None or sog_mps < self.min_sog_mps or dt <= 0:
            self._prev_cog = cog_deg
            return self.offset_deg
        # Turn-rate gate: prefer gyro when available (more immediate), fall back to
        # COG difference which lags by one frame.
        if yaw_rate_dps is not None:
            if abs(yaw_rate_dps) > self.max_turn_dps:
                self._prev_cog = cog_deg
                return self.offset_deg
        elif self._prev_cog is not None:
            if abs(angle_difference(self._prev_cog, cog_deg)) / dt > self.max_turn_dps:
                self._prev_cog = cog_deg  # mid-turn -> skip (COG != heading here)
                return self.offset_deg
        self._prev_cog = cog_deg
        target = angle_difference(magnetic_heading_deg, cog_deg)  # signed cog - mag
        a = min(1.0, dt / self.time_constant_s)
        residual = angle_difference(self.offset_deg, target)
        self.offset_deg = normalize_deg(self.offset_deg + a * residual)
        # Track spread as EMA of |residual|; large spread → inconsistent samples.
        sa = min(1.0, dt / _SPREAD_TC_S)
        self._spread_ema += sa * (abs(residual) - self._spread_ema)
        self._n_samples += 1
        self._run_time_s += dt
        self.settled = (
            self._n_samples >= _MIN_SETTLE_SAMPLES
            and self._run_time_s >= _MIN_SETTLE_TIME_S
            and self._spread_ema <= _MAX_SPREAD_DEG
        )
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

    def _current_declination(self, magnetic_heading: float, dt: float,
                              yaw_rate_dps: float | None = None) -> float:
        if self.declination_mode == "manual":
            return self.manual_declination_deg
        if self.declination_mode == "off":
            return 0.0
        cog = sog = None
        if self.motion_provider is not None:
            mv = self.motion_provider()
            if mv is not None:
                cog, sog = mv
        return self.estimator.update(magnetic_heading, cog, sog, dt, yaw_rate_dps)

    async def sample_once(self, dt: float) -> str | None:
        """Read one full AHRS state and return the HDM heading sentence (or None
        on timeout). Also publishes the raw IMU sample (accel+gyro) on
        :data:`events.IMU_IN` for logging/analysis. One ``read_state`` gets both;
        the blocking serial read runs in a thread so it never stalls the loop."""
        loop = asyncio.get_event_loop()
        try:
            state = await loop.run_in_executor(
                None, lambda: self._sensor.read_state(self.read_timeout)
            )
        except Exception as exc:  # noqa: BLE001 - timeout/parse; keep the loop alive
            logger.debug("hwt901b read: %s", exc)
            return None
        angle = getattr(state, "angle", None)
        if angle is None:
            return None  # no fused heading yet
        # Module yaw is CCW-positive [-180,180]; a compass bearing is CW-positive
        # [0,360) -> negate (matches hwt901b.calibration.yaw_to_heading at decl 0).
        magnetic = normalize_deg(-angle.yaw)
        # Pass gyro z (yaw rate, deg/s) to the estimator when available; it gives a
        # more immediate straight-line gate than the COG-difference fallback.
        g = getattr(state, "angular_velocity", None)
        yaw_rate = g.z if g is not None else None
        heading = normalize_deg(magnetic + self._current_declination(magnetic, dt, yaw_rate))
        self.last_heading_deg = heading
        if self.bus is not None:
            imu = _imu_from_state(state)
            if imu is not None:
                await self.bus.publish(events.IMU_IN, imu)
        return nmea.encode_hdm(heading)

    async def _loop(self) -> None:
        while True:
            period = 1.0 / self.hz  # recompute each iteration so hz changes apply live
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
        return _menu_schema(self.declination_mode, self.manual_declination_deg, self.hz)

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
    (for auto-declination) and bus, applying persisted device-menu settings."""
    hw = cfg.hardware

    def motion():
        st = getattr(runtime, "state", None)
        if st is None or st.fix is None:
            return None
        return (st.fix.cog_deg, st.sog_knots * 0.514444)  # knots -> m/s

    saved = (getattr(hw, "device_settings", None) or {}).get("compass", {})
    return open_hwt901b_compass(
        hw.compass_port, hw.baudrate, runtime.bus,
        hz=float(saved.get("hz", cfg.sensors.compass_hz)),
        motion_provider=motion,
        declination_mode=str(saved.get("declination_mode", "auto")),
        manual_declination_deg=float(saved.get("manual_declination_deg", 0.0)),
    )


register_driver("compass", "hwt901b", _build,
                label="WitMotion HWT901B AHRS", menu=default_menu())
