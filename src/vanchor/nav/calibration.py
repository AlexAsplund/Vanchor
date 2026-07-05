"""Fusion calibration: short capture passes that measure this boat's sensors and
tune the GNSS/INS filter (nav.fusion) to them. A *system-ID* flow, separate from
the one-time boat-setup wizard -- re-run when a sensor moves.

Three capture MODES, each a different guided manoeuvre analysed from the same
recorded channels:

* ``still``  -- boat stationary, motor off. Measures gyro bias + per-sensor noise
  and derives the fusion gains from it.
* ``align``  -- drive straight at cruise. The steady difference between the
  compass heading and the GNSS course-over-ground is the compass/IMU **mounting
  yaw offset** (leeway is small on a straight run), applied as a heading offset.
* ``interference`` -- hold heading fixed (tie the bow off) and ramp motor thrust.
  How far the magnetic heading drifts from the magnetics-free gyro reference as
  thrust rises quantifies the **motor's magnetic interference** (a diagnostic:
  "your compass moves N° at full thrust" -> maybe go dual-antenna).

All pure/deterministic (no clock/I/O beyond the JSON helpers). A calibration field
left ``None`` keeps the NavFusion default, and captures MERGE (running ``align``
after ``still`` keeps the tuned gains and adds the offset), so a partial capture
never regresses anything.
"""
from __future__ import annotations

import contextlib
import json
import math
import statistics
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from ..core.geo import EARTH_RADIUS_M, angle_difference, normalize_deg

CALIBRATION_FILE = "fusion_cal.json"
CAPTURE_MODES = ("still", "align", "interference")
_MIN_SAMPLES = 20               # below this the capture is too short to trust
_MOVING_SPEED_MPS = 0.5         # mean speed above which the boat wasn't "still"
_ALIGN_MIN_SOG_MPS = 0.7        # a fix must be moving this fast to bound the course
_ALIGN_MIN_FRAMES = 10
_INTERF_MIN_THRUST_RANGE = 0.2  # thrust must sweep at least this much to be usable
_INTERF_MIN_STEER_RANGE = 20.0  # steer (deg) must sweep this much to fit the servo term
_INTERF_UNUSABLE_DEG = 20.0     # heading drift (deg) at which the compass scores 0

# The NavFusion gain fields a calibration may override.
GAIN_KEYS = ("heading_gain", "vel_tau_s", "dr_timeout_s",
             "crab_min_sog_mps", "crab_min_sog_measured_mps")


@dataclass
class FusionCalibration:
    """Per-boat fusion calibration. Every field is optional (``None`` => keep the
    NavFusion default / unmeasured), so results from different capture modes
    merge cleanly."""

    # still: bias correction + tuned gains
    gyro_bias_dps: float | None = None
    heading_gain: float | None = None
    vel_tau_s: float | None = None
    dr_timeout_s: float | None = None
    crab_min_sog_mps: float | None = None
    crab_min_sog_measured_mps: float | None = None
    # align: compass/IMU mounting yaw offset (added to the heading)
    heading_offset_deg: float | None = None
    # interference: diagnostics (not auto-applied)
    motor_interference_deg: float | None = None          # max heading drift over the sweep
    # Remedy model: correction = |thrust| * (slope + sin_coeff*sin(steer) + cos_coeff*cos(steer)).
    # slope is the thrust-only term (backward compatible); the sin/cos terms are the
    # SERVO contribution -- as the servo rotates the motor, its field direction turns.
    motor_interference_slope: float | None = None        # deg per unit thrust (steer-independent)
    motor_interference_sin: float | None = None          # servo term (deg/thrust) * sin(steer)
    motor_interference_cos: float | None = None          # servo term (deg/thrust) * cos(steer)
    motor_interference_score: int | None = None          # 0 (unusable) .. 100 (no interference)
    # EXPERIMENTAL: apply -slope*|thrust| to the heading in real time to undo the
    # motor's pull. None => unchanged; a dedicated toggle sets it True/False so a
    # later capture-mode save never flips it.
    interference_comp_enabled: bool | None = None
    # measured noise (provenance / display)
    gps_pos_sigma_m: float | None = None
    gps_vel_sigma_mps: float | None = None
    heading_sigma_deg: float | None = None
    yaw_rate_sigma_dps: float | None = None
    # last-capture provenance
    samples: int = 0
    duration_s: float = 0.0

    def gain_overrides(self) -> dict:
        """The non-None gain fields, as NavFusion attribute kwargs."""
        return {k: getattr(self, k) for k in GAIN_KEYS if getattr(self, k) is not None}

    def merged_with(self, other: FusionCalibration) -> FusionCalibration:
        """A copy with ``other``'s explicitly-measured (non-None) fields layered
        on top -- so each capture updates only what it measured."""
        updates = {k: v for k, v in asdict(other).items()
                   if v is not None and k not in ("samples", "duration_s")}
        updates["samples"] = other.samples
        updates["duration_s"] = other.duration_s
        return replace(self, **updates)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> FusionCalibration:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


class CaptureBuffer:
    """Accumulates raw samples during a capture. Per-channel scalar lists for the
    still-mode noise, plus a time-ordered list of heading *frames* (each carries
    the concurrent course, speed, thrust and gyro-integrated heading) for the
    align/interference analyses."""

    def __init__(self) -> None:
        self.yaw_rate: list[float] = []            # raw gyro yaw rate (deg/s)
        self.vel: list[tuple[float, float]] = []    # (vel_n, vel_e) m/s when present
        self.pos: list[tuple[float, float]] = []    # (lat, lon)
        self.frames: list[dict] = []                # per compass update, see add_heading
        self._t0: float | None = None
        self._t1: float | None = None

    def _stamp(self, t: float) -> None:
        if self._t0 is None:
            self._t0 = t
        self._t1 = t

    def add_imu(self, yaw_rate_dps: float, t: float) -> None:
        self.yaw_rate.append(yaw_rate_dps)
        self._stamp(t)

    def add_gps(self, lat: float, lon: float, vel_n: float | None,
                vel_e: float | None, t: float) -> None:
        self.pos.append((lat, lon))
        if vel_n is not None and vel_e is not None:
            self.vel.append((vel_n, vel_e))
        self._stamp(t)

    def add_heading(self, heading: float, *, cog: float | None, sog: float,
                    thrust: float, gyro: float, t: float, steer: float = 0.0) -> None:
        self.frames.append({"heading": heading, "cog": cog, "sog": sog,
                            "thrust": thrust, "steer": steer, "gyro": gyro})
        self._stamp(t)

    @property
    def headings(self) -> list[float]:
        return [f["heading"] for f in self.frames]

    @property
    def duration_s(self) -> float:
        if self._t0 is None or self._t1 is None:
            return 0.0
        return self._t1 - self._t0

    @property
    def count(self) -> int:
        return len(self.yaw_rate) + len(self.pos) + len(self.frames)


# -- helpers -------------------------------------------------------------- #
def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _round_opt(x: float | None, n: int) -> float | None:
    return round(x, n) if x is not None else None


def _circular_mean_deg(angles: list[float]) -> float:
    s = statistics.fmean(math.sin(math.radians(a)) for a in angles)
    c = statistics.fmean(math.cos(math.radians(a)) for a in angles)
    return math.degrees(math.atan2(s, c))


def _circular_std_deg(angles: list[float]) -> float | None:
    if len(angles) < 2:
        return None
    s = statistics.fmean(math.sin(math.radians(a)) for a in angles)
    c = statistics.fmean(math.cos(math.radians(a)) for a in angles)
    r = math.hypot(s, c)
    if r <= 1e-9:
        return 180.0
    return math.degrees(math.sqrt(max(0.0, -2.0 * math.log(r))))


def _pos_sigma_m(pos: list[tuple[float, float]]) -> float | None:
    if len(pos) < 2:
        return None
    lat0 = statistics.fmean(p[0] for p in pos)
    lon0 = statistics.fmean(p[1] for p in pos)
    k = math.pi / 180.0 * EARTH_RADIUS_M
    north = [(p[0] - lat0) * k for p in pos]
    east = [(p[1] - lon0) * k * math.cos(math.radians(lat0)) for p in pos]
    return math.hypot(statistics.pstdev(north), statistics.pstdev(east))


def _slope(xs: list[float], ys: list[float]) -> float:
    """Least-squares slope dy/dx (0 if x has no spread)."""
    mx = statistics.fmean(xs)
    my = statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 1e-12:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def _solve3(m: list[list[float]], v: list[float]) -> tuple[float, float, float]:
    """Solve a 3x3 linear system by Gaussian elimination with partial pivoting."""
    a = [row[:] + [v[i]] for i, row in enumerate(m)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(a[r][col]))
        a[col], a[piv] = a[piv], a[col]
        if abs(a[col][col]) < 1e-12:
            continue
        for r in range(3):
            if r != col:
                f = a[r][col] / a[col][col]
                for k in range(col, 4):
                    a[r][k] -= f * a[col][k]
    return tuple(a[i][3] / a[i][i] if abs(a[i][i]) > 1e-12 else 0.0 for i in range(3))  # type: ignore[return-value]


def _fit_interference(thr: list[float], steer_rad: list[float],
                      dev: list[float]) -> tuple[float, float, float]:
    """Least-squares fit of dev ~= |thrust|*(A + B*sin(steer) + C*cos(steer)),
    returning (A, B, C). Features are thrust-scaled so the model is 0 at 0 thrust."""
    feats = [[t, t * math.sin(s), t * math.cos(s)] for t, s in zip(thr, steer_rad)]
    m = [[sum(feats[k][i] * feats[k][j] for k in range(len(feats))) for j in range(3)]
         for i in range(3)]
    for i in range(3):
        m[i][i] += 1e-6  # tiny ridge for numerical safety
    rhs = [sum(feats[k][i] * dev[k] for k in range(len(feats))) for i in range(3)]
    return _solve3(m, rhs)


# -- tuners --------------------------------------------------------------- #
def tune_still(buf: CaptureBuffer) -> tuple[FusionCalibration, list[str]]:
    """Gyro bias + per-sensor noise -> fusion gains."""
    warnings: list[str] = []
    cal = FusionCalibration(samples=buf.count, duration_s=round(buf.duration_s, 1))
    if buf.count < _MIN_SAMPLES:
        warnings.append(f"Only {buf.count} samples -- keep the boat still and capture longer.")

    if len(buf.yaw_rate) >= 2:
        cal.gyro_bias_dps = round(statistics.fmean(buf.yaw_rate), 4)
        cal.yaw_rate_sigma_dps = round(statistics.pstdev(buf.yaw_rate), 4)

    if len(buf.vel) >= 2:
        sn = statistics.pstdev([v[0] for v in buf.vel])
        se = statistics.pstdev([v[1] for v in buf.vel])
        cal.gps_vel_sigma_mps = round(math.hypot(sn, se) / math.sqrt(2), 4)
        mean_speed = math.hypot(statistics.fmean(v[0] for v in buf.vel),
                                statistics.fmean(v[1] for v in buf.vel))
        if mean_speed > _MOVING_SPEED_MPS:
            warnings.append(f"The boat was moving (~{mean_speed:.1f} m/s); hold it "
                            "still with the motor off and re-run.")

    cal.gps_pos_sigma_m = _round_opt(_pos_sigma_m(buf.pos), 3)
    cal.heading_sigma_deg = _round_opt(_circular_std_deg(buf.headings), 3)

    if cal.gps_vel_sigma_mps is not None:
        v = cal.gps_vel_sigma_mps
        cal.vel_tau_s = round(_clamp(0.5 + 8.0 * v, 0.5, 5.0), 2)
        cal.crab_min_sog_mps = round(_clamp(4.0 * v, 0.15, 0.6), 2)
        cal.crab_min_sog_measured_mps = round(_clamp(2.0 * v, 0.03, 0.2), 3)
    if cal.heading_sigma_deg is not None:
        cal.heading_gain = round(_clamp(0.15 / (1.0 + cal.heading_sigma_deg), 0.02, 0.15), 3)
    return cal, warnings


def tune_align(buf: CaptureBuffer) -> tuple[FusionCalibration, list[str]]:
    """Straight-run compass-vs-course -> mounting yaw offset."""
    warnings: list[str] = []
    cal = FusionCalibration(samples=buf.count, duration_s=round(buf.duration_s, 1))
    moving = [f for f in buf.frames
              if f["cog"] is not None and f["sog"] >= _ALIGN_MIN_SOG_MPS]
    if len(moving) < _ALIGN_MIN_FRAMES:
        warnings.append("Not enough steady motion -- drive straight at cruise speed "
                        "for ~15 s and re-run.")
        return cal, warnings
    # offset such that heading + offset ~= course (small leeway on a straight run)
    diffs = [angle_difference(f["heading"], f["cog"]) for f in moving]
    cal.heading_offset_deg = round(_circular_mean_deg(diffs), 2)
    spread = _circular_std_deg([f["heading"] for f in moving])
    if spread is not None and spread > 8.0:
        warnings.append(f"Heading varied a lot (~{spread:.0f}°) -- hold a straight "
                        "course so the offset isn't polluted by turns.")
    return cal, warnings


def tune_interference(buf: CaptureBuffer) -> tuple[FusionCalibration, list[str]]:
    """Thrust sweep -> how far the magnetic heading drifts from the gyro reference."""
    warnings: list[str] = []
    cal = FusionCalibration(samples=buf.count, duration_s=round(buf.duration_s, 1))
    frames = buf.frames
    if len(frames) < _ALIGN_MIN_FRAMES:
        warnings.append("Too few samples -- ramp the motor slowly over ~15 s and re-run.")
        return cal, warnings
    thrusts = [abs(f["thrust"]) for f in frames]
    if max(thrusts) - min(thrusts) < _INTERF_MIN_THRUST_RANGE:
        warnings.append("Thrust barely changed -- ramp it from 0 toward full and re-run.")
        return cal, warnings
    # (compass - gyro) offset relative to its value at the start of the sweep:
    # the gyro is magnetics-free, so a growing offset is interference, not a real turn.
    base = angle_difference(frames[0]["gyro"], frames[0]["heading"])
    devs = [angle_difference(f["gyro"], f["heading"]) - base for f in frames]
    devs = [(d + 180.0) % 360.0 - 180.0 for d in devs]  # wrap to [-180, 180)
    max_dev = max(abs(d) for d in devs)
    cal.motor_interference_deg = round(max_dev, 2)

    # Fit the remedy model. If the SERVO (steer) swept enough, fit the full
    # thrust + steer model so the correction follows the motor as it rotates;
    # otherwise fall back to the thrust-only slope and say the servo term is unknown.
    steers = [float(f.get("steer", 0.0)) for f in frames]
    if max(steers) - min(steers) >= _INTERF_MIN_STEER_RANGE:
        a, b, c = _fit_interference(thrusts, [math.radians(s) for s in steers], devs)
        cal.motor_interference_slope = round(a, 3)
        cal.motor_interference_sin = round(b, 3)
        cal.motor_interference_cos = round(c, 3)
    else:
        cal.motor_interference_slope = round(_slope(thrusts, devs), 3)
        warnings.append("Steering didn't vary, so the servo's contribution wasn't "
                        "measured -- sweep the steering through its range while you "
                        "ramp thrust to compensate for the servo too.")

    # Quality score: 100 = the motor doesn't move the compass at all; 0 = it moves
    # it by _INTERF_UNUSABLE_DEG or more (heading that corrupt is unusable for a
    # heading-critical spot-lock). Linear between.
    cal.motor_interference_score = round(100 * _clamp(1.0 - max_dev / _INTERF_UNUSABLE_DEG, 0.0, 1.0))
    return cal, warnings


def interference_recommendations(score: int | None) -> list[str]:
    """Ordered mitigation actions for a motor-interference score (0-100), most
    to least worthwhile, escalating as the score worsens. Empty if not measured."""
    if score is None:
        return []
    if score >= 85:
        return ["Well sited -- the motor barely moves the compass. No action needed."]
    recs = [
        "Move the compass/IMU farther from the motor and its power cables: the "
        "magnetic field falls off with the cube of distance, so even a few extra "
        "centimetres help a lot. Mount it high and as far forward as practical.",
        "Route the motor's power cables away from the compass, and twist the +/- "
        "pair together so their opposing magnetic fields largely cancel.",
        "Keep the compass clear of ferrous metal and the battery (hard/soft-iron).",
    ]
    if score < 70:
        recs.append(
            "Re-run the HWT901B's own magnetometer calibration in place, so its "
            "internal hard/soft-iron correction accounts for the fixed metal around it.")
    if score < 55:
        recs.append(
            "Add magnetic shielding: a mu-metal (high-permeability) shroud around "
            "the compass or motor redirects the DC magnetic field. Note a plain "
            "conductive Faraday cage blocks RF/electrical noise but NOT a static "
            "magnetic field -- use mu-metal for that.")
        recs.append(
            "Bond the motor and electronics to a clean common ground: this cuts "
            "electrical (ground-loop/EMI) noise, though the DC field from motor "
            "current is best fixed by distance + cable routing above.")
    if score < 40:
        recs.append(
            "The drift is repeatable with thrust, so a software compensation "
            "(subtract the measured deg/thrust from the heading) can partly correct "
            "it once the physical fixes are done.")
        recs.append(
            "If it stays this bad, switch to a magnetics-free heading source -- a "
            "dual-antenna GNSS compass is immune to the motor entirely.")
    return recs


def tune(buf: CaptureBuffer, mode: str) -> tuple[FusionCalibration, list[str]]:
    if mode == "align":
        return tune_align(buf)
    if mode == "interference":
        return tune_interference(buf)
    return tune_still(buf)


# -- persistence ---------------------------------------------------------- #
def load_calibration(data_dir: str | Path) -> FusionCalibration | None:
    path = Path(data_dir) / CALIBRATION_FILE
    try:
        return FusionCalibration.from_dict(json.loads(path.read_text()))
    except (OSError, ValueError):
        return None


def save_calibration(data_dir: str | Path, cal: FusionCalibration) -> None:
    (Path(data_dir) / CALIBRATION_FILE).write_text(json.dumps(cal.to_dict(), indent=2))


def clear_calibration(data_dir: str | Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        (Path(data_dir) / CALIBRATION_FILE).unlink()
