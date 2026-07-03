"""Simulated devices that implement the real hardware interfaces.

Because these subclass the same ABCs as future serial devices, the controller,
navigator and event wiring cannot tell the difference between simulated and
real hardware. The simulated GPS/compass derive noisy NMEA from the boat's
ground-truth state; the simulated motor records the latest command so the boat
physics can read it.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from typing import Callable

from ..core import events
from ..core.events import EventBus
from ..core.geo import angle_difference, mps_to_knots, offset_meters
from ..core.models import BoatState, ImuSample, MotorCommand
from ..hardware.interfaces import Actuator, MotorController, Sensor
from ..nav import nmea

logger = logging.getLogger("vanchor.sim.devices")

TruthFn = Callable[[], BoatState]

# A deliberately corrupt NMEA sentence used by the ``garbage`` fault (#37): it
# carries a ``*`` with a non-hex checksum field, which ``nmea.parse`` always
# rejects with an ``NmeaError`` regardless of the require_checksum flag.
_GARBAGE_NMEA = "$GPRMC,GARBAGE,DATA,####,,,*ZZ"


def _sign(x: float) -> int:
    """Return -1, 0, or 1 for the sign of *x*."""
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


class SimMotorController(MotorController):
    """Records the most recent command; the boat physics reads ``command``.

    Optional actuation shaping (all parameters default to zero = OFF) mirrors
    the delays present in the real firmware so sim-trained gains can be stress-
    tested against the actuation holes that cause real-water limit cycles.

    All three shaping stages are **opt-in** and composed in order:

    1. **reverse_delay_s** — when the commanded thrust direction flips (e.g.
       forward → reverse) the output is held at zero for this many seconds.
       Mirrors the applied-direction gate in firmware/engine.ino that prevents
       the prop from reversing before it has shed momentum (~0.9 s on hardware).

    2. **thrust_slew_per_s** — the applied thrust may not change faster than
       this normalized rate per second (0 = unlimited).  Models the soft-start
       ramp the ESC uses to limit inrush current.

    3. **thrust_lag_tau_s** — first-order (exponential) lag toward the slew-
       limited target, with time-constant tau (0 = instant).  Models prop spin-
       up inertia: the prop cannot instantly change speed even after the ESC
       has fully commanded it.

    **dt source**: the shaping state is advanced by calling ``step(dt)`` with
    the simulator's physics dt.  ``Simulator.step`` now calls it every physics
    tick, but ``step`` short-circuits to a no-op while every shaping parameter is
    zero (the default), so existing tuned gains and recorded scenarios are
    bit-for-bit unchanged until a parameter is set (via config / the device
    API).  Deterministic tests call ``step`` directly to control sim-time.
    """

    def __init__(
        self,
        *,
        reverse_delay_s: float = 0.0,
        thrust_slew_per_s: float = 0.0,
        thrust_lag_tau_s: float = 0.0,
    ) -> None:
        self._reverse_delay_s = reverse_delay_s
        self._thrust_slew_per_s = thrust_slew_per_s
        self._thrust_lag_tau_s = thrust_lag_tau_s
        self._requested = MotorCommand()
        self._applied_thrust: float = 0.0
        self._reverse_hold_remaining: float = 0.0

    def configure(
        self,
        *,
        reverse_delay_s: float | None = None,
        thrust_slew_per_s: float | None = None,
        thrust_lag_tau_s: float | None = None,
    ) -> None:
        """Update the shaping parameters on a live controller (roadmap #36).

        Only the supplied (non-``None``) parameters change; the rest are left as
        they are. Setting every parameter back to zero returns the controller to
        the transparent-passthrough default. Used by the device-config API so a
        bench operator can dial actuation shaping in without a restart.
        """
        if reverse_delay_s is not None:
            self._reverse_delay_s = float(reverse_delay_s)
        if thrust_slew_per_s is not None:
            self._thrust_slew_per_s = float(thrust_slew_per_s)
        if thrust_lag_tau_s is not None:
            self._thrust_lag_tau_s = float(thrust_lag_tau_s)

    def _shaping_enabled(self) -> bool:
        return (
            self._reverse_delay_s != 0.0
            or self._thrust_slew_per_s != 0.0
            or self._thrust_lag_tau_s != 0.0
        )

    def apply(self, command: MotorCommand) -> None:
        """Record *command*; also arms the reverse-delay gate when the thrust
        direction flips (positive → negative or negative → positive)."""
        if self._reverse_delay_s > 0.0:
            prev_sign = _sign(self._requested.thrust)
            new_sign = _sign(command.thrust)
            if prev_sign != 0 and new_sign != 0 and prev_sign != new_sign:
                self._reverse_hold_remaining = self._reverse_delay_s
        self._requested = command

    def step(self, dt: float) -> None:
        """Advance actuation shaping by *dt* seconds of simulator time.

        No-op when all shaping parameters are zero (the default).  Tests that
        exercise the opt-in shaping should call this after each ``apply`` to
        move sim time forward before reading ``command``.
        """
        if dt <= 0.0 or not self._shaping_enabled():
            return

        # Stage 1 — reverse-delay gate: hold output at zero while the timer runs.
        if self._reverse_hold_remaining > 0.0:
            self._reverse_hold_remaining = max(0.0, self._reverse_hold_remaining - dt)
            target = 0.0
        else:
            target = self._requested.thrust

        # Stage 2 — slew-rate limit.
        if self._thrust_slew_per_s > 0.0:
            max_delta = self._thrust_slew_per_s * dt
            target = self._applied_thrust + max(
                -max_delta, min(max_delta, target - self._applied_thrust)
            )

        # Stage 3 — first-order lag (exponential approach).
        if self._thrust_lag_tau_s > 0.0:
            alpha = min(1.0, dt / self._thrust_lag_tau_s)
            self._applied_thrust += alpha * (target - self._applied_thrust)
        else:
            self._applied_thrust = target

    @property
    def command(self) -> MotorCommand:
        if not self._shaping_enabled():
            # Default path: pass through instantly with no state mutation.
            return self._requested
        return MotorCommand(
            thrust=self._applied_thrust,
            steering=self._requested.steering,
        )


class SimServo(Actuator):
    """A trivial simulated servo/stepper, demonstrating the generic actuator
    interface. Not required for the control loop, but shows how a steering
    actuator would be modelled and tested."""

    def __init__(self) -> None:
        self._position = 0.0

    def set_normalized(self, value: float) -> None:
        self._position = max(-1.0, min(1.0, value))

    @property
    def position(self) -> float:
        return self._position


# Fault names a SimGps understands (roadmap #37). All default OFF; each is an
# explicit, independently-toggled degradation used to stress-test the autopilot's
# staleness/spike/parse defences against realistic receiver + serial failures.
GPS_FAULTS = ("dropout", "eof", "garbage", "glitch", "latency")
# Fault names a SimCompass understands (roadmap #37).
COMPASS_FAULTS = ("freeze", "garbage")


class SimGps(Sensor):
    def __init__(
        self,
        get_truth: TruthFn,
        bus: EventBus | None = None,
        *,
        update_hz: float = 1.0,
        # Steady, denoised plotter output (not ~1.5 m raw-receiver scatter); see
        # SensorConfig.gps_noise_m. Keeps the autopilot from chasing phantom XTE.
        position_noise_m: float = 0.35,
        seed: int | None = 1234,
    ) -> None:
        self.get_truth = get_truth
        self.bus = bus
        self.update_hz = update_hz
        self.position_noise_m = position_noise_m
        self._rng = random.Random(seed)
        self._task: asyncio.Task | None = None
        # --- fault-injection knobs (#37); all OFF by default ---------------- #
        # dropout / EOF: emit nothing so the fix goes stale (the loss-of-fix
        # failsafe should latch). glitch: a big position jump (spike-guard bait).
        # garbage: an unparseable sentence (parser must reject it). latency: a
        # baud-saturation model -- each fix is buffered and only published this
        # many seconds late, so fresh fixes arrive stale.
        self.fault_dropout = False
        self.fault_glitch = False
        self.fault_glitch_m = 50.0
        self.fault_garbage = False
        self.fault_latency_s = 0.0
        self._latency_buf: list[tuple[float, str]] = []

    def set_fault(self, name: str, enabled: bool = True, **params) -> bool:
        """Toggle a named GPS fault (see :data:`GPS_FAULTS`). Returns ``True`` if
        the fault name was recognised. ``dropout`` and ``eof`` are aliases (both
        silence the stream). ``glitch`` accepts ``glitch_m`` (jump size);
        ``latency`` accepts ``latency_s`` (delay). Unknown names are a no-op."""
        if name in ("dropout", "eof"):
            self.fault_dropout = bool(enabled)
        elif name == "glitch":
            self.fault_glitch = bool(enabled)
            if "glitch_m" in params:
                self.fault_glitch_m = float(params["glitch_m"])
        elif name == "garbage":
            self.fault_garbage = bool(enabled)
        elif name == "latency":
            self.fault_latency_s = float(params.get("latency_s", 2.0)) if enabled else 0.0
            if not enabled:
                self._latency_buf.clear()
        else:
            return False
        return True

    def sample(self, truth: BoatState | None = None) -> str:
        """Build one RMC sentence from ground truth (pure, for tests).

        Course/speed-over-ground are derived from the *ground* velocity (hull
        motion plus drift), exactly as a real GPS reports them -- so the
        controller can observe the wind/current drift in COG/SOG.

        Honours the ``garbage`` and ``glitch`` faults (#37): garbage returns an
        intentionally malformed sentence the parser rejects; glitch offsets the
        reported position by a large jump to exercise the spike guard."""
        truth = truth or self.get_truth()
        if self.fault_garbage:
            return _GARBAGE_NMEA
        noisy = offset_meters(
            truth.point,
            self._rng.gauss(0.0, self.position_noise_m),
            self._rng.gauss(0.0, self.position_noise_m),
        )
        if self.fault_glitch:
            noisy = offset_meters(noisy, self.fault_glitch_m, self.fault_glitch_m)
        sog_mps = math.hypot(truth.ground_ve, truth.ground_vn)
        # When essentially stationary COG is undefined; report the heading.
        if sog_mps > 0.05:
            cog = math.degrees(math.atan2(truth.ground_ve, truth.ground_vn)) % 360.0
        else:
            cog = truth.heading_deg
        return nmea.encode_rmc(noisy, sog_knots=mps_to_knots(sog_mps), cog_deg=cog)

    async def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        period = 1.0 / self.update_hz
        loop = asyncio.get_running_loop()
        next_deadline = loop.time() + period
        while True:
            try:
                # dropout / EOF: publish nothing so the navigator's fix ages out.
                if not self.fault_dropout and self.bus is not None:
                    now = loop.time()
                    if self.fault_latency_s > 0.0:
                        # Baud-saturation model: buffer this fix and only release
                        # ones that are at least fault_latency_s old, so fresh
                        # fixes always arrive stale.
                        self._latency_buf.append((now, self.sample()))
                        cutoff = now - self.fault_latency_s
                        while self._latency_buf and self._latency_buf[0][0] <= cutoff:
                            _, due = self._latency_buf.pop(0)
                            await self.bus.publish(events.NMEA_IN, due)
                    else:
                        await self.bus.publish(events.NMEA_IN, self.sample())
            except Exception:
                logger.exception("SimGps publish error; continuing")
            delay = next_deadline - loop.time()
            next_deadline += period
            if delay > 0:
                await asyncio.sleep(delay)


class SimDepthSounder(Sensor):
    """Simulated depth sounder: samples the synthetic bathymetry under the boat
    and emits DPT NMEA, exactly like a real transducer."""

    def __init__(
        self,
        get_truth: TruthFn,
        bathymetry,
        bus: EventBus | None = None,
        *,
        update_hz: float = 2.0,
        noise_m: float = 0.1,
        seed: int | None = 777,
    ) -> None:
        self.get_truth = get_truth
        self.bathymetry = bathymetry
        self.bus = bus
        self.update_hz = update_hz
        self.noise_m = noise_m
        self._rng = random.Random(seed)
        self._task: asyncio.Task | None = None

    def sample(self, truth: BoatState | None = None) -> str:
        truth = truth or self.get_truth()
        depth = self.bathymetry.depth_at(truth.point) + self._rng.gauss(0.0, self.noise_m)
        return nmea.encode_dpt(max(0.0, depth))

    async def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        period = 1.0 / self.update_hz
        loop = asyncio.get_running_loop()
        next_deadline = loop.time() + period
        while True:
            try:
                if self.bus is not None:
                    await self.bus.publish(events.NMEA_IN, self.sample())
            except Exception:
                logger.exception("SimDepthSounder publish error; continuing")
            delay = next_deadline - loop.time()
            next_deadline += period
            if delay > 0:
                await asyncio.sleep(delay)


class SimCompass(Sensor):
    def __init__(
        self,
        get_truth: TruthFn,
        bus: EventBus | None = None,
        *,
        update_hz: float = 5.0,
        heading_noise_deg: float = 1.0,
        seed: int | None = 4321,
        sea_state=None,
    ) -> None:
        self.get_truth = get_truth
        self.bus = bus
        self.update_hz = update_hz
        self.heading_noise_deg = heading_noise_deg
        self._rng = random.Random(seed)
        self._task: asyncio.Task | None = None
        self._prev_heading: float | None = None  # for the simulated yaw rate
        # Optional deterministic sea-state model (#38) driving the IMU. ``None``
        # (or a model with Hs<=0) leaves the flat-water IMU bit-for-bit unchanged.
        self.sea_state = sea_state
        self._sea_t = 0.0  # elapsed sim-time fed to the wave model
        # --- fault-injection knobs (#37); all OFF by default ---------------- #
        # freeze: heading stuck at the value captured when the fault engaged (a
        # hung magnetometer). garbage: an unparseable sentence the parser rejects.
        self.fault_freeze = False
        self.fault_garbage = False
        self._frozen_heading: float | None = None

    def set_fault(self, name: str, enabled: bool = True, **params) -> bool:
        """Toggle a named compass fault (see :data:`COMPASS_FAULTS`). Returns
        ``True`` if the fault name was recognised, else a no-op ``False``."""
        if name == "freeze":
            self.fault_freeze = bool(enabled)
            if not enabled:
                self._frozen_heading = None
        elif name == "garbage":
            self.fault_garbage = bool(enabled)
        else:
            return False
        return True

    def sample(self, truth: BoatState | None = None) -> str:
        truth = truth or self.get_truth()
        if self.fault_garbage:
            return _GARBAGE_NMEA
        if self.fault_freeze:
            # Latch the first heading seen under the fault and hold it forever.
            if self._frozen_heading is None:
                self._frozen_heading = truth.heading_deg
            return nmea.encode_hdm(self._frozen_heading)
        heading = truth.heading_deg + self._rng.gauss(0.0, self.heading_noise_deg)
        return nmea.encode_hdm(heading)

    def imu_sample(self, truth: BoatState, dt: float) -> ImuSample:
        """A flat-water simulated IMU: yaw rate from the heading change, ~1 g
        down, everything else ~0 plus light noise. Enough to exercise the IMU
        pipeline / data-collection path.

        When a :class:`~vanchor.sim.sea_state.SeaState` is attached and enabled
        (#38), its roll/pitch/heave motion (attitude, roll/pitch rates, and the
        gravity + heave accelerometer signature) is folded on top. With no sea
        state (or Hs<=0) the output is identical to the flat-water model, so the
        default behaviour is bit-for-bit preserved."""
        yaw_rate = 0.0
        if self._prev_heading is not None and dt > 0:
            yaw_rate = angle_difference(self._prev_heading, truth.heading_deg) / dt
        self._prev_heading = truth.heading_deg
        n = lambda s: self._rng.gauss(0.0, s)  # noqa: E731
        sample = ImuSample(
            ax=n(0.05), ay=n(0.05), az=9.80665 + n(0.05),
            gx=n(0.2), gy=n(0.2), gz=yaw_rate + n(0.3),
            roll_deg=n(0.3), pitch_deg=n(0.3), source="sim",
        )
        if self.sea_state is not None and self.sea_state.enabled:
            self._sea_t += max(0.0, dt)
            motion = self.sea_state.sample(self._sea_t)
            sample = self.sea_state.apply_to_imu(sample, motion)
        return sample

    async def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        period = 1.0 / self.update_hz
        loop = asyncio.get_running_loop()
        next_deadline = loop.time() + period
        while True:
            try:
                truth = self.get_truth()
                if self.bus is not None:
                    await self.bus.publish(events.NMEA_IN, self.sample(truth))
                    await self.bus.publish(events.IMU_IN, self.imu_sample(truth, period))
            except Exception:
                logger.exception("SimCompass publish error; continuing")
            delay = next_deadline - loop.time()
            next_deadline += period
            if delay > 0:
                await asyncio.sleep(delay)
