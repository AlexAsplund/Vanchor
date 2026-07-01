# `vanchor.analysis`

<a id="vanchor.analysis"></a>

# vanchor.analysis

Simulation analysis framework: run scenarios, record full time series, and
turn them into metrics, terminal reports, CSV, and plots.

Quick start::

    from vanchor.analysis import run_scenario, anchor_metrics, print_report
    from vanchor.analysis.scenarios import SCENARIOS

    log = run_scenario(SCENARIOS["anchor_tight"])
    print_report(log, anchor_metrics(log))

Or from the shell::

    python -m vanchor.analysis anchor_tight --plot out/anchor.png --csv out/anchor.csv


<a id="vanchor.analysis.__main__"></a>

# vanchor.analysis.\_\_main\_\_

CLI: run a named scenario and report on it.

python -m vanchor.analysis --list
python -m vanchor.analysis anchor_tight
python -m vanchor.analysis anchor_tight --csv out/a.csv --plot out/a.png


<a id="vanchor.analysis.metrics"></a>

# vanchor.analysis.metrics

Quantitative metrics computed from a :class:`~vanchor.analysis.runner.SimLog`.

These turn a raw time series into the numbers you actually reason about when
tuning -- overshoot, settling time, steady-state error, how hard the motor is
working, whether it is chattering, etc. Everything operates on *ground truth*
(what the boat really did), not the noisy perceived signal.

<a id="vanchor.analysis.metrics.AnchorMetrics"></a>

## AnchorMetrics Objects

```python
@dataclass
class AnchorMetrics()
```

<a id="vanchor.analysis.metrics.AnchorMetrics.start_distance_m"></a>

#### start\_distance\_m

how far off-station the run began

<a id="vanchor.analysis.metrics.AnchorMetrics.closest_approach_m"></a>

#### closest\_approach\_m

nearest the boat got to the mark

<a id="vanchor.analysis.metrics.AnchorMetrics.overshoot_m"></a>

#### overshoot\_m

peak distance reached AFTER the closest approach

<a id="vanchor.analysis.metrics.AnchorMetrics.settling_time_s"></a>

#### settling\_time\_s

time to enter & stay within tolerance (nan if never)

<a id="vanchor.analysis.metrics.AnchorMetrics.within_radius_pct"></a>

#### within\_radius\_pct

% of post-settle time inside the radius

<a id="vanchor.analysis.metrics.AnchorMetrics.steady_mean_m"></a>

#### steady\_mean\_m

tail-window mean distance to anchor

<a id="vanchor.analysis.metrics.AnchorMetrics.steady_peak_to_peak_m"></a>

#### steady\_peak\_to\_peak\_m

oscillation amplitude in the tail

<a id="vanchor.analysis.metrics.AnchorMetrics.reverse_fraction"></a>

#### reverse\_fraction

fraction of time using reverse thrust

<a id="vanchor.analysis.metrics.AnchorMetrics.thrust_reversals"></a>

#### thrust\_reversals

sign flips (chattering indicator)

<a id="vanchor.analysis.metrics.AnchorMetrics.control_effort"></a>

#### control\_effort

integral of |d(thrust)|, total actuator travel

<a id="vanchor.analysis.metrics.anchor_metrics"></a>

#### anchor\_metrics

```python
def anchor_metrics(log: SimLog,
                   *,
                   tail_seconds: float = 30.0,
                   settle_tolerance_m: float | None = None) -> AnchorMetrics
```

Analyse a station-keeping run from its ground-truth distance to anchor.

<a id="vanchor.analysis.metrics.HeadingMetrics"></a>

## HeadingMetrics Objects

```python
@dataclass
class HeadingMetrics()
```

<a id="vanchor.analysis.metrics.HeadingMetrics.rise_time_s"></a>

#### rise\_time\_s

time to first reach 90% of the heading step

<a id="vanchor.analysis.metrics.HeadingMetrics.overshoot_deg"></a>

#### overshoot\_deg

worst excursion past the target

<a id="vanchor.analysis.metrics.HeadingMetrics.settling_time_s"></a>

#### settling\_time\_s

time to stay within tolerance

<a id="vanchor.analysis.metrics.HeadingMetrics.steady_error_deg"></a>

#### steady\_error\_deg

mean |error| in the tail

<a id="vanchor.analysis.metrics.SteeringActivity"></a>

## SteeringActivity Objects

```python
@dataclass
class SteeringActivity()
```

How hard the steering actuator is being worked -- a jitter/wear proxy.

<a id="vanchor.analysis.metrics.SteeringActivity.max_rate_dps"></a>

#### max\_rate\_dps

peak rotation rate of the steering head

<a id="vanchor.analysis.metrics.SteeringActivity.reversals_per_s"></a>

#### reversals\_per\_s

direction changes per second

<a id="vanchor.analysis.metrics.steering_activity"></a>

#### steering\_activity

```python
def steering_activity(log: SimLog,
                      max_steer_angle_deg: float = 35.0) -> SteeringActivity
```

Rotation rate / reversals of the steering command (``steering`` in [-1,1]
maps to +/-``max_steer_angle_deg`` of head rotation).


<a id="vanchor.analysis.report"></a>

# vanchor.analysis.report

Human- and machine-readable reporting for a :class:`SimLog`.

- ``text_report`` / ``print_report`` -- a terminal summary with ASCII sparklines
  (no dependencies), so you can understand a run at a glance over SSH.
- ``write_csv`` -- the full time series for spreadsheets / pandas.
- ``write_plots`` -- multi-panel PNG charts via matplotlib *if it is installed*
  (optional; the rest works without it).

<a id="vanchor.analysis.report.sparkline"></a>

#### sparkline

```python
def sparkline(values: list[float], width: int = 50) -> str
```

A compact unicode sparkline of a series (NaNs render as spaces).

<a id="vanchor.analysis.report.write_plots"></a>

#### write\_plots

```python
def write_plots(log: SimLog, path: str | Path) -> Path | None
```

Write a multi-panel PNG. Returns None (with a message) if matplotlib is
not installed.


<a id="vanchor.analysis.runner"></a>

# vanchor.analysis.runner

Headless, instrumented simulation runner for analysis.

This is the analysis counterpart to the live server: it wires the *same*
navigator + controller + simulator + simulated devices into a deterministic,
hardware-free closed loop, steps it forward, and records a full time series of
both ground truth and what the controller *perceived* every physics tick.

The result -- a :class:`SimLog` -- is what :mod:`vanchor.analysis.metrics` and
:mod:`vanchor.analysis.report` turn into numbers and pictures. Scenarios are
plain data (start, environment, timed commands, optional gain overrides) so
experiments and tuning sweeps are easy to express and reproduce.

<a id="vanchor.analysis.runner.Command"></a>

## Command Objects

```python
@dataclass(frozen=True)
class Command()
```

A controller/sim command issued at a given simulated time.

<a id="vanchor.analysis.runner.Scenario"></a>

## Scenario Objects

```python
@dataclass
class Scenario()
```

A fully-specified, reproducible simulation experiment.

<a id="vanchor.analysis.runner.Sample"></a>

## Sample Objects

```python
@dataclass
class Sample()
```

One recorded instant of the closed loop.

<a id="vanchor.analysis.runner.SimLog"></a>

## SimLog Objects

```python
class SimLog()
```

The recorded time series of a scenario, with convenience accessors.

<a id="vanchor.analysis.runner.run_scenario"></a>

#### run\_scenario

```python
def run_scenario(scenario: Scenario) -> SimLog
```

Run a scenario deterministically and return its recorded :class:`SimLog`.


<a id="vanchor.analysis.scenarios"></a>

# vanchor.analysis.scenarios

A small library of reproducible, named scenarios for analysis and tuning.

Add one by appending to :data:`SCENARIOS`. Each is a plain :class:`Scenario`,
so a tuning experiment is just a copy with one field changed.

<a id="vanchor.analysis.scenarios.START"></a>

#### START

Lake Vänern default


<a id="vanchor.analysis.tune"></a>

# vanchor.analysis.tune

CLI for the auto-tuner.

python -m vanchor.analysis.tune --list
python -m vanchor.analysis.tune heading
python -m vanchor.analysis.tune anchor --max-evals 120
python -m vanchor.analysis.tune all


<a id="vanchor.analysis.tuning"></a>

# vanchor.analysis.tuning

Auto-assisted PID / gain tuning, built on the analysis framework.

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

<a id="vanchor.analysis.tuning.optimize"></a>

#### optimize

```python
def optimize(cost_fn: Callable[[dict], float],
             params: list[Param],
             *,
             max_evals: int = 80,
             init_step_frac: float = 0.4,
             shrink: float = 0.5,
             min_step_frac: float = 0.01) -> tuple[dict, float, int]
```

Coordinate-descent / pattern search minimising ``cost_fn``.

For each parameter it probes +/- a step; on improvement it moves there, and
when a full sweep yields no improvement it shrinks every step. Deterministic
and dependency-free. Returns ``(best_point, best_cost, n_evals)``.

<a id="vanchor.analysis.tuning.TuningJob"></a>

## TuningJob Objects

```python
@dataclass
class TuningJob()
```

<a id="vanchor.analysis.tuning.TuningJob.evaluate"></a>

#### evaluate

-> (cost, info)

<a id="vanchor.analysis.tuning.TuningJob.config_fields"></a>

#### config\_fields

param name -> "section.field" for the suggested config

