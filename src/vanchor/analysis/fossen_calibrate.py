"""Recorded-truth auto-calibration of the Fossen boat model (roadmap #46).

The :mod:`vanchor.sim.fossen` model has a couple dozen physical constants
(:class:`~vanchor.sim.fossen.FossenParams`) hand-tuned to a nominal 4.1 m skiff.
A *different* real boat -- heavier, beamier, a stronger motor -- will not match
those defaults, and every sim-based tool (the analysis harness, the auto-tuner,
the ML anchor training) is only as trustworthy as the sim's fidelity to the
boat it is standing in for.

This module closes that gap **offline**: given a recording of a real run --
a time series of ``(motor command, GPS/compass truth)`` samples logged on the
water -- it fits a chosen subset of ``FossenParams`` by least squares so that
re-simulating the same command sequence reproduces the recorded motion. The
result is a calibrated ``FossenParams`` for that specific hull.

Method
------
For a candidate parameter vector we build a fresh :class:`FossenBoat`, seed it
with the recording's initial position/heading/speed, replay the recorded command
stream, and compare the simulated trajectory to the recorded one. The residual
stacks, per step, the errors in **speed**, **heading**, and **position**; SciPy's
:func:`scipy.optimize.least_squares` (bounded trust-region) drives them to zero.

Because the simulator is deterministic, feeding it truth generated *by the model
itself* recovers the generating parameters (see the unit test) -- which both
validates the fitter and is the honest ceiling on what it can do: real data has
noise and unmodelled physics, so on real logs expect a best-fit, not a perfect
one. Only numpy + scipy; no I/O, no sim stepping beyond the model under test.

Identifiability note
--------------------
Not every parameter is observable from every run. A straight full-throttle run
identifies the surge terms (top speed, thrust scale); it says nothing about yaw
damping. Calibrate the parameters your maneuver actually excites -- accelerate in
a straight line *and* hold a hard turn -- and fit that subset (``param_names``).
"""

from __future__ import annotations

import math
from dataclasses import MISSING, dataclass, field, fields

import numpy as np
from scipy.optimize import least_squares

from ..core.geo import EARTH_RADIUS_M, angle_difference
from ..core.models import Environment, GeoPoint, MotorCommand
from ..sim.fossen import FossenBoat, FossenParams


@dataclass(frozen=True)
class RecordedSample:
    """One logged instant of a real run.

    ``thrust`` / ``steering`` are the normalized motor command that was *held*
    from this sample until the next (piecewise-constant, as the motor driver
    sees it). The remaining fields are the measured ground truth -- what a GPS +
    compass reports: position, heading (deg), and speed over ground (m/s).
    """

    t: float
    thrust: float
    steering: float
    point: GeoPoint
    heading_deg: float
    speed_mps: float


@dataclass
class Recording:
    """A full logged run: ordered samples plus the environment they ran in.

    The environment (wind/current) is replayed into the sim during calibration
    so the fit is not corrupted by attributing drift to the wrong cause. If you
    logged in flat calm, the default :class:`Environment` is correct.
    """

    samples: list[RecordedSample]
    environment: Environment = field(default_factory=Environment)

    def __post_init__(self) -> None:
        if len(self.samples) < 2:
            raise ValueError("a Recording needs at least two samples to fit")


@dataclass
class CalibrationResult:
    """Outcome of a fit."""

    param_names: list[str]
    initial: dict[str, float]  # starting guess (raw, pre-__post_init__)
    fitted: dict[str, float]  # best-fit values (raw)
    cost: float  # 0.5 * sum(residual^2) at the solution
    residual_rms: float  # RMS residual (mixed units; ~0 == perfect replay)
    nfev: int
    success: bool

    def params(self, base: dict[str, float] | None = None) -> FossenParams:
        """Build the calibrated :class:`FossenParams` from the fit.

        ``base`` supplies the non-fitted fields (defaults to the model defaults).
        """
        merged = default_param_base() if base is None else dict(base)
        merged.update(self.fitted)
        return FossenParams(**merged)


def default_param_base() -> dict[str, float]:
    """The model's *raw* default parameter values (before ``__post_init__``).

    ``FossenParams.__post_init__`` multiplies several damping fields in place by a
    hull-slenderness factor, so reading them back off a constructed instance and
    re-feeding them would double-apply that scaling. We therefore always compose
    parameters from the dataclass field defaults (the raw, as-authored values)
    and only override the fitted names -- never round-trip through an instance.
    """
    base: dict[str, float] = {}
    for f in fields(FossenParams):
        if f.default is not MISSING:
            base[f.name] = f.default
    return base


def _local_en(origin: GeoPoint, p: GeoPoint) -> tuple[float, float]:
    """Equirectangular east/north offset (m) of ``p`` from ``origin``."""
    dn = math.radians(p.lat - origin.lat) * EARTH_RADIUS_M
    de = (
        math.radians(p.lon - origin.lon)
        * EARTH_RADIUS_M
        * math.cos(math.radians(origin.lat))
    )
    return de, dn


def _default_bounds(x0: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Sign-aware default bounds spanning ~0.2x .. 5x each initial value.

    Works for the model's negative damping coefficients as well as positive
    scales. A zero initial value (nothing to scale) gets a small symmetric band.
    """
    lo = np.empty_like(x0)
    hi = np.empty_like(x0)
    for i, v in enumerate(x0):
        if v == 0.0:
            lo[i], hi[i] = -1.0, 1.0
        else:
            a, b = 0.2 * v, 5.0 * v
            lo[i], hi[i] = min(a, b), max(a, b)
    return lo, hi


def simulate_recording(
    params: FossenParams,
    commands: list[tuple[float, float, float]],
    *,
    duration_s: float,
    dt: float = 0.1,
    start: GeoPoint = GeoPoint(59.66275, 13.32247),
    start_heading_deg: float = 0.0,
    environment: Environment | None = None,
) -> Recording:
    """Produce a synthetic :class:`Recording` by running the Fossen model.

    ``commands`` is a piecewise-constant schedule of ``(t_start, thrust,
    steering)`` segments, sorted by ``t_start``; the active segment at each step
    is the last one whose ``t_start <= t``. This is the tool used to make
    "recorded truth" for tests and to sanity-check a calibration against a known
    boat, but it is equally the shape a real logger would emit.
    """
    from ..core.models import BoatState

    env = environment if environment is not None else Environment()
    schedule = sorted(commands, key=lambda c: c[0])
    boat = FossenBoat(
        BoatState(point=start, heading_deg=start_heading_deg), params
    )

    def cmd_at(t: float) -> tuple[float, float]:
        thrust, steering = 0.0, 0.0
        for t0, th, st in schedule:
            if t0 <= t + 1e-9:
                thrust, steering = th, st
            else:
                break
        return thrust, steering

    samples: list[RecordedSample] = []
    n = int(round(duration_s / dt))
    for i in range(n + 1):
        t = i * dt
        s = boat.state
        thrust, steering = cmd_at(t)
        samples.append(
            RecordedSample(
                t=t,
                thrust=thrust,
                steering=steering,
                point=s.point,
                heading_deg=s.heading_deg,
                speed_mps=math.hypot(s.ground_ve, s.ground_vn),
            )
        )
        if i < n:
            boat.step(dt, MotorCommand(thrust, steering), env)
    return Recording(samples=samples, environment=env)


def _residuals(
    x: np.ndarray,
    param_names: list[str],
    base: dict[str, float],
    rec: Recording,
    *,
    w_speed: float,
    w_heading: float,
    w_position: float,
) -> np.ndarray:
    """Stacked per-step (speed, heading, east, north) errors for candidate ``x``."""
    from ..core.models import BoatState

    merged = dict(base)
    for name, val in zip(param_names, x):
        merged[name] = float(val)
    params = FossenParams(**merged)

    samples = rec.samples
    s0 = samples[0]
    boat = FossenBoat(
        BoatState(point=s0.point, heading_deg=s0.heading_deg), params
    )
    # Seed the initial body velocity from the first sample so the replay starts
    # from the recorded motion, not from rest (surge = measured speed).
    boat._nu[0] = s0.speed_mps

    origin = s0.point
    res: list[float] = []
    for i in range(len(samples) - 1):
        cur, nxt = samples[i], samples[i + 1]
        step_dt = nxt.t - cur.t
        boat.step(step_dt, MotorCommand(cur.thrust, cur.steering), rec.environment)
        pred = boat.state
        pred_speed = math.hypot(pred.ground_ve, pred.ground_vn)
        res.append(w_speed * (pred_speed - nxt.speed_mps))
        res.append(
            w_heading
            * math.radians(angle_difference(nxt.heading_deg, pred.heading_deg))
        )
        pe, pn = _local_en(origin, pred.point)
        oe, on = _local_en(origin, nxt.point)
        res.append(w_position * (pe - oe))
        res.append(w_position * (pn - on))
    return np.asarray(res, dtype=float)


def calibrate_fossen(
    recording: Recording,
    param_names: list[str],
    *,
    base: dict[str, float] | None = None,
    bounds: tuple[np.ndarray, np.ndarray] | None = None,
    w_speed: float = 1.0,
    w_heading: float = 1.0,
    w_position: float = 0.05,
    max_nfev: int = 300,
) -> CalibrationResult:
    """Fit ``param_names`` of :class:`FossenParams` to a recorded run.

    Parameters
    ----------
    recording:
        The logged ``(command, truth)`` time series to match.
    param_names:
        Which :class:`FossenParams` fields to fit (e.g.
        ``["max_thrust_n", "max_speed_mps", "n_r"]``). Only the parameters your
        maneuver excites are identifiable -- see the module docstring.
    base:
        Raw values for the non-fitted fields (defaults to the model defaults via
        :func:`default_param_base`). Also supplies the initial guess for the
        fitted fields.
    bounds:
        ``(lower, upper)`` arrays aligned to ``param_names``. Defaults to a
        sign-aware ~0.2x..5x band around the initial guess.
    w_speed, w_heading, w_position:
        Residual weights (speed in m/s, heading in rad, position in m). The
        defaults balance the three so no single channel dominates.

    Returns
    -------
    CalibrationResult
        The fitted values plus fit diagnostics. ``result.params()`` builds the
        calibrated :class:`FossenParams`.
    """
    if not param_names:
        raise ValueError("param_names must name at least one FossenParams field")
    base = default_param_base() if base is None else dict(base)
    valid = {f.name for f in fields(FossenParams)}
    unknown = [n for n in param_names if n not in valid]
    if unknown:
        raise ValueError(f"not FossenParams fields: {unknown}")

    x0 = np.array([base[n] for n in param_names], dtype=float)
    lo, hi = _default_bounds(x0) if bounds is None else bounds
    lo = np.asarray(lo, dtype=float)
    hi = np.asarray(hi, dtype=float)
    x0 = np.clip(x0, lo, hi)

    sol = least_squares(
        _residuals,
        x0,
        bounds=(lo, hi),
        args=(param_names, base, recording),
        kwargs=dict(w_speed=w_speed, w_heading=w_heading, w_position=w_position),
        x_scale="jac",
        max_nfev=max_nfev,
    )
    fitted = {n: float(v) for n, v in zip(param_names, sol.x)}
    n_res = max(1, sol.fun.size)
    return CalibrationResult(
        param_names=list(param_names),
        initial={n: float(v) for n, v in zip(param_names, x0)},
        fitted=fitted,
        cost=float(sol.cost),
        residual_rms=float(np.sqrt(2.0 * sol.cost / n_res)),
        nfev=int(sol.nfev),
        success=bool(sol.success),
    )
