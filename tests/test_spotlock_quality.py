"""Spot-lock quality metric (#34): rolling RMS radial error + % time within
the anchor radius, mode-agnostic (PID anchor_hold AND learned anchor_ml), fed
each control tick, reset when the mark is cleared/moved, paused otherwise,
surfaced as ``spotlock_quality`` in telemetry."""

from __future__ import annotations

import pytest

from vanchor.controller.controller import SpotLockQuality
from vanchor.core.models import ControlModeName, Environment

from .harness import Harness


# --------------------------------------------------------------------------- #
# Accumulator unit tests
# --------------------------------------------------------------------------- #
def test_first_sample_seeds_accumulators():
    q = SpotLockQuality(window_s=60.0)
    q.update(3.0, 5.0, 0.2)
    assert q.rms_m == pytest.approx(3.0)
    assert q.pct_in_radius == pytest.approx(100.0)


def test_constant_error_converges_to_that_error():
    q = SpotLockQuality(window_s=10.0)
    for _ in range(300):  # 60 s at 5 Hz
        q.update(2.0, 5.0, 0.2)
    assert q.rms_m == pytest.approx(2.0, abs=1e-6)
    assert q.pct_in_radius == pytest.approx(100.0)


def test_leaving_the_radius_moves_both_metrics():
    q = SpotLockQuality(window_s=10.0)
    for _ in range(300):
        q.update(2.0, 5.0, 0.2)
    for _ in range(300):  # then 60 s at 8 m (outside a 5 m radius)
        q.update(8.0, 5.0, 0.2)
    assert q.rms_m == pytest.approx(8.0, abs=0.05)
    assert q.pct_in_radius < 1.0


def test_mixed_occupancy_tracks_fraction():
    q = SpotLockQuality(window_s=10.0)
    for i in range(600):  # alternate in/out -> ~50% occupancy
        q.update(4.0 if i % 2 == 0 else 6.0, 5.0, 0.2)
    assert q.pct_in_radius == pytest.approx(50.0, abs=2.0)


def test_reset_clears_everything():
    q = SpotLockQuality()
    q.update(4.0, 5.0, 0.2)
    q.reset()
    assert q.rms_m == 0.0
    assert q.pct_in_radius == 0.0
    assert q.elapsed_s == 0.0


def test_zero_dt_ignored():
    q = SpotLockQuality()
    q.update(4.0, 5.0, 0.0)
    assert q.elapsed_s == 0.0 and q.rms_m == 0.0


# --------------------------------------------------------------------------- #
# Controller integration
# --------------------------------------------------------------------------- #
def test_anchor_hold_populates_quality_metric():
    h = Harness(model="fossen")
    h.command({"type": "anchor_hold", "radius_m": 5.0})
    h.run(seconds=90)
    st = h.state
    assert st.spotlock_holding_s > 0.0
    assert st.spotlock_pct_in_radius > 80.0    # calm water: holds easily
    assert 0.0 <= st.spotlock_rms_m < 5.0


def test_anchor_ml_populates_quality_metric_too():
    """Mode-agnostic: the learned hold feeds the SAME tracker, so PID vs ML
    holds are directly comparable."""
    env = Environment(wind_speed=3.0, wind_dir=45.0)
    h = Harness(model="fossen", environment=env)
    h.command({"type": "anchor_ml", "radius_m": 6.0})
    h.run(seconds=120)
    st = h.state
    assert st.mode == ControlModeName.ANCHOR_ML
    assert st.spotlock_holding_s > 0.0
    assert st.spotlock_pct_in_radius > 70.0
    assert st.spotlock_rms_m < 6.0


def test_metric_pauses_out_of_anchor_modes_and_resets_when_cleared():
    h = Harness(model="fossen")
    h.command({"type": "anchor_hold", "radius_m": 5.0})
    h.run(seconds=30)
    rms = h.state.spotlock_rms_m
    assert h.state.spotlock_holding_s > 0.0
    # Manual mode with the anchor still set: PAUSED (numbers frozen, the stale
    # distance_to_anchor_m must not pollute the metric).
    h.command({"type": "stop"})
    held = h.state.spotlock_holding_s
    h.run(seconds=10)
    assert h.state.spotlock_rms_m == rms
    assert h.state.spotlock_holding_s == held
    # Clearing the mark resets the metric to zero.
    h.state.anchor = None
    h.run(seconds=2)
    assert h.state.spotlock_rms_m == 0.0
    assert h.state.spotlock_pct_in_radius == 0.0
    assert h.state.spotlock_holding_s == 0.0


def test_new_mark_restarts_the_measurement():
    h = Harness(model="fossen")
    h.command({"type": "anchor_hold", "radius_m": 5.0})
    h.run(seconds=60)
    before = h.state.spotlock_holding_s
    assert before > 30.0
    # Re-drop at a different mark -> a fresh measurement window.
    h.command({"type": "anchor_hold", "radius_m": 5.0,
               "anchor": {"lat": h.state.anchor.lat + 3e-5,
                          "lon": h.state.anchor.lon}})
    h.run(seconds=5)
    assert 0.0 < h.state.spotlock_holding_s < before


def test_quality_surfaced_in_telemetry_dict():
    h = Harness(model="fossen")
    h.command({"type": "anchor_hold", "radius_m": 5.0})
    h.run(seconds=20)
    q = h.state.to_dict()["spotlock_quality"]
    assert set(q) == {"rms_m", "pct_in_radius", "window_s", "holding_s"}
    assert q["window_s"] == 60.0
    assert 0.0 <= q["pct_in_radius"] <= 100.0
    assert q["holding_s"] > 0.0
