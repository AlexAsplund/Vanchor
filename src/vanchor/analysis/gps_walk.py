"""GPS walk characterization from a debug recording (issue #154).

Feeds the no-RTK station-keeping design: record the boat STATIONARY (antenna
fixed on land, or tied to a dock/buoy) with the debug recorder, then run this
over the ``session-*.ndjson[.gz]`` part(s) to get the numbers that set the hold
controller's constants:

* position scatter (std N/E, CEP50/95, max excursion) -- the "walk" envelope
* position autocorrelation time tau -- how SLOW the walk is; sets the outer
  position-loop time constant (averaging much beyond tau stops helping)
* averaging-window deviation -- how much a window-mean still moves vs window
  length (Allan-style); shows where averaging saturates
* velocity noise floor at rest (Doppler NED components) -- sets the inner
  velocity-loop deadband/gains
* reported-accuracy honesty -- mean reported hAcc vs the empirical 2D RMS

Stdlib only (json/gzip/math/statistics); a recording is NDJSON lines of
``{"t": epoch, "kind": "telemetry", "data": {...}}`` where data carries
``position{lat,lon}``, optionally ``fusion{ground_vel_n_mps, ground_vel_e_mps}``
and ``gps{h_acc_m}``.

Usage::

    python -m vanchor.analysis.gps_walk session-XXXX.ndjson.gz [more parts...]
"""
from __future__ import annotations

import gzip
import json
import math
import statistics
import sys
from dataclasses import dataclass

M_PER_DEG_LAT = 111_320.0


@dataclass
class Sample:
    t: float
    lat: float
    lon: float
    vel_n: float | None = None
    vel_e: float | None = None
    h_acc: float | None = None


def read_samples(paths: list[str]) -> list[Sample]:
    """Telemetry position/velocity samples from recording part(s), time-sorted."""
    out: list[Sample] = []
    for path in paths:
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("kind") != "telemetry":
                    continue
                d = rec.get("data") or {}
                pos = d.get("position") or {}
                lat, lon = pos.get("lat"), pos.get("lon")
                if lat is None or lon is None:
                    continue
                fus = d.get("fusion") or {}
                gps = d.get("gps") or {}
                out.append(Sample(
                    t=float(rec["t"]), lat=float(lat), lon=float(lon),
                    vel_n=_maybe(fus.get("ground_vel_n_mps")),
                    vel_e=_maybe(fus.get("ground_vel_e_mps")),
                    h_acc=_maybe(gps.get("h_acc_m")),
                ))
    out.sort(key=lambda s: s.t)
    return out


def _maybe(v) -> float | None:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def to_local_en(samples: list[Sample]) -> list[tuple[float, float, float]]:
    """(t, east_m, north_m) relative to the mean position (cos-lat scaled)."""
    mlat = statistics.fmean(s.lat for s in samples)
    mlon = statistics.fmean(s.lon for s in samples)
    coslat = max(0.1, math.cos(math.radians(mlat)))
    return [(s.t,
             (s.lon - mlon) * M_PER_DEG_LAT * coslat,
             (s.lat - mlat) * M_PER_DEG_LAT) for s in samples]


def scatter_stats(en: list[tuple[float, float, float]]) -> dict:
    """Position scatter around the mean: std, CEP50/95, max excursion."""
    r = sorted(math.hypot(e, n) for _, e, n in en)
    n_std = statistics.pstdev(n for _, _, n in en)
    e_std = statistics.pstdev(e for _, e, _ in en)
    return {
        "std_east_m": e_std, "std_north_m": n_std,
        "rms_2d_m": math.sqrt(e_std ** 2 + n_std ** 2),
        "cep50_m": r[len(r) // 2],
        "cep95_m": r[min(len(r) - 1, int(len(r) * 0.95))],
        "max_m": r[-1],
    }


def autocorr_time_s(en: list[tuple[float, float, float]]) -> float | None:
    """Lag where the east/north autocorrelation first drops below 1/e.

    The walk's memory: filter/averaging time constants beyond ~tau give
    diminishing returns, and the anchor reference itself moves on this scale."""
    if len(en) < 20:
        return None
    ts = [t for t, _, _ in en]
    dt = (ts[-1] - ts[0]) / (len(ts) - 1)
    if dt <= 0:
        return None
    e_mean = statistics.fmean(e for _, e, _ in en)
    n_mean = statistics.fmean(n for _, _, n in en)
    e0 = [e - e_mean for _, e, _ in en]
    n0 = [n - n_mean for _, _, n in en]
    denom = sum(a * a for a in e0) + sum(a * a for a in n0)
    if denom <= 0:
        return None
    max_lag = len(en) // 2
    for lag in range(1, max_lag):
        num = (sum(e0[i] * e0[i + lag] for i in range(len(e0) - lag))
               + sum(n0[i] * n0[i + lag] for i in range(len(n0) - lag)))
        if num / denom < 1.0 / math.e:
            return lag * dt
    return max_lag * dt  # never decayed inside the record -> lower bound


def window_deviation(en: list[tuple[float, float, float]],
                     windows_s: tuple[float, ...] = (1, 5, 10, 30, 60, 120)) -> dict:
    """Allan-style: mean step between consecutive window-mean positions.

    Reading it: white noise makes the step SHRINK with longer windows (~1/sqrt N)
    while the slow walk makes it GROW toward saturation (~sqrt(2)*sigma once the
    window exceeds the walk's tau). Real GPS is the mix -- the MINIMUM marks the
    optimal averaging window; beyond it the walk dominates and longer averaging
    only adds lag."""
    t0 = en[0][0]
    out: dict[float, float] = {}
    for w in windows_s:
        means: list[tuple[float, float]] = []
        bucket: list[tuple[float, float]] = []
        edge = t0 + w
        for t, e, n in en:
            if t >= edge:
                if bucket:
                    means.append((statistics.fmean(p[0] for p in bucket),
                                  statistics.fmean(p[1] for p in bucket)))
                bucket = []
                edge += w
            bucket.append((e, n))
        if len(means) >= 3:
            steps = [math.hypot(means[i + 1][0] - means[i][0],
                                means[i + 1][1] - means[i][1])
                     for i in range(len(means) - 1)]
            out[w] = statistics.fmean(steps)
    return out


def velocity_floor(samples: list[Sample]) -> dict | None:
    """Doppler velocity noise at rest: std/mean of the NED components."""
    vn = [s.vel_n for s in samples if s.vel_n is not None]
    ve = [s.vel_e for s in samples if s.vel_e is not None]
    if len(vn) < 10:
        return None
    speed = [math.hypot(a, b) for a, b in zip(vn, ve)]
    return {
        "n": len(vn),
        "std_vel_n_mps": statistics.pstdev(vn),
        "std_vel_e_mps": statistics.pstdev(ve),
        "mean_speed_mps": statistics.fmean(speed),
        "p95_speed_mps": sorted(speed)[min(len(speed) - 1, int(len(speed) * 0.95))],
    }


def analyze(paths: list[str]) -> dict:
    samples = read_samples(paths)
    if len(samples) < 10:
        return {"ok": False, "error": f"only {len(samples)} usable telemetry "
                "samples; record a longer stationary session"}
    en = to_local_en(samples)
    duration = samples[-1].t - samples[0].t
    scatter = scatter_stats(en)
    tau = autocorr_time_s(en)
    vel = velocity_floor(samples)
    haccs = [s.h_acc for s in samples if s.h_acc is not None]
    honesty = None
    if haccs:
        honesty = {"mean_reported_hacc_m": statistics.fmean(haccs),
                   "empirical_rms_2d_m": scatter["rms_2d_m"]}
    return {
        "ok": True,
        "samples": len(samples),
        "duration_s": duration,
        "rate_hz": (len(samples) - 1) / duration if duration > 0 else 0.0,
        "scatter": scatter,
        "autocorr_time_s": tau,
        "window_deviation_m": window_deviation(en),
        "velocity_floor": vel,
        "hacc_honesty": honesty,
    }


def format_report(res: dict) -> str:
    if not res.get("ok"):
        return f"gps_walk: {res.get('error')}"
    L = [
        "GPS walk characterization (stationary recording)",
        f"  samples : {res['samples']}  over {res['duration_s']:.0f} s "
        f"({res['rate_hz']:.1f} Hz)",
        "  -- position scatter (vs mean) --",
    ]
    s = res["scatter"]
    L.append(f"  std E/N : {s['std_east_m']:.2f} / {s['std_north_m']:.2f} m   "
             f"2D RMS {s['rms_2d_m']:.2f} m")
    L.append(f"  CEP50   : {s['cep50_m']:.2f} m   CEP95 {s['cep95_m']:.2f} m   "
             f"max {s['max_m']:.2f} m")
    tau = res["autocorr_time_s"]
    L.append(f"  walk tau: {tau:.0f} s (autocorr 1/e)" if tau is not None
             else "  walk tau: n/a (too few samples)")
    wd = res["window_deviation_m"]
    if wd:
        L.append("  -- averaged-position step per window (minimum = optimal window) --")
        for w, v in sorted(wd.items()):
            L.append(f"    {w:5.0f} s window -> mean step {v:.2f} m")
    v = res["velocity_floor"]
    if v:
        L.append("  -- Doppler velocity floor at rest --")
        L.append(f"  std N/E : {v['std_vel_n_mps']:.3f} / {v['std_vel_e_mps']:.3f} m/s"
                 f"   mean |v| {v['mean_speed_mps']:.3f}  p95 {v['p95_speed_mps']:.3f} m/s")
        L.append(f"  -> inner-loop velocity deadband ~ {2 * v['p95_speed_mps']:.2f} m/s")
    h = res["hacc_honesty"]
    if h:
        ratio = (h["empirical_rms_2d_m"] / h["mean_reported_hacc_m"]
                 if h["mean_reported_hacc_m"] > 0 else float("nan"))
        L.append("  -- reported-accuracy honesty --")
        L.append(f"  mean hAcc {h['mean_reported_hacc_m']:.2f} m vs empirical "
                 f"RMS {h['empirical_rms_2d_m']:.2f} m  (ratio {ratio:.2f})")
    if tau is not None:
        L.append(f"  -> suggested outer position-loop tau: {max(10.0, min(tau, 60.0)):.0f} s")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 2
    print(format_report(analyze(args)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
