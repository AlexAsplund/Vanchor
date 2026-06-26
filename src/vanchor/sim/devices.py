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
from ..core.geo import mps_to_knots, offset_meters
from ..core.models import BoatState, MotorCommand
from ..hardware.interfaces import Actuator, MotorController, Sensor
from ..nav import nmea

logger = logging.getLogger("vanchor.sim.devices")

TruthFn = Callable[[], BoatState]


class SimMotorController(MotorController):
    """Records the most recent command; the boat physics reads ``command``."""

    def __init__(self) -> None:
        self._command = MotorCommand()

    def apply(self, command: MotorCommand) -> None:
        self._command = command

    @property
    def command(self) -> MotorCommand:
        return self._command


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

    def sample(self, truth: BoatState | None = None) -> str:
        """Build one noisy RMC sentence from ground truth (pure, for tests).

        Course/speed-over-ground are derived from the *ground* velocity (hull
        motion plus drift), exactly as a real GPS reports them -- so the
        controller can observe the wind/current drift in COG/SOG."""
        truth = truth or self.get_truth()
        noisy = offset_meters(
            truth.point,
            self._rng.gauss(0.0, self.position_noise_m),
            self._rng.gauss(0.0, self.position_noise_m),
        )
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

    async def _loop(self) -> None:
        period = 1.0 / self.update_hz
        while True:
            sentence = self.sample()
            if self.bus is not None:
                await self.bus.publish(events.NMEA_IN, sentence)
            await asyncio.sleep(period)


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

    async def _loop(self) -> None:
        period = 1.0 / self.update_hz
        while True:
            if self.bus is not None:
                await self.bus.publish(events.NMEA_IN, self.sample())
            await asyncio.sleep(period)


class SimCompass(Sensor):
    def __init__(
        self,
        get_truth: TruthFn,
        bus: EventBus | None = None,
        *,
        update_hz: float = 5.0,
        heading_noise_deg: float = 1.0,
        seed: int | None = 4321,
    ) -> None:
        self.get_truth = get_truth
        self.bus = bus
        self.update_hz = update_hz
        self.heading_noise_deg = heading_noise_deg
        self._rng = random.Random(seed)
        self._task: asyncio.Task | None = None

    def sample(self, truth: BoatState | None = None) -> str:
        truth = truth or self.get_truth()
        heading = truth.heading_deg + self._rng.gauss(0.0, self.heading_noise_deg)
        return nmea.encode_hdm(heading)

    async def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        period = 1.0 / self.update_hz
        while True:
            sentence = self.sample()
            if self.bus is not None:
                await self.bus.publish(events.NMEA_IN, sentence)
            await asyncio.sleep(period)
