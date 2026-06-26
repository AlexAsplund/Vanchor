"""Realistic, variable, tunable weather (task #44).

Today the simulator has a steady base wind/current plus a fast Ornstein-
Uhlenbeck *gust* (:mod:`.gust`). Real weather also drifts slowly over a session:
the wind speed eases and freshens, the direction backs and veers, and on rivers
the current is strong and steady while lakes have almost none.

:class:`WeatherModel` adds that slow wander. It evolves three quantities with a
**much slower** OU process than gusts (minutes, not seconds):

- wind speed (m/s)
- wind direction (deg), as a wandering offset added to the base direction
- current speed (m/s)

A ``wind_variability`` / ``current_variability`` amount in ``[0, 1]`` scales how
far each wanders; ``0`` means perfectly steady (the value never changes). Gusts
still ride on top of the evolving base wind, applied in the simulator.

Presets (:data:`WEATHER_PRESETS`) bundle sensible base values + variability for
common water bodies (calm / lake / river / coastal) and can be applied live.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# Slow-wander correlation time: how many seconds the slow weather takes to
# meaningfully change. Minutes-scale, i.e. far slower than the ~5 s gust tau.
WIND_SPEED_TAU_S = 180.0
WIND_DIR_TAU_S = 240.0
CURRENT_TAU_S = 300.0

# Maximum excursion at variability=1, scaling the OU stationary std.
MAX_WIND_SPEED_SWING_MPS = 4.0  # +/- a few m/s of slow freshening/easing
MAX_WIND_DIR_SWING_DEG = 40.0  # +/- backing/veering of the wind
MAX_CURRENT_SWING_MPS = 0.4  # +/- slow current variation


def _ou_step(value: float, tau: float, sigma: float, dt: float, rng: random.Random) -> float:
    """One Ornstein-Uhlenbeck step (mean-reverting toward zero)."""
    if sigma <= 0.0 or dt <= 0.0:
        return 0.0
    return value + (-value / tau) * dt + sigma * math.sqrt(dt) * rng.gauss(0.0, 1.0)


@dataclass
class WeatherModel:
    """Slow, bounded wander of wind speed/direction and current.

    The model holds *offsets* from the steady base values; call :meth:`apply`
    each tick with the live base values to get the evolved values to use.
    """

    wind_variability: float = 0.0  # 0 = steady, 1 = full slow wander
    current_variability: float = 0.0
    seed: int = 271828

    _wind_speed_off: float = field(default=0.0, init=False)
    _wind_dir_off: float = field(default=0.0, init=False)
    _current_off: float = field(default=0.0, init=False)
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def reset(self) -> None:
        self._wind_speed_off = 0.0
        self._wind_dir_off = 0.0
        self._current_off = 0.0

    @staticmethod
    def _sigma(amount: float, swing: float, tau: float) -> float:
        """OU noise scale so the stationary std is ``amount * swing``."""
        amount = max(0.0, min(1.0, amount))
        std = amount * swing
        return std * math.sqrt(2.0 / tau)

    def step(self, dt: float) -> None:
        """Advance the slow wander by ``dt`` seconds."""
        wv = max(0.0, min(1.0, self.wind_variability))
        cv = max(0.0, min(1.0, self.current_variability))
        if wv <= 0.0:
            self._wind_speed_off = 0.0
            self._wind_dir_off = 0.0
        else:
            self._wind_speed_off = _ou_step(
                self._wind_speed_off,
                WIND_SPEED_TAU_S,
                self._sigma(wv, MAX_WIND_SPEED_SWING_MPS, WIND_SPEED_TAU_S),
                dt,
                self._rng,
            )
            self._wind_dir_off = _ou_step(
                self._wind_dir_off,
                WIND_DIR_TAU_S,
                self._sigma(wv, MAX_WIND_DIR_SWING_DEG, WIND_DIR_TAU_S),
                dt,
                self._rng,
            )
        if cv <= 0.0:
            self._current_off = 0.0
        else:
            self._current_off = _ou_step(
                self._current_off,
                CURRENT_TAU_S,
                self._sigma(cv, MAX_CURRENT_SWING_MPS, CURRENT_TAU_S),
                dt,
                self._rng,
            )

    # Evolved values given the steady base values --------------------------- #
    def wind_speed(self, base: float) -> float:
        return max(0.0, base + self._wind_speed_off)

    def wind_dir(self, base: float) -> float:
        return (base + self._wind_dir_off) % 360.0

    def current_speed(self, base: float) -> float:
        return max(0.0, base + self._current_off)


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WeatherPreset:
    id: str
    label: str
    current_speed: float
    current_dir: float
    wind_speed: float
    wind_dir: float
    gust_amplitude_mps: float
    wind_variability: float
    current_variability: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "current_speed": self.current_speed,
            "current_dir": self.current_dir,
            "wind_speed": self.wind_speed,
            "wind_dir": self.wind_dir,
            "gust_amplitude_mps": self.gust_amplitude_mps,
            "wind_variability": self.wind_variability,
            "current_variability": self.current_variability,
        }


WEATHER_PRESETS: dict[str, WeatherPreset] = {
    p.id: p
    for p in (
        WeatherPreset(
            id="calm",
            label="Calm",
            current_speed=0.0,
            current_dir=0.0,
            wind_speed=1.0,
            wind_dir=180.0,
            gust_amplitude_mps=0.3,
            wind_variability=0.1,
            current_variability=0.0,
        ),
        WeatherPreset(
            id="lake",
            label="Lake (gusty wind, no current)",
            current_speed=0.0,
            current_dir=0.0,
            wind_speed=4.0,
            wind_dir=200.0,
            gust_amplitude_mps=1.5,
            wind_variability=0.5,
            current_variability=0.0,
        ),
        WeatherPreset(
            id="river",
            label="River (strong steady current)",
            current_speed=1.2,
            current_dir=90.0,
            wind_speed=2.0,
            wind_dir=270.0,
            gust_amplitude_mps=0.5,
            wind_variability=0.2,
            current_variability=0.1,
        ),
        WeatherPreset(
            id="coastal",
            label="Coastal (wind + current + gusts)",
            current_speed=0.6,
            current_dir=45.0,
            wind_speed=7.0,
            wind_dir=225.0,
            gust_amplitude_mps=2.5,
            wind_variability=0.7,
            current_variability=0.5,
        ),
    )
}
