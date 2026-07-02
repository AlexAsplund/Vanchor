"""Tests for the auto-assisted PID tuner."""

import pytest

from vanchor.analysis.tuning import (
    Param,
    TUNING_JOBS,
    format_result,
    gains_block_from_tuning,
    optimize,
    tune,
)


def test_optimize_finds_quadratic_minimum():
    params = [Param("x", 0.0, -2.0, 2.0), Param("y", 0.0, -2.0, 2.0)]
    cost = lambda v: (v["x"] - 0.3) ** 2 + (v["y"] - 0.7) ** 2  # noqa: E731
    best, best_cost, evals = optimize(cost, params, max_evals=300)
    assert best["x"] == pytest.approx(0.3, abs=0.1)
    assert best["y"] == pytest.approx(0.7, abs=0.1)
    assert best_cost < 0.02
    assert evals <= 300


def test_optimize_respects_bounds():
    params = [Param("x", 0.0, 0.0, 1.0)]
    # Minimum of this is at x = -5, but bounds clamp to 0.
    best, _, _ = optimize(lambda v: (v["x"] + 5.0) ** 2, params, max_evals=50)
    assert 0.0 <= best["x"] <= 1.0


def test_all_jobs_listed():
    assert set(TUNING_JOBS) == {"heading", "anchor", "cruise", "drift"}


def test_tune_cruise_improves_or_holds():
    r = tune("cruise", max_evals=25)
    assert r.tuned_cost <= r.baseline_cost + 1e-9  # never worse than baseline
    assert {"kp", "ki"} <= set(r.tuned_params)
    assert r.evals >= 1


def test_tune_heading_runs():
    r = tune("heading", max_evals=20)
    assert "heading_kp" in r.tuned_params
    assert r.tuned_cost <= r.baseline_cost + 1e-9


def test_format_result_includes_config_suggestion():
    r = tune("drift", max_evals=12)
    text = format_result(r)
    assert "Auto-tune: drift" in text
    assert "drift_kp" in text and "drift_ki" in text


def test_unknown_job_raises():
    with pytest.raises(ValueError):
        tune("nope")


def test_gains_block_from_tuning_maps_each_job():
    # Every tuning job maps onto exactly the boat-profile gains section it tuned.
    assert set(gains_block_from_tuning("heading", {"heading_kp": 0.04, "heading_kd": 0.01})) == {
        "heading"
    }
    assert set(gains_block_from_tuning("anchor", {"kp": 0.1, "kd": 0.5, "idle_deadband_m": 0.8})) == {
        "anchor"
    }
    assert gains_block_from_tuning("bogus", {}) == {}
