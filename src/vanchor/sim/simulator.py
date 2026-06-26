"""The simulator ties the boat, the environment and the simulated devices
together and runs the physics forward in time.

It owns ground truth. The simulated GPS/compass read that truth and publish
noisy NMEA; the simulated motor controller is read each physics tick to drive
the boat. The result is a closed loop:

    motor command -> boat physics -> GPS/compass NMEA -> navigator -> state
        -> control mode -> helm -> motor command -> ...

``step(dt)`` advances physics once (used by deterministic tests); ``run``
drives it in real time for the live UI.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging

from ..core.models import BoatState, Environment
from .battery import Battery, BatteryConfig
from .boat import Boat, BoatParams
from .devices import SimCompass, SimGps, SimMotorController
from .gust import GustModel
from .weather import WeatherModel

logger = logging.getLogger("vanchor.sim")


def _make_boat(model: str, start: BoatState | None, params: BoatParams | None):
    """Boat-model factory. ``"simple"`` is the first-order default; ``"fossen"``
    is the higher-fidelity 3-DOF maneuvering model. Both share the same
    ``step``/``truth``/``.state`` interface so they are interchangeable."""
    if model == "fossen":
        from .fossen import FossenBoat, FossenParams

        fossen_params = params if isinstance(params, FossenParams) else None
        return FossenBoat(start, fossen_params)
    return Boat(start, params)


class Simulator:
    def __init__(
        self,
        *,
        start: BoatState | None = None,
        params: BoatParams | None = None,
        environment: Environment | None = None,
        physics_hz: float = 20.0,
        time_scale: float = 1.0,
        model: str = "simple",
        battery_config: BatteryConfig | None = None,
    ) -> None:
        self.boat = _make_boat(model, start, params)
        self.model = model
        self.environment = environment or Environment()
        self.motor = SimMotorController()
        # Simulated battery: drained by the applied thrust each physics step.
        self.battery = Battery(battery_config)
        self.physics_hz = physics_hz
        self.time_scale = time_scale
        self._gust = GustModel(
            amplitude_mps=self.environment.gust_amplitude_mps,
            tau_s=self.environment.gust_tau_s,
        )
        # Slow, session-scale weather wander (much slower than gusts). Evolves
        # relative to the user-set *base* values captured here; we write the
        # evolved values back into ``environment`` so telemetry shows them live.
        self._weather = WeatherModel(
            wind_variability=self.environment.wind_variability,
            current_variability=self.environment.current_variability,
        )
        self._base_wind_speed = self.environment.wind_speed
        self._base_wind_dir = self.environment.wind_dir
        self._base_current_speed = self.environment.current_speed
        self.current_gust_mps = 0.0  # last applied gust offset (for telemetry)
        self._running = False

    def set_weather_base(self) -> None:
        """Re-capture the current environment values as the steady base.

        Call after externally setting ``environment`` (e.g. a ``set_environment``
        command or a preset) so the slow wander wanders around the new values
        rather than treating an already-evolved value as the base.
        """
        self._base_wind_speed = self.environment.wind_speed
        self._base_wind_dir = self.environment.wind_dir
        self._base_current_speed = self.environment.current_speed
        self._weather.wind_variability = self.environment.wind_variability
        self._weather.current_variability = self.environment.current_variability
        self._weather.reset()

    # ------------------------------------------------------------------ #
    # Deterministic stepping (tests)
    # ------------------------------------------------------------------ #
    def step(self, dt: float) -> None:
        """Advance the boat one physics step using the latest motor command.

        Gusts are layered on top of the (user-set) base wind for this step only,
        so ``self.environment.wind_speed`` remains the steady base value."""
        env = self.environment
        # Slow weather wander: evolve offsets and write the evolving base wind /
        # current back into the live environment so telemetry reflects them.
        self._weather.wind_variability = env.wind_variability
        self._weather.current_variability = env.current_variability
        if env.wind_variability > 0.0 or env.current_variability > 0.0:
            self._weather.step(dt)
            env.wind_speed = self._weather.wind_speed(self._base_wind_speed)
            env.wind_dir = self._weather.wind_dir(self._base_wind_dir)
            env.current_speed = self._weather.current_speed(self._base_current_speed)

        self._gust.amplitude_mps = env.gust_amplitude_mps
        self._gust.tau_s = env.gust_tau_s
        self.current_gust_mps = self._gust.step(dt)
        step_env = env
        if self.current_gust_mps:
            # Gusts ride on top of the (possibly evolving) base wind, for this
            # physics step only -- the live env keeps the slow base value.
            step_env = dataclasses.replace(
                env, wind_speed=max(0.0, env.wind_speed + self.current_gust_mps)
            )
        self.boat.step(dt, self.motor.command, step_env)

        # Drain the battery for the thrust we just applied, using the boat's
        # ground speed for the range estimate.
        self.battery.step(dt, self.motor.command.thrust, self.boat.state.speed_mps)

    def truth(self) -> BoatState:
        return self.boat.truth()

    def teleport(self, lat: float, lon: float, heading: float | None = None) -> None:
        """Snap the simulated boat's ground truth to ``(lat, lon)`` and stop it.

        Optionally sets the heading. The boat's surge/sway/yaw velocities are
        zeroed so it doesn't keep coasting from its pre-teleport momentum."""
        from ..core.models import GeoPoint

        self.boat.teleport(GeoPoint(float(lat), float(lon)), heading)
        logger.info("teleported boat to (%.6f, %.6f)", float(lat), float(lon))

    # ------------------------------------------------------------------ #
    # Real-time loop (live UI)
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        self._running = True
        period = 1.0 / self.physics_hz
        logger.info("simulator physics loop started at %.0f Hz", self.physics_hz)
        while self._running:
            self.step(period * self.time_scale)
            await asyncio.sleep(period)

    def stop(self) -> None:
        self._running = False
