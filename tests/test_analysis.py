"""Tests for the simulation analysis framework."""

import math

import pytest

from vanchor.analysis import (
    anchor_metrics,
    channel_stats,
    heading_metrics,
    run_scenario,
    sparkline,
    text_report,
    write_csv,
)
from vanchor.analysis.scenarios import SCENARIOS
from vanchor.controller.modes import AnchorConfig
import dataclasses


def test_run_scenario_records_full_series():
    log = run_scenario(SCENARIOS["anchor_tight"])
    assert len(log) > 1000
    s = log.samples[0]
    # Ground truth and perceived channels are both present.
    assert hasattr(s, "truth_lat") and hasattr(s, "perc_lat")
    assert not math.isnan(log.series("truth_lat")[0])


def test_anchor_metrics_reasonable():
    log = run_scenario(SCENARIOS["anchor_tight"])
    m = anchor_metrics(log)
    assert m.radius_m == 2.0
    # The tuned controller keeps the boat within (or very near) the radius and
    # actually uses reverse thrust.
    assert m.within_radius_pct > 90.0
    assert m.reverse_fraction > 0.0
    assert m.steady_mean_m < m.radius_m


def test_anchor_holds_station_and_uses_reverse():
    """Regression guard: the tuned anchor controller holds the boat near the
    mark and actively uses reverse thrust (rather than orbiting forward-only)."""
    m = anchor_metrics(run_scenario(SCENARIOS["anchor_tight"]))
    assert m.within_radius_pct >= 90.0
    assert m.reverse_fraction > 0.0
    assert m.steady_mean_m < m.radius_m


def test_heading_metrics():
    log = run_scenario(SCENARIOS["heading_step"])
    m = heading_metrics(log, 90.0, start_t=2.0)
    assert m.steady_error_deg < 5.0
    assert m.settling_time_s < 40.0


def test_sparkline_and_report_and_csv(tmp_path):
    log = run_scenario(SCENARIOS["heading_step"])
    spark = sparkline(log.series("truth_heading"))
    assert isinstance(spark, str) and len(spark) > 0
    report = text_report(log)
    assert "Simulation analysis" in report
    stats = channel_stats(log)
    assert any(c.name == "thrust" for c in stats)
    out = write_csv(log, tmp_path / "run.csv")
    assert out.exists()
    assert out.read_text().count("\n") > 100  # header + many rows


def test_steering_is_physically_realisable():
    """Steering must stay within the head's rotation speed and not be a
    high-frequency jitter (slew limit + low-pass)."""
    from vanchor.analysis import steering_activity

    for name in ("heading_step", "anchor_drift"):
        act = steering_activity(run_scenario(SCENARIOS[name]))
        # Mean rate is the meaningful jitter figure (was ~26-62 dps before the
        # low-pass + slew limit; the rare per-sample peak is a timing artifact).
        assert act.mean_rate_dps < 20.0
        assert act.max_rate_dps < 80.0  # vs ~334 dps unbounded before


def test_anchor_metrics_raises_without_anchor():
    # A heading scenario never sets an anchor.
    log = run_scenario(SCENARIOS["heading_step"])
    with pytest.raises(ValueError):
        anchor_metrics(log)
