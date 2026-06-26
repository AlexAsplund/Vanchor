"""Wind gusts: a smooth, time-varying perturbation on top of the base wind.

Real wind is not steady -- it gusts and lulls. We model that as an
Ornstein-Uhlenbeck process (a smoothed random walk that decays back toward
zero), which gives realistic ramping gusts rather than white noise. The result
is added to the base wind speed each physics step, so the controller has to cope
with a wind that surges and eases -- a good stress test for station-keeping.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field


@dataclass
class GustModel:
    amplitude_mps: float = 0.0  # ~std of the gust component; 0 disables gusts
    tau_s: float = 5.0  # correlation time -- how slowly gusts build and fade
    seed: int = 12345

    _value: float = field(default=0.0, init=False)
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def reset(self) -> None:
        self._value = 0.0

    def step(self, dt: float) -> float:
        """Advance the gust process by ``dt`` and return the current offset (m/s)."""
        if self.amplitude_mps <= 0.0 or dt <= 0.0:
            self._value = 0.0
            return 0.0
        # OU step: pull toward zero, plus scaled noise so the stationary std is
        # ~amplitude_mps.
        sigma = self.amplitude_mps * math.sqrt(2.0 / self.tau_s)
        self._value += -self._value / self.tau_s * dt + sigma * math.sqrt(dt) * self._rng.gauss(0.0, 1.0)
        return self._value
