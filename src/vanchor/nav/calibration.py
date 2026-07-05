"""Fusion calibration: capture the boat's sensor noise while it sits still and
tune the GNSS/INS filter (nav.fusion) to it.

This is a small *system-ID* pass, separate from the one-time boat-setup wizard: a
short still capture (motor off) measures the gyro bias and the per-sensor noise,
from which we derive boat-specific fusion gains instead of the hand-picked
defaults. Everything here is pure/deterministic (no clock, no I/O except the
JSON load/save helpers); the runtime injects timestamps.

The mappings from measured noise to gains are deliberately simple, monotonic and
clamped -- documented heuristics, not a formal optimum: noisier velocity -> more
smoothing + higher crab thresholds; noisier compass -> a gentler complementary
blend (trust the gyro more). A calibration with a field left ``None`` means "keep
the NavFusion default", so a partial capture never makes things worse.
"""
from __future__ import annotations

import contextlib
import json
import math
import statistics
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from ..core.geo import EARTH_RADIUS_M

CALIBRATION_FILE = "fusion_cal.json"
_MIN_SAMPLES = 20                       # below this the capture is too short to trust
_MOVING_SPEED_MPS = 0.5                 # mean speed above which the boat wasn't "still"

# The NavFusion gain fields a calibration may override.
GAIN_KEYS = ("heading_gain", "vel_tau_s", "dr_timeout_s",
             "crab_min_sog_mps", "crab_min_sog_measured_mps")


@dataclass
class FusionCalibration:
    """A per-boat fusion calibration: a gyro-bias correction, optional tuned
    gains (``None`` => use the NavFusion default) and the measured noise the
    tuning came from (for display/provenance)."""

    gyro_bias_dps: float = 0.0
    # Tuned NavFusion gains (None => default).
    heading_gain: float | None = None
    vel_tau_s: float | None = None
    dr_timeout_s: float | None = None
    crab_min_sog_mps: float | None = None
    crab_min_sog_measured_mps: float | None = None
    # Measured noise this calibration was derived from.
    gps_pos_sigma_m: float | None = None
    gps_vel_sigma_mps: float | None = None
    heading_sigma_deg: float | None = None
    yaw_rate_sigma_dps: float | None = None
    # Provenance.
    samples: int = 0
    duration_s: float = 0.0

    def gain_overrides(self) -> dict:
        """The non-None gain fields, as NavFusion constructor/attribute kwargs."""
        return {k: getattr(self, k) for k in GAIN_KEYS if getattr(self, k) is not None}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> FusionCalibration:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


class CaptureBuffer:
    """Accumulates raw sensor samples during a still capture, per channel (each
    sensor arrives at its own rate, so channels are independent lists)."""

    def __init__(self) -> None:
        self.yaw_rate: list[float] = []           # raw gyro yaw rate (deg/s)
        self.vel: list[tuple[float, float]] = []   # (vel_n, vel_e) m/s when present
        self.pos: list[tuple[float, float]] = []   # (lat, lon)
        self.heading: list[float] = []             # compass heading (deg)
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

    def add_heading(self, heading_deg: float, t: float) -> None:
        self.heading.append(heading_deg)
        self._stamp(t)

    @property
    def duration_s(self) -> float:
        if self._t0 is None or self._t1 is None:
            return 0.0
        return self._t1 - self._t0

    @property
    def count(self) -> int:
        return len(self.yaw_rate) + len(self.pos) + len(self.heading)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _circular_std_deg(angles: list[float]) -> float | None:
    """Circular standard deviation (deg) of a set of headings."""
    if len(angles) < 2:
        return None
    s = statistics.fmean(math.sin(math.radians(a)) for a in angles)
    c = statistics.fmean(math.cos(math.radians(a)) for a in angles)
    r = math.hypot(s, c)
    if r <= 1e-9:
        return 180.0
    return math.degrees(math.sqrt(max(0.0, -2.0 * math.log(r))))


def _pos_sigma_m(pos: list[tuple[float, float]]) -> float | None:
    """Combined N/E scatter (m) of a set of lat/lon points about their centroid."""
    if len(pos) < 2:
        return None
    lat0 = statistics.fmean(p[0] for p in pos)
    lon0 = statistics.fmean(p[1] for p in pos)
    k = math.pi / 180.0 * EARTH_RADIUS_M
    north = [(p[0] - lat0) * k for p in pos]
    east = [(p[1] - lon0) * k * math.cos(math.radians(lat0)) for p in pos]
    return math.hypot(statistics.pstdev(north), statistics.pstdev(east))


def tune(buf: CaptureBuffer) -> tuple[FusionCalibration, list[str]]:
    """Derive a :class:`FusionCalibration` from a still-capture buffer, plus any
    warnings (too few samples, boat wasn't still)."""
    warnings: list[str] = []
    cal = FusionCalibration(samples=buf.count, duration_s=round(buf.duration_s, 1))

    if buf.count < _MIN_SAMPLES:
        warnings.append(
            f"Only {buf.count} samples captured -- keep the boat still and "
            "capture longer for a reliable calibration."
        )

    if len(buf.yaw_rate) >= 2:
        cal.gyro_bias_dps = round(statistics.fmean(buf.yaw_rate), 4)
        cal.yaw_rate_sigma_dps = round(statistics.pstdev(buf.yaw_rate), 4)

    if len(buf.vel) >= 2:
        sn = statistics.pstdev([v[0] for v in buf.vel])
        se = statistics.pstdev([v[1] for v in buf.vel])
        cal.gps_vel_sigma_mps = round(math.hypot(sn, se) / math.sqrt(2), 4)  # per-axis RMS
        mean_speed = math.hypot(
            statistics.fmean(v[0] for v in buf.vel),
            statistics.fmean(v[1] for v in buf.vel),
        )
        if mean_speed > _MOVING_SPEED_MPS:
            warnings.append(
                f"The boat was moving (~{mean_speed:.1f} m/s) during the capture; "
                "hold it still with the motor off and re-run."
            )

    cal.gps_pos_sigma_m = _round_opt(_pos_sigma_m(buf.pos), 3)
    cal.heading_sigma_deg = _round_opt(_circular_std_deg(buf.heading), 3)

    # noise -> gains (monotonic, clamped heuristics)
    if cal.gps_vel_sigma_mps is not None:
        v = cal.gps_vel_sigma_mps
        cal.vel_tau_s = round(_clamp(0.5 + 8.0 * v, 0.5, 5.0), 2)
        cal.crab_min_sog_mps = round(_clamp(4.0 * v, 0.15, 0.6), 2)
        cal.crab_min_sog_measured_mps = round(_clamp(2.0 * v, 0.03, 0.2), 3)
    if cal.heading_sigma_deg is not None:
        cal.heading_gain = round(_clamp(0.15 / (1.0 + cal.heading_sigma_deg), 0.02, 0.15), 3)

    return cal, warnings


def _round_opt(x: float | None, n: int) -> float | None:
    return round(x, n) if x is not None else None


# -- persistence ---------------------------------------------------------- #
def load_calibration(data_dir: str | Path) -> FusionCalibration | None:
    path = Path(data_dir) / CALIBRATION_FILE
    try:
        return FusionCalibration.from_dict(json.loads(path.read_text()))
    except (OSError, ValueError):
        return None


def save_calibration(data_dir: str | Path, cal: FusionCalibration) -> None:
    path = Path(data_dir) / CALIBRATION_FILE
    path.write_text(json.dumps(cal.to_dict(), indent=2))


def clear_calibration(data_dir: str | Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        (Path(data_dir) / CALIBRATION_FILE).unlink()
