"""Auto-assisted PID / gain tuning, built on the analysis framework.

The idea: a control loop's quality is already measurable (``metrics.py``) and
reproducible (``runner.py``). So tuning is just *search* over the gains to
minimise a cost built from those metrics. This module provides:

  * :func:`optimize` -- a small, dependency-free coordinate-descent / pattern
    search (no scipy/numpy needed),
  * a set of :class:`TuningJob`s (heading, anchor, cruise, drift) that each know
    how to apply candidate gains to a scenario and score the result,
  * :func:`tune` + :func:`format_result` to run one and report it.

It is *auto-assisted*, not magic: it proposes gains and shows the before/after
metrics and a ready-to-paste config snippet; a human decides whether to adopt.
"""

from __future__ import annotations

import dataclasses
import math
import statistics
from dataclasses import dataclass, field
from typing import Callable

from ..controller.modes import AnchorConfig, DriftConfig
from ..core.models import Environment, GeoPoint
from ..core.pid import PID
from .metrics import anchor_metrics, heading_metrics
from .runner import Command, Scenario, run_scenario

START = GeoPoint(59.66275, 13.32247)


# --------------------------------------------------------------------------- #
# Optimizer
# --------------------------------------------------------------------------- #
@dataclass
class Param:
    name: str
    init: float
    low: float
    high: float


def optimize(
    cost_fn: Callable[[dict], float],
    params: list[Param],
    *,
    max_evals: int = 80,
    init_step_frac: float = 0.4,
    shrink: float = 0.5,
    min_step_frac: float = 0.01,
) -> tuple[dict, float, int]:
    """Coordinate-descent / pattern search minimising ``cost_fn``.

    For each parameter it probes +/- a step; on improvement it moves there, and
    when a full sweep yields no improvement it shrinks every step. Deterministic
    and dependency-free. Returns ``(best_point, best_cost, n_evals)``.
    """
    x = {p.name: p.init for p in params}
    step = {p.name: (p.high - p.low) * init_step_frac for p in params}
    cache: dict[tuple, float] = {}

    def evaluate(point: dict) -> float:
        key = tuple(round(point[p.name], 6) for p in params)
        if key not in cache:
            cache[key] = cost_fn(point)
        return cache[key]

    best = evaluate(x)
    evals = 1
    while evals < max_evals:
        improved = False
        for p in params:
            for direction in (1.0, -1.0):
                if evals >= max_evals:
                    break
                cand = dict(x)
                cand[p.name] = min(p.high, max(p.low, x[p.name] + direction * step[p.name]))
                if abs(cand[p.name] - x[p.name]) < 1e-12:
                    continue
                c = evaluate(cand)
                evals += 1
                if c < best:
                    x, best = cand, c
                    improved = True
                    break
        if not improved:
            for p in params:
                step[p.name] *= shrink
            if all(step[p.name] <= (p.high - p.low) * min_step_frac for p in params):
                break
    return x, best, evals


# --------------------------------------------------------------------------- #
# Tuning jobs
# --------------------------------------------------------------------------- #
@dataclass
class TuningJob:
    name: str
    description: str
    params: list[Param]
    baseline: dict
    evaluate: Callable[[dict], tuple[float, dict]]  # -> (cost, info)
    config_fields: dict  # param name -> "section.field" for the suggested config


@dataclass
class TuningResult:
    job: str
    baseline_params: dict
    baseline_cost: float
    baseline_info: dict
    tuned_params: dict
    tuned_cost: float
    tuned_info: dict
    evals: int
    config_fields: dict


_BIG = 1e6


def _heading_job() -> TuningJob:
    def evaluate(v: dict) -> tuple[float, dict]:
        pid = PID(kp=v["heading_kp"], ki=0.0, kd=v["heading_kd"], output_min=-1.0, output_max=1.0)
        sc = Scenario(
            name="tune_heading",
            model="fossen",
            duration_s=45.0,
            commands=[Command(2.0, {"type": "heading_hold", "heading": 90.0, "throttle": 0.5})],
            helm_pid=pid,
        )
        m = heading_metrics(run_scenario(sc), 90.0, start_t=2.0)
        settle = m.settling_time_s if math.isfinite(m.settling_time_s) else 60.0
        cost = settle + 3.0 * m.overshoot_deg + 8.0 * m.steady_error_deg
        return cost, {
            "settling_s": round(settle, 1),
            "overshoot_deg": round(m.overshoot_deg, 1),
            "steady_err_deg": round(m.steady_error_deg, 2),
        }

    return TuningJob(
        name="heading",
        description="Helm heading-hold PID (kp, kd)",
        params=[Param("heading_kp", 0.035, 0.005, 0.08), Param("heading_kd", 0.012, 0.0, 0.06)],
        baseline={"heading_kp": 0.035, "heading_kd": 0.012},
        evaluate=evaluate,
        config_fields={"heading_kp": "control.heading_kp", "heading_kd": "control.heading_kd"},
    )


def _anchor_scenarios() -> list[Scenario]:
    from ..core.geo import destination_point

    tight = Scenario(
        name="tune_anchor_tight",
        model="fossen",
        duration_s=80.0,
        environment=Environment(),
        commands=[
            Command(2.0, {"type": "anchor_hold", "radius_m": 2.0}),
            Command(
                3.0,
                {
                    "type": "teleport",
                    "lat": destination_point(START, 10.0, 45.0).lat,
                    "lon": destination_point(START, 10.0, 45.0).lon,
                },
            ),
        ],
    )
    drift = Scenario(
        name="tune_anchor_drift",
        model="fossen",
        duration_s=90.0,
        environment=Environment(current_speed=0.3, current_dir=90.0, wind_speed=5.0, wind_dir=120.0),
        commands=[Command(2.0, {"type": "anchor_hold", "radius_m": 5.0})],
    )
    return [tight, drift]


def _anchor_job() -> TuningJob:
    scenarios = _anchor_scenarios()

    def evaluate(v: dict) -> tuple[float, dict]:
        cfg = AnchorConfig(kp=v["kp"], kd=v["kd"], idle_deadband_m=v["idle_deadband_m"])
        total = 0.0
        within = []
        for sc in scenarios:
            m = anchor_metrics(run_scenario(dataclasses.replace(sc, anchor_config=cfg)))
            within.append(m.within_radius_pct)
            total += (
                (100.0 - m.within_radius_pct) * 0.3
                + m.steady_rms_m * 2.0
                + m.overshoot_m * 0.3
                + m.steady_peak_to_peak_m
            )
        return total, {"within_pct": [round(w) for w in within]}

    return TuningJob(
        name="anchor",
        description="Anchor hold gains (kp, kd, idle_deadband_m)",
        params=[
            Param("kp", 0.12, 0.04, 0.30),
            Param("kd", 0.6, 0.0, 1.5),
            Param("idle_deadband_m", 0.8, 0.0, 2.0),
        ],
        baseline={"kp": 0.12, "kd": 0.6, "idle_deadband_m": 0.8},
        evaluate=evaluate,
        config_fields={
            "kp": "control.anchor_kp",
            "kd": "control.anchor_kd",
            "idle_deadband_m": "control.anchor_idle_deadband_m",
        },
    )


def _speed_hold_cost(log, target: float, on_from: float) -> tuple[float, dict]:
    """Shared scorer for cruise/drift: steady SOG error + overshoot + ripple."""
    after = [s for s in log.samples if s.t > on_from]
    tail = [s.sog_knots for s in log.samples if s.t > log.samples[-1].t - 10.0]
    if not after or not tail:
        return _BIG, {}
    steady = statistics.mean(tail)
    err = abs(steady - target)
    overshoot = max(0.0, max(s.sog_knots for s in after) - target)
    ripple = statistics.pstdev(tail)
    cost = err * 10.0 + overshoot * 4.0 + ripple * 3.0
    return cost, {"steady_kn": round(steady, 2), "overshoot_kn": round(overshoot, 2)}


def _cruise_job() -> TuningJob:
    target = 2.0

    def evaluate(v: dict) -> tuple[float, dict]:
        pid = PID(kp=v["kp"], ki=v["ki"], kd=0.0, output_min=0.0, output_max=1.0)
        sc = Scenario(
            name="tune_cruise",
            model="fossen",
            duration_s=45.0,
            environment=Environment(current_speed=0.2, current_dir=0.0),
            commands=[
                Command(2.0, {"type": "heading_hold", "heading": 90.0, "throttle": 0.5}),
                Command(3.0, {"type": "cruise", "knots": target}),
            ],
            cruise_pid=pid,
        )
        return _speed_hold_cost(run_scenario(sc), target, on_from=3.0)

    return TuningJob(
        name="cruise",
        description="Cruise Control SOG PID (kp, ki)",
        params=[Param("kp", 0.64, 0.1, 1.5), Param("ki", 0.25, 0.0, 1.0)],
        baseline={"kp": 0.64, "ki": 0.25},
        evaluate=evaluate,
        config_fields={"kp": "control.cruise_kp", "ki": "control.cruise_ki"},
    )


def _drift_job() -> TuningJob:
    target = 0.6

    def evaluate(v: dict) -> tuple[float, dict]:
        sc = Scenario(
            name="tune_drift",
            model="fossen",
            duration_s=50.0,
            environment=Environment(wind_speed=5.0, wind_dir=90.0),
            commands=[Command(2.0, {"type": "drift", "heading": 90.0, "knots": target})],
            drift_config=DriftConfig(kp=v["kp"], ki=v["ki"]),
        )
        return _speed_hold_cost(run_scenario(sc), target, on_from=2.0)

    return TuningJob(
        name="drift",
        description="Drift mode SOG PID (kp, ki)",
        params=[Param("kp", 0.5, 0.1, 1.5), Param("ki", 0.25, 0.0, 1.0)],
        baseline={"kp": 0.5, "ki": 0.25},
        evaluate=evaluate,
        config_fields={"kp": "control.drift_kp", "ki": "control.drift_ki"},
    )


TUNING_JOBS: dict[str, Callable[[], TuningJob]] = {
    "heading": _heading_job,
    "anchor": _anchor_job,
    "cruise": _cruise_job,
    "drift": _drift_job,
}


def tune(job_name: str, *, max_evals: int = 80) -> TuningResult:
    if job_name not in TUNING_JOBS:
        raise ValueError(f"unknown tuning job {job_name!r}; try {list(TUNING_JOBS)}")
    job = TUNING_JOBS[job_name]()

    base_cost, base_info = job.evaluate(job.baseline)
    best, best_cost, evals = optimize(
        lambda v: job.evaluate(v)[0], job.params, max_evals=max_evals
    )
    tuned_cost, tuned_info = job.evaluate(best)
    return TuningResult(
        job=job.name,
        baseline_params=job.baseline,
        baseline_cost=base_cost,
        baseline_info=base_info,
        tuned_params=best,
        tuned_cost=tuned_cost,
        tuned_info=tuned_info,
        evals=evals,
        config_fields=job.config_fields,
    )


def format_result(r: TuningResult) -> str:
    lines = [f"=== Auto-tune: {r.job} ===", f"evaluations: {r.evals}"]
    improve = (
        100.0 * (r.baseline_cost - r.tuned_cost) / r.baseline_cost
        if r.baseline_cost
        else 0.0
    )
    lines.append("")
    lines.append(f"  {'param':<18}{'baseline':>12}{'tuned':>12}")
    for name in r.baseline_params:
        lines.append(
            f"  {name:<18}{r.baseline_params[name]:>12.4f}{r.tuned_params[name]:>12.4f}"
        )
    lines.append("")
    lines.append(f"  cost   baseline {r.baseline_cost:.3f}  ->  tuned {r.tuned_cost:.3f}  ({improve:+.0f}%)")
    lines.append(f"  metrics baseline {r.baseline_info}")
    lines.append(f"  metrics tuned    {r.tuned_info}")
    lines.append("")
    lines.append("  suggested config (vanchor.example.yaml):")
    for name, field_path in r.config_fields.items():
        section, key = field_path.split(".")
        lines.append(f"    {section}: {{ {key}: {r.tuned_params[name]:.4f} }}")
    return "\n".join(lines)
