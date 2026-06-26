"""Tests for wind gusts."""

import statistics

from vanchor.analysis import anchor_metrics, run_scenario
from vanchor.analysis.scenarios import SCENARIOS
from vanchor.sim.gust import GustModel


def test_no_gust_when_disabled():
    g = GustModel(amplitude_mps=0.0)
    assert all(g.step(0.05) == 0.0 for _ in range(100))


def test_gust_varies_both_ways_and_centers():
    g = GustModel(amplitude_mps=3.0, tau_s=5.0, seed=1)
    vals = [g.step(0.05) for _ in range(4000)]
    assert max(vals) > 1.0 and min(vals) < -1.0  # gusts and lulls
    assert abs(statistics.mean(vals)) < 1.0  # roughly centered on the base wind
    assert statistics.pstdev(vals) > 1.0  # meaningfully variable
    assert max(vals) < 20.0  # but bounded/smooth, not white noise spikes


def test_gust_deterministic_with_seed():
    a = [GustModel(amplitude_mps=2.0, seed=7).step(0.05) for _ in range(50)]
    b = [GustModel(amplitude_mps=2.0, seed=7).step(0.05) for _ in range(50)]
    assert a == b


def test_anchor_holds_under_gusts():
    # The controller should still hold station under a gusty wind.
    m = anchor_metrics(run_scenario(SCENARIOS["anchor_gusty"]))
    assert m.within_radius_pct > 85.0
