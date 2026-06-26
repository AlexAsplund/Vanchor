"""Quantitative metrics computed from a :class:`~vanchor.analysis.runner.SimLog`.

These turn a raw time series into the numbers you actually reason about when
tuning -- overshoot, settling time, steady-state error, how hard the motor is
working, whether it is chattering, etc. Everything operates on *ground truth*
(what the boat really did), not the noisy perceived signal.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from ..core.geo import angle_difference
from .runner import SimLog


# --------------------------------------------------------------------------- #
# Small NaN-aware statistics
# --------------------------------------------------------------------------- #
def _clean(xs: list[float]) -> list[float]:
    return [x for x in xs if not math.isnan(x)]


def _mean(xs: list[float]) -> float:
    xs = _clean(xs)
    return sum(xs) / len(xs) if xs else math.nan


def _std(xs: list[float]) -> float:
    xs = _clean(xs)
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _rms(xs: list[float]) -> float:
    xs = _clean(xs)
    return math.sqrt(sum(x * x for x in xs) / len(xs)) if xs else math.nan


@dataclass
class ChannelStats:
    name: str
    n: int
    min: float
    max: float
    mean: float
    std: float

    @classmethod
    def of(cls, name: str, xs: list[float]) -> "ChannelStats":
        c = _clean(xs)
        return cls(
            name=name,
            n=len(c),
            min=min(c) if c else math.nan,
            max=max(c) if c else math.nan,
            mean=_mean(c),
            std=_std(c),
        )


# --------------------------------------------------------------------------- #
# Anchor-hold metrics
# --------------------------------------------------------------------------- #
@dataclass
class AnchorMetrics:
    radius_m: float
    start_distance_m: float          # how far off-station the run began
    closest_approach_m: float        # nearest the boat got to the mark
    closest_approach_t: float
    overshoot_m: float               # peak distance reached AFTER the closest approach
    settling_time_s: float           # time to enter & stay within tolerance (nan if never)
    settle_tolerance_m: float
    within_radius_pct: float         # % of post-settle time inside the radius
    steady_mean_m: float             # tail-window mean distance to anchor
    steady_rms_m: float
    steady_max_m: float
    steady_peak_to_peak_m: float     # oscillation amplitude in the tail
    thrust_mean: float
    thrust_abs_mean: float
    reverse_fraction: float          # fraction of time using reverse thrust
    thrust_reversals: int            # sign flips (chattering indicator)
    control_effort: float            # integral of |d(thrust)|, total actuator travel

    def to_dict(self) -> dict:
        return asdict(self)


def anchor_metrics(
    log: SimLog, *, tail_seconds: float = 30.0, settle_tolerance_m: float | None = None
) -> AnchorMetrics:
    """Analyse a station-keeping run from its ground-truth distance to anchor."""
    samples = [s for s in log.samples if not math.isnan(s.dist_anchor_truth_m)]
    if not samples:
        raise ValueError("no anchor set during this run -- nothing to analyse")

    ts = [s.t for s in samples]
    dist = [s.dist_anchor_truth_m for s in samples]
    thrust = [s.thrust for s in samples]
    radius = samples[-1].anchor_radius_m
    tol = settle_tolerance_m if settle_tolerance_m is not None else max(radius, 1.5)

    start_distance = dist[0]
    closest_i = min(range(len(dist)), key=lambda i: dist[i])
    closest = dist[closest_i]
    # Overshoot = how far it swung back out *after* its closest approach.
    after = dist[closest_i:]
    overshoot = (max(after) if after else closest)

    # Settling time: last moment it was still outside tolerance (it "settles"
    # once it never again leaves the tolerance band).
    settling_time = math.nan
    for i in range(len(dist)):
        if dist[i] <= tol and all(d <= tol for d in dist[i:]):
            settling_time = ts[i] - ts[0]
            break

    # Post-settle / tail-window stats.
    tail = [s for s in samples if s.t >= ts[-1] - tail_seconds]
    tail_d = [s.dist_anchor_truth_m for s in tail]
    within = (
        100.0 * sum(1 for d in tail_d if d <= radius) / len(tail_d) if tail_d else 0.0
    )

    # Thrust usage.
    reversals = 0
    for a, b in zip(thrust, thrust[1:]):
        if (a > 0.02 and b < -0.02) or (a < -0.02 and b > 0.02):
            reversals += 1
    effort = sum(abs(b - a) for a, b in zip(thrust, thrust[1:]))
    rev_frac = sum(1 for x in thrust if x < -0.02) / len(thrust)

    return AnchorMetrics(
        radius_m=radius,
        start_distance_m=start_distance,
        closest_approach_m=closest,
        closest_approach_t=ts[closest_i] - ts[0],
        overshoot_m=overshoot,
        settling_time_s=settling_time,
        settle_tolerance_m=tol,
        within_radius_pct=within,
        steady_mean_m=_mean(tail_d),
        steady_rms_m=_rms(tail_d),
        steady_max_m=max(tail_d) if tail_d else math.nan,
        steady_peak_to_peak_m=(max(tail_d) - min(tail_d)) if tail_d else math.nan,
        thrust_mean=_mean(thrust),
        thrust_abs_mean=_mean([abs(x) for x in thrust]),
        reverse_fraction=rev_frac,
        thrust_reversals=reversals,
        control_effort=effort,
    )


# --------------------------------------------------------------------------- #
# Heading-hold metrics
# --------------------------------------------------------------------------- #
@dataclass
class HeadingMetrics:
    target_deg: float
    rise_time_s: float       # time to first reach 90% of the heading step
    overshoot_deg: float     # worst excursion past the target
    settling_time_s: float   # time to stay within tolerance
    settle_tolerance_deg: float
    steady_error_deg: float  # mean |error| in the tail

    def to_dict(self) -> dict:
        return asdict(self)


def heading_metrics(
    log: SimLog,
    target_deg: float,
    *,
    start_t: float = 0.0,
    tail_seconds: float = 15.0,
    settle_tolerance_deg: float = 3.0,
) -> HeadingMetrics:
    samples = [s for s in log.samples if s.t >= start_t]
    if not samples:
        raise ValueError("no samples after start_t")
    ts = [s.t for s in samples]
    err = [angle_difference(s.truth_heading, target_deg) for s in samples]  # signed
    abserr = [abs(e) for e in err]
    initial = abserr[0] if abserr else 0.0

    rise = math.nan
    for i, e in enumerate(abserr):
        if e <= 0.1 * initial:
            rise = ts[i] - ts[0]
            break

    # Overshoot: furthest the heading went *past* the target (opposite sign to
    # the initial error).
    init_sign = 1.0 if err[0] >= 0 else -1.0
    past = [(-init_sign) * e for e in err]  # positive = beyond target
    overshoot = max(0.0, max(past))

    settling = math.nan
    for i in range(len(abserr)):
        if abserr[i] <= settle_tolerance_deg and all(
            a <= settle_tolerance_deg for a in abserr[i:]
        ):
            settling = ts[i] - ts[0]
            break

    tail = [abs(angle_difference(s.truth_heading, target_deg)) for s in samples if s.t >= ts[-1] - tail_seconds]
    return HeadingMetrics(
        target_deg=target_deg,
        rise_time_s=rise,
        overshoot_deg=overshoot,
        settling_time_s=settling,
        settle_tolerance_deg=settle_tolerance_deg,
        steady_error_deg=_mean(tail),
    )


# --------------------------------------------------------------------------- #
# Generic per-channel description
# --------------------------------------------------------------------------- #
NUMERIC_CHANNELS = [
    "dist_anchor_truth_m",
    "cross_track_m",
    "dist_waypoint_m",
    "truth_heading",
    "target_heading",
    "truth_speed_mps",
    "sog_knots",
    "thrust",
    "steering",
    "steer_angle_deg",
]


def channel_stats(log: SimLog, channels: list[str] | None = None) -> list[ChannelStats]:
    return [ChannelStats.of(c, log.series(c)) for c in (channels or NUMERIC_CHANNELS)]


@dataclass
class SteeringActivity:
    """How hard the steering actuator is being worked -- a jitter/wear proxy."""

    max_rate_dps: float  # peak rotation rate of the steering head
    mean_rate_dps: float
    reversals_per_s: float  # direction changes per second

    def to_dict(self) -> dict:
        return asdict(self)


def steering_activity(log: SimLog, max_steer_angle_deg: float = 35.0) -> SteeringActivity:
    """Rotation rate / reversals of the steering command (``steering`` in [-1,1]
    maps to +/-``max_steer_angle_deg`` of head rotation)."""
    s = log.series("steering")
    t = log.times()
    rates: list[float] = []
    reversals = 0
    last_sign = 0
    last, last_t = s[0], t[0]
    for i in range(1, len(s)):
        if s[i] != last:
            rates.append(abs(s[i] - last) * max_steer_angle_deg / (t[i] - last_t))
            sign = 1 if s[i] > last else -1
            if last_sign and sign != last_sign:
                reversals += 1
            last_sign, last, last_t = sign, s[i], t[i]
    dur = (t[-1] - t[0]) or 1.0
    return SteeringActivity(
        max_rate_dps=max(rates) if rates else 0.0,
        mean_rate_dps=(sum(rates) / len(rates)) if rates else 0.0,
        reversals_per_s=reversals / dur,
    )
