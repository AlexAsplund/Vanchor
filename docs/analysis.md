# Simulation analysis framework

`vanchor.analysis` runs the **same** navigator + controller + simulator + sensors
as the live server, but headless and deterministic, recording a full time series
of both *ground truth* and what the controller *perceived*. It turns "the boat
feels like it's hunting" into numbers you can tune against.

## Quick start

```bash
# list built-in scenarios
python -m vanchor.analysis --list

# run one: prints a terminal report with sparklines
python -m vanchor.analysis anchor_tight

# also write the full series to CSV and a multi-panel PNG
python -m vanchor.analysis anchor_tight --csv out/a.csv --plot out/a.png
```

From Python (for sweeps / experiments):

```python
import dataclasses
from vanchor.analysis import run_scenario, anchor_metrics
from vanchor.analysis.scenarios import SCENARIOS
from vanchor.controller.modes import AnchorConfig

base = SCENARIOS["anchor_tight"]
for kd in (0.4, 0.6, 0.9):
    log = run_scenario(dataclasses.replace(base, anchor_config=AnchorConfig(kd=kd)))
    print(kd, anchor_metrics(log).overshoot_m)
```

## Pieces

- `runner.py` — `Scenario` (start, environment, model, timed `Command`s, optional
  gain overrides) → `run_scenario()` → `SimLog` of `Sample`s. Every physics tick
  records truth lat/lon/heading/speed, perceived lat/lon/heading, sog, the motor
  command (+ azimuth), distance-to-anchor (truth *and* perceived), cross-track,
  etc.
- `metrics.py` — `anchor_metrics` (overshoot, settling time, % within radius,
  steady mean/RMS/peak-to-peak, thrust usage, reversals, control effort),
  `heading_metrics` (rise/overshoot/settling/steady-error), `channel_stats`.
- `report.py` — `print_report` (with unicode sparklines), `write_csv`,
  `write_plots` (matplotlib, optional).
- `scenarios.py` — named, reproducible scenarios (`anchor_tight`,
  `anchor_drift`, `heading_step`, `waypoint_box`, ...).

Everything is deterministic (seeded sensor noise), so a result is reproducible
and a tuning sweep is a one-field change.

## Worked example: the anchor "overshoot"

Reported symptom: on a tight radius the anchor hold oscillated/overshot. Running
`anchor_tight` (2 m radius, calm, 1.5 m GPS noise) and reading the metrics showed
the real story:

1. **It wasn't momentum overshoot.** Increasing the braking gain `kd` made it
   *worse*, not better — so the energy wasn't coming from carrying speed.
2. **It was GPS-noise hunting.** `target_heading` swung 0–359° while the boat sat
   ~1 m from the mark: with 1.5 m noise on a 2 m radius, the controller was
   chasing the (very noisy) *bearing* to a nearby mark. Re-running with 0.5 m GPS
   noise made the hunting vanish — confirming the controller was sound and the
   signal was the problem.
3. **Filtering the position hurt** (a low-pass adds lag a control loop fights).

## Auto-assisted tuning

Because quality is measurable (metrics) and reproducible (scenarios), tuning is
just *search* over the gains. `vanchor.analysis.tuning` provides a dependency-free
coordinate-descent optimiser and tuning **jobs** that each apply candidate gains
to a scenario and score the result:

```bash
python -m vanchor.analysis.tune --list
python -m vanchor.analysis.tune heading      # tune the Helm heading PID
python -m vanchor.analysis.tune all          # heading, anchor, cruise, drift
```

Each job runs in well under a second and prints baseline vs. tuned gains, the
before/after metrics, and a ready-to-paste config snippet. It is *auto-assisted*:
it proposes gains and shows the evidence; a human decides whether to adopt. From
Python: `from vanchor.analysis import tune, format_result;
print(format_result(tune("anchor", max_evals=120)))`. Cost functions live in
`tuning.py` (e.g. heading = settling + 3·overshoot + 8·steady-error; anchor =
(100−within%)·0.3 + 2·rms + 0.3·overshoot + peak-to-peak, averaged over a tight
and a drift scenario). To tune something new, add a `TuningJob` with its params,
a scenario, and a cost built from metrics.

The fix (see `controller/modes.py:AnchorHoldMode`): a **graduated response** —
*recover* (re-point the bow at the mark, with reverse + velocity braking) only
when pushed clearly outside the radius; otherwise *station-keep* by holding the
**current** heading and trimming position fore/aft (never re-pointing at the
noisy bearing); and *idle* within an `idle_deadband_m` (0.8 m) band. Result on
`anchor_tight`: ~94% time within the 2 m radius, **mean 0.95 m**, and steady yaw
activity ~2 °/s (a spinning boat would be 10–25 °/s) — no orbit, no spin, and the
servo isn't worked at idle.
