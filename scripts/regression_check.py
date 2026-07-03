#!/usr/bin/env python3
"""Sim-based regression gates (roadmap #39).

Runs a fixed set of key :mod:`vanchor.analysis` scenarios, extracts a handful
of *stability/quality* metrics from each (settling time, overshoot, steady-state
error, control effort, ...), and compares them against a committed baseline JSON
with per-metric tolerances. Exits non-zero if any gated metric drifts outside
tolerance -- so a controller/sim change that quietly makes station-keeping worse
fails CI instead of shipping.

The scenarios run the *same* navigator + controller + Fossen sim the live app
does, entirely in-process and deterministically (every noise source is seeded),
so the numbers are reproducible run-to-run and machine-to-machine.

Usage::

    python scripts/regression_check.py                # gate against baseline
    python scripts/regression_check.py --update       # (re)generate the baseline
    python scripts/regression_check.py -v             # show every metric + delta
    python scripts/regression_check.py anchor_tight   # only these scenarios

The baseline lives at ``src/vanchor/analysis/baselines/regression.json`` and is
committed. Regenerate (and eyeball the diff) whenever an *intended* control/sim
change moves the numbers.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

# Make ``vanchor`` importable when run as a bare script (python scripts/...).
_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from vanchor.analysis.metrics import anchor_metrics, heading_metrics  # noqa: E402
from vanchor.analysis.runner import SimLog, run_scenario  # noqa: E402
from vanchor.analysis.scenarios import SCENARIOS  # noqa: E402

BASELINE_PATH = _REPO / "src" / "vanchor" / "analysis" / "baselines" / "regression.json"


# --------------------------------------------------------------------------- #
# Tolerance model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Tol:
    """A per-metric tolerance: pass iff |actual - base| <= abs_tol + rel*|base|.

    NaN is a first-class value here (e.g. "never settled"): a NaN baseline
    requires a NaN actual and vice versa, so a regression that turns a settling
    time into "never" (or the reverse) is caught.
    """

    abs_tol: float = 0.0
    rel_tol: float = 0.0

    def check(self, base: float, actual: float) -> tuple[bool, float, float]:
        """Return (ok, delta, allowed)."""
        b_nan, a_nan = _isnan(base), _isnan(actual)
        if b_nan or a_nan:
            return (b_nan and a_nan, math.nan, math.nan)
        delta = abs(actual - base)
        allowed = self.abs_tol + self.rel_tol * abs(base)
        return (delta <= allowed, delta, allowed)


def _isnan(x: float) -> bool:
    return isinstance(x, float) and math.isnan(x)


# --------------------------------------------------------------------------- #
# What we gate, per scenario. Only the metrics listed here are enforced; the
# baseline stores exactly these values. Tolerances are deliberately generous
# enough to absorb float rounding across machines, but tight enough to catch a
# real controller regression (a settling time that doubles, an overshoot that
# grows, station-keeping RMS that loosens).
# --------------------------------------------------------------------------- #
_ANCHOR_GATES = {
    "settling_time_s": Tol(abs_tol=4.0, rel_tol=0.20),
    "overshoot_m": Tol(abs_tol=0.30, rel_tol=0.25),
    "steady_rms_m": Tol(abs_tol=0.30, rel_tol=0.25),
    "within_radius_pct": Tol(abs_tol=5.0),
    "control_effort": Tol(abs_tol=2.0, rel_tol=0.30),
}
_HEADING_GATES = {
    "settling_time_s": Tol(abs_tol=4.0, rel_tol=0.20),
    "overshoot_deg": Tol(abs_tol=4.0, rel_tol=0.25),
    "steady_error_deg": Tol(abs_tol=1.5, rel_tol=0.30),
}
_WAYPOINT_GATES = {
    "max_cross_track_m": Tol(abs_tol=2.0, rel_tol=0.25),
    "final_dist_waypoint_m": Tol(abs_tol=3.0, rel_tol=0.30),
}

# The regression set: representative of every guided mode we care about.
GATES: dict[str, dict[str, Tol]] = {
    "anchor_tight": _ANCHOR_GATES,
    "anchor_tight_simple": _ANCHOR_GATES,
    "anchor_drift": _ANCHOR_GATES,
    "anchor_gusty": _ANCHOR_GATES,
    "heading_step": _HEADING_GATES,
    "waypoint_box": _WAYPOINT_GATES,
}


# --------------------------------------------------------------------------- #
# Metric extraction
# --------------------------------------------------------------------------- #
def _has_anchor(log: SimLog) -> bool:
    return any(not math.isnan(s.dist_anchor_truth_m) for s in log.samples)


def _heading_target(scenario) -> tuple[float, float] | None:
    for cmd in scenario.commands:
        if cmd.command.get("type") == "heading_hold" and "heading" in cmd.command:
            return float(cmd.command["heading"]), cmd.t
    return None


def extract_metrics(name: str) -> dict[str, float]:
    """Run a scenario and return just the gated metrics for it."""
    scenario = SCENARIOS[name]
    log = run_scenario(scenario)
    gated = GATES[name]

    if _has_anchor(log):
        m = anchor_metrics(log).to_dict()
        return {k: float(m[k]) for k in gated}

    heading = _heading_target(scenario)
    if heading is not None:
        target, start_t = heading
        m = heading_metrics(log, target, start_t=start_t).to_dict()
        return {k: float(m[k]) for k in gated}

    # Waypoint (no dedicated metrics object): derive tracking quality from the
    # recorded series -- worst cross-track excursion and how close we finished.
    xt = [abs(v) for v in log.series("cross_track_m") if not math.isnan(v)]
    dw = [v for v in log.series("dist_waypoint_m") if not math.isnan(v)]
    return {
        "max_cross_track_m": max(xt) if xt else math.nan,
        "final_dist_waypoint_m": dw[-1] if dw else math.nan,
    }


# --------------------------------------------------------------------------- #
# Baseline IO
# --------------------------------------------------------------------------- #
def _jsonify(x: float) -> float | str:
    """JSON has no NaN; store it as the string 'nan' and round floats."""
    if _isnan(x):
        return "nan"
    return round(float(x), 6)


def _dejsonify(x) -> float:
    if isinstance(x, str) and x.lower() == "nan":
        return math.nan
    return float(x)


def load_baseline() -> dict[str, dict[str, float]]:
    if not BASELINE_PATH.exists():
        raise SystemExit(
            f"no baseline at {BASELINE_PATH}\n"
            f"generate it first:  python {Path(__file__).name} --update"
        )
    raw = json.loads(BASELINE_PATH.read_text())
    scenarios = raw.get("scenarios", raw)
    return {
        name: {k: _dejsonify(v) for k, v in metrics.items()}
        for name, metrics in scenarios.items()
    }


def write_baseline(results: dict[str, dict[str, float]]) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": (
            "Committed sim-regression baseline (scripts/regression_check.py, "
            "roadmap #39). Regenerate with --update after an INTENDED control/sim "
            "change and review the diff. 'nan' = metric undefined (e.g. never "
            "settled). Deterministic: every sim noise source is seeded."
        ),
        "scenarios": {
            name: {k: _jsonify(v) for k, v in metrics.items()}
            for name, metrics in sorted(results.items())
        },
    }
    BASELINE_PATH.write_text(json.dumps(payload, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def run(names: list[str], *, update: bool, verbose: bool) -> int:
    results = {name: extract_metrics(name) for name in names}

    if update:
        # Merge into any existing baseline so updating a subset keeps the rest.
        merged: dict[str, dict[str, float]] = {}
        if BASELINE_PATH.exists():
            merged.update(load_baseline())
        merged.update(results)
        write_baseline(merged)
        print(f"wrote baseline for {len(results)} scenario(s) -> {BASELINE_PATH}")
        for name in names:
            print(f"  {name}: " + ", ".join(f"{k}={v:.4g}" for k, v in results[name].items()))
        return 0

    baseline = load_baseline()
    failures: list[str] = []
    for name in names:
        base = baseline.get(name)
        if base is None:
            failures.append(f"{name}: NOT in baseline (run --update)")
            print(f"FAIL {name}: missing from baseline")
            continue
        line_fail = False
        rows: list[str] = []
        for metric, tol in GATES[name].items():
            actual = results[name][metric]
            expected = base.get(metric, math.nan)
            ok, delta, allowed = tol.check(expected, actual)
            mark = "ok " if ok else "FAIL"
            rows.append(
                f"    {mark} {metric:<22} base={_fmt(expected):>10} "
                f"got={_fmt(actual):>10} d={_fmt(delta):>8} <= {_fmt(allowed):>8}"
            )
            if not ok:
                line_fail = True
                failures.append(
                    f"{name}.{metric}: base={_fmt(expected)} got={_fmt(actual)} "
                    f"(|d|={_fmt(delta)} > {_fmt(allowed)})"
                )
        status = "FAIL" if line_fail else "PASS"
        print(f"[{status}] {name}")
        if verbose or line_fail:
            print("\n".join(rows))

    print()
    if failures:
        print(f"REGRESSION: {len(failures)} metric(s) outside tolerance:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"OK: {len(names)} scenario(s) within tolerance.")
    return 0


def _fmt(x: float) -> str:
    return "nan" if _isnan(x) else f"{x:.4f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "scenarios", nargs="*", help="scenario names to check (default: all gated)"
    )
    parser.add_argument("--update", action="store_true", help="regenerate the baseline")
    parser.add_argument("-v", "--verbose", action="store_true", help="show every metric")
    parser.add_argument("--list", action="store_true", help="list gated scenarios")
    args = parser.parse_args(argv)

    if args.list:
        for name in GATES:
            print(name)
        return 0

    names = args.scenarios or list(GATES)
    unknown = [n for n in names if n not in GATES]
    if unknown:
        raise SystemExit(f"unknown/ungated scenario(s): {', '.join(unknown)}")

    return run(names, update=args.update, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
