"""Roadmap #38: deterministic sea-state model driving the sim IMU.

Covers:
  * the wave model is deterministic (seeded; no wall-clock) and off at Hs<=0;
  * with waves ON the sim IMU shows the expected roll/pitch/heave oscillation;
  * with waves OFF (default / no sea state) the IMU is bit-for-bit unchanged
    from the flat-water model (regression);
  * the config field wires through ``SeaState.from_config``.
"""

from __future__ import annotations

import math

import pytest

from vanchor.core.config import AppConfig, SeaStateConfig
from vanchor.core.models import BoatState, GeoPoint
from vanchor.sim.devices import SimCompass
from vanchor.sim.sea_state import SeaState

_HERE = GeoPoint(59.3293, 18.0686)


def _truth(heading: float = 0.0) -> BoatState:
    return BoatState(point=_HERE, heading_deg=heading)


# --- model-level ---------------------------------------------------------- #
def test_off_by_default() -> None:
    s = SeaState.from_config(SeaStateConfig())  # Hs = 0
    assert s.enabled is False
    m = s.sample(1.234)
    assert m == type(m)()  # all-zero WaveMotion


def test_deterministic_same_seed() -> None:
    a = SeaState(significant_wave_height_m=1.0, peak_period_s=4.0, seed=7)
    b = SeaState(significant_wave_height_m=1.0, peak_period_s=4.0, seed=7)
    for t in (0.0, 0.5, 1.7, 3.3):
        assert a.sample(t) == b.sample(t)


def test_different_seed_differs() -> None:
    a = SeaState(significant_wave_height_m=1.0, seed=1)
    b = SeaState(significant_wave_height_m=1.0, seed=2)
    assert a.sample(1.0) != b.sample(1.0)


def test_beam_sea_is_mostly_roll() -> None:
    s = SeaState(significant_wave_height_m=1.5, peak_period_s=4.0, heading_deg=90.0)
    # Sample the peak of the oscillation over a period; roll should dominate pitch.
    max_roll = max(abs(s.sample(t).roll_deg) for t in _times(8.0))
    max_pitch = max(abs(s.sample(t).pitch_deg) for t in _times(8.0))
    assert max_roll > 1.0
    assert max_pitch < 1e-6  # beam sea -> no pitch


def test_amplitude_scales_with_wave_height() -> None:
    small = SeaState(significant_wave_height_m=0.5, heading_deg=90.0, seed=3)
    big = SeaState(significant_wave_height_m=2.0, heading_deg=90.0, seed=3)
    small_max = max(abs(small.sample(t).roll_deg) for t in _times(8.0))
    big_max = max(abs(big.sample(t).roll_deg) for t in _times(8.0))
    assert big_max > 2.0 * small_max


def _times(span: float, n: int = 400):
    return [span * i / n for i in range(n)]


# --- IMU integration ------------------------------------------------------ #
def test_imu_regression_when_off_matches_flat_water() -> None:
    """No sea state vs a disabled (Hs=0) sea state must give BIT-FOR-BIT identical
    IMU output (same seed, same call sequence)."""
    base = SimCompass(_truth, bus=None, seed=99)
    disabled = SimCompass(_truth, bus=None, seed=99,
                          sea_state=SeaState.from_config(SeaStateConfig()))
    for _ in range(20):
        a = base.imu_sample(_truth(10.0), 0.1)
        b = disabled.imu_sample(_truth(10.0), 0.1)
        assert (a.ax, a.ay, a.az) == (b.ax, b.ay, b.az)
        assert (a.gx, a.gy, a.gz) == (b.gx, b.gy, b.gz)
        assert (a.roll_deg, a.pitch_deg) == (b.roll_deg, b.pitch_deg)


def test_imu_oscillates_with_waves_on() -> None:
    """With waves enabled the IMU roll and gyro/accel must actually oscillate
    (span well beyond the ~0.3 deg flat-water noise floor)."""
    sea = SeaState(significant_wave_height_m=1.5, peak_period_s=3.0,
                   heading_deg=90.0, seed=5)
    compass = SimCompass(_truth, bus=None, seed=99, sea_state=sea)
    rolls, gxs, ays = [], [], []
    for _ in range(200):  # 200 * 0.05 s = 10 s -> a few wave periods
        s = compass.imu_sample(_truth(0.0), 0.05)
        rolls.append(s.roll_deg)
        gxs.append(s.gx)
        ays.append(s.ay)
    roll_span = max(rolls) - min(rolls)
    assert roll_span > 4.0            # multi-degree roll swing
    assert max(gxs) - min(gxs) > 2.0  # roll-rate gyro swings
    # Lateral accel picks up the gravity projection of the roll (~g*sin(roll)).
    assert max(ays) - min(ays) > 0.5


def test_imu_heave_shows_in_vertical_accel() -> None:
    """Heave must perturb az away from the flat-water ~1 g baseline."""
    sea = SeaState(significant_wave_height_m=2.0, peak_period_s=3.0,
                   heading_deg=0.0, seed=8)
    compass = SimCompass(_truth, bus=None, seed=1, heading_noise_deg=0.0, sea_state=sea)
    azs = [compass.imu_sample(_truth(0.0), 0.05).az for _ in range(200)]
    span = max(azs) - min(azs)
    assert span > 0.3  # vertical acceleration swings with the heave


def test_config_field_wires_through() -> None:
    cfg = AppConfig()
    cfg.sea_state = SeaStateConfig(significant_wave_height_m=1.0, peak_period_s=5.0)
    s = SeaState.from_config(cfg.sea_state)
    assert s.enabled is True
    assert s.peak_period_s == pytest.approx(5.0)
    # A period-5 peak component: angular frequency 2*pi/5.
    assert any(abs(c.w - 2 * math.pi / 5.0) < 1e-9 for c in s._roll)
