"""Tests for the CI regression gate + sim soak (scripts/regression_check.py,
scripts/soak.py).

These live under ``scripts/`` (not ``tests/``) so they ship with the tools they
cover. Run them explicitly::

    pytest scripts/test_ci_regression.py -q
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from types import SimpleNamespace

# The scripts dir (this file's dir) is on sys.path under pytest's default import
# mode, so the sibling modules import by bare name.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import regression_check as rc  # noqa: E402
import soak as sk  # noqa: E402


# --------------------------------------------------------------------------- #
# regression_check: tolerance model
# --------------------------------------------------------------------------- #
def test_tol_within_and_outside():
    tol = rc.Tol(abs_tol=0.5, rel_tol=0.1)  # allowed = 0.5 + 0.1*|base|
    ok, delta, allowed = tol.check(10.0, 10.4)
    assert ok and math.isclose(delta, 0.4) and math.isclose(allowed, 1.5)
    ok, _, _ = tol.check(10.0, 12.0)  # delta 2.0 > 1.5
    assert not ok


def test_tol_nan_semantics():
    tol = rc.Tol(abs_tol=1.0)
    # nan matches nan (e.g. "never settled" stayed "never settled").
    ok, _, _ = tol.check(math.nan, math.nan)
    assert ok
    # nan vs number is a regression either direction.
    assert not tol.check(math.nan, 5.0)[0]
    assert not tol.check(5.0, math.nan)[0]


def test_json_roundtrip_including_nan():
    assert rc._jsonify(math.nan) == "nan"
    assert math.isnan(rc._dejsonify("nan"))
    assert rc._dejsonify(rc._jsonify(3.14159265)) == 3.141593


# --------------------------------------------------------------------------- #
# regression_check: metric extraction covers each gated scenario type
# --------------------------------------------------------------------------- #
def test_extract_metrics_shapes():
    for name, gates in rc.GATES.items():
        metrics = rc.extract_metrics(name)
        assert set(metrics) == set(gates), name
        for k, v in metrics.items():
            assert isinstance(v, float), (name, k)


def test_committed_baseline_passes():
    """The checked-in baseline must match a fresh run (this is the CI gate)."""
    assert rc.BASELINE_PATH.exists(), "run: python scripts/regression_check.py --update"
    assert rc.main([]) == 0


def test_regression_is_detected(monkeypatch):
    """A loosened baseline value must trip the gate (non-zero exit)."""
    base = rc.load_baseline()
    # Pretend the anchor overshoot baseline was implausibly tight.
    base["anchor_tight"] = dict(base["anchor_tight"])
    base["anchor_tight"]["overshoot_m"] = 0.001
    monkeypatch.setattr(rc, "load_baseline", lambda: base)
    assert rc.main(["anchor_tight"]) == 1


# --------------------------------------------------------------------------- #
# soak: invariant checks (pure, no event loop)
# --------------------------------------------------------------------------- #
def _fake_rt(thrust=0.0, steering=0.0, tasks=None):
    return SimpleNamespace(
        state=SimpleNamespace(
            motor_command=SimpleNamespace(thrust=thrust, steering=steering),
            mode=SimpleNamespace(value="manual"),
        ),
        _tasks=tasks or [],
    )


def test_check_stopped_flags_stuck_motor():
    res = sk.SoakResult(duration_s=1.0)
    sk._check_stopped(_fake_rt(thrust=0.4), res, "unit")
    assert res.violations and "stuck motor" in res.violations[0]

    ok = sk.SoakResult(duration_s=1.0)
    sk._check_stopped(_fake_rt(thrust=0.0), ok, "unit")
    assert ok.ok


def test_track_motor_flags_out_of_range():
    res = sk.SoakResult(duration_s=1.0)
    sk._track_motor(_fake_rt(thrust=1.5, steering=0.2), res)
    assert res.violations
    assert res.max_abs_thrust == 1.5


def test_check_tasks_flags_crashed_task():
    class _Task:
        def done(self):
            return True

        def cancelled(self):
            return False

        def exception(self):
            return RuntimeError("boom")

    res = sk.SoakResult(duration_s=1.0)
    sk._check_tasks(_fake_rt(tasks=[_Task()]), res)
    assert res.violations and "crashed" in res.violations[0]


def test_check_memory_cap():
    res = sk.SoakResult(duration_s=1.0)
    res.rss_baseline_mb, res.rss_peak_mb = 40.0, 300.0
    sk._check_memory(res, cap_growth_mb=120.0)
    assert res.violations and "RSS grew" in res.violations[0]

    ok = sk.SoakResult(duration_s=1.0)
    ok.rss_baseline_mb, ok.rss_peak_mb = 40.0, 45.0
    sk._check_memory(ok, cap_growth_mb=120.0)
    assert ok.ok


# --------------------------------------------------------------------------- #
# soak: a real short end-to-end run must pass all invariants
# --------------------------------------------------------------------------- #
async def test_short_soak_end_to_end():
    result = await sk.soak(
        duration_s=6.0,
        time_scale=10.0,
        churn_period_s=0.5,
        drop_period_s=1.5,
        seed=7,
        verbose=False,
    )
    assert result.ok, result.summary()
    assert result.commands_issued > 0
    assert result.link_drops > 0
    # Every injected drop should have engaged the failsafe (boat was underway).
    assert result.failsafe_engagements == result.link_drops
    assert result.max_abs_thrust <= 1.0 + 1e-6
    assert result.stop_checks > 0
