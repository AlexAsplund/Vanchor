"""Tests for the slow, variable, tunable weather model (task #44)."""

import statistics

from vanchor.core.models import Environment
from vanchor.sim.simulator import Simulator
from vanchor.sim.weather import WEATHER_PRESETS, WeatherModel


def test_steady_when_variability_zero():
    w = WeatherModel(wind_variability=0.0, current_variability=0.0)
    for _ in range(1000):
        w.step(1.0)
    assert w.wind_speed(5.0) == 5.0
    assert w.wind_dir(180.0) == 180.0
    assert w.current_speed(0.5) == 0.5


def test_wind_wanders_when_variable():
    w = WeatherModel(wind_variability=0.8, seed=1)
    speeds, dirs = [], []
    for _ in range(4000):
        w.step(1.0)
        speeds.append(w.wind_speed(5.0))
        dirs.append(w.wind_dir(180.0))
    # Both speed and direction must actually vary over the session.
    assert statistics.pstdev(speeds) > 0.2
    assert statistics.pstdev(dirs) > 1.0
    # ... but stay bounded / physical (slow wander, not white-noise spikes).
    assert all(s >= 0.0 for s in speeds)
    assert max(speeds) < 5.0 + 4 * 4.0  # within a few stationary std of base
    assert all(0.0 <= d < 360.0 for d in dirs)


def test_wander_is_slow():
    # Consecutive 1 s samples should change only a little (correlation time is
    # minutes), unlike a fast gust.
    w = WeatherModel(wind_variability=1.0, seed=2)
    prev = w.wind_speed(5.0)
    deltas = []
    for _ in range(500):
        w.step(1.0)
        cur = w.wind_speed(5.0)
        deltas.append(abs(cur - prev))
        prev = cur
    assert statistics.mean(deltas) < 0.5  # gentle per-second change


def test_current_variability_independent():
    w = WeatherModel(wind_variability=0.0, current_variability=0.6, seed=3)
    currents = []
    for _ in range(3000):
        w.step(1.0)
        currents.append(w.current_speed(0.5))
    assert statistics.pstdev(currents) > 0.02
    assert w.wind_speed(5.0) == 5.0  # wind untouched


def test_deterministic_with_seed():
    a = WeatherModel(wind_variability=0.7, seed=9)
    b = WeatherModel(wind_variability=0.7, seed=9)
    va = [(a.step(0.5), a.wind_speed(5.0))[1] for _ in range(50)]
    vb = [(b.step(0.5), b.wind_speed(5.0))[1] for _ in range(50)]
    assert va == vb


def test_presets_defined():
    assert {"calm", "lake", "river", "coastal"} <= set(WEATHER_PRESETS)
    lake = WEATHER_PRESETS["lake"]
    assert lake.current_speed == 0.0  # lakes have ~no current
    assert lake.wind_variability > 0.0
    river = WEATHER_PRESETS["river"]
    assert river.current_speed > 0.5  # strong steady current


def test_simulator_reflects_evolving_wind():
    env = Environment(wind_speed=5.0, wind_dir=180.0, wind_variability=0.9)
    sim = Simulator(environment=env, model="simple")
    seen_speeds, seen_dirs = set(), set()
    for _ in range(2000):
        sim.step(0.5)
        seen_speeds.add(round(sim.environment.wind_speed, 3))
        seen_dirs.add(round(sim.environment.wind_dir, 3))
    # The live environment values actually move over the session.
    assert len(seen_speeds) > 10
    assert len(seen_dirs) > 10


def test_simulator_steady_when_no_variability():
    env = Environment(wind_speed=5.0, wind_dir=180.0, wind_variability=0.0)
    sim = Simulator(environment=env, model="simple")
    for _ in range(500):
        sim.step(0.5)
    assert sim.environment.wind_speed == 5.0
    assert sim.environment.wind_dir == 180.0


def test_apply_preset_via_set_weather_base():
    env = Environment()
    sim = Simulator(environment=env, model="simple")
    preset = WEATHER_PRESETS["lake"]
    sim.environment.wind_speed = preset.wind_speed
    sim.environment.wind_variability = preset.wind_variability
    sim.set_weather_base()
    assert sim._base_wind_speed == preset.wind_speed
    # And it now wanders around that base.
    seen = set()
    for _ in range(1000):
        sim.step(0.5)
        seen.add(round(sim.environment.wind_speed, 2))
    assert len(seen) > 5
