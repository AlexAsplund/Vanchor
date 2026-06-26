"""Human- and machine-readable reporting for a :class:`SimLog`.

- ``text_report`` / ``print_report`` -- a terminal summary with ASCII sparklines
  (no dependencies), so you can understand a run at a glance over SSH.
- ``write_csv`` -- the full time series for spreadsheets / pandas.
- ``write_plots`` -- multi-panel PNG charts via matplotlib *if it is installed*
  (optional; the rest works without it).
"""

from __future__ import annotations

import csv
import math
from dataclasses import fields
from pathlib import Path

from .metrics import AnchorMetrics, ChannelStats, channel_stats
from .runner import SimLog

_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int = 50) -> str:
    """A compact unicode sparkline of a series (NaNs render as spaces)."""
    clean = [v for v in values if not math.isnan(v)]
    if not clean:
        return ""
    lo, hi = min(clean), max(clean)
    span = (hi - lo) or 1.0
    # Down-sample to ``width`` buckets by averaging.
    n = len(values)
    out = []
    for i in range(width):
        a = i * n // width
        b = max(a + 1, (i + 1) * n // width)
        chunk = [v for v in values[a:b] if not math.isnan(v)]
        if not chunk:
            out.append(" ")
            continue
        avg = sum(chunk) / len(chunk)
        idx = int((avg - lo) / span * (len(_BLOCKS) - 1))
        out.append(_BLOCKS[min(len(_BLOCKS) - 1, max(0, idx))])
    return "".join(out)


def _fmt(v: float, d: int = 2) -> str:
    return "  nan" if (isinstance(v, float) and math.isnan(v)) else f"{v:.{d}f}"


def text_report(log: SimLog, anchor: AnchorMetrics | None = None) -> str:
    sc = log.scenario
    lines: list[str] = []
    lines.append(f"=== Simulation analysis: {sc.name} ===")
    dur = log.times()[-1] if log.samples else 0.0
    lines.append(
        f"model={sc.model}  duration={dur:.0f}s  samples={len(log)}  "
        f"env(cur={sc.environment.current_speed}@{sc.environment.current_dir} "
        f"wind={sc.environment.wind_speed}@{sc.environment.wind_dir})"
    )

    if anchor is not None:
        lines.append("")
        lines.append("-- Anchor hold --")
        m = anchor
        lines.append(f"  radius              : {_fmt(m.radius_m)} m")
        lines.append(f"  start distance      : {_fmt(m.start_distance_m)} m")
        lines.append(
            f"  closest approach    : {_fmt(m.closest_approach_m)} m "
            f"@ {_fmt(m.closest_approach_t, 1)} s"
        )
        lines.append(
            f"  OVERSHOOT (rebound) : {_fmt(m.overshoot_m)} m   "
            f"(peak distance after closest approach)"
        )
        lines.append(
            f"  settling time       : {_fmt(m.settling_time_s, 1)} s "
            f"(stay within {_fmt(m.settle_tolerance_m, 1)} m)"
        )
        lines.append(f"  time within radius  : {_fmt(m.within_radius_pct, 1)} %")
        lines.append(
            f"  steady (last window): mean {_fmt(m.steady_mean_m)}  rms {_fmt(m.steady_rms_m)}  "
            f"max {_fmt(m.steady_max_m)}  pk-pk {_fmt(m.steady_peak_to_peak_m)} m"
        )
        lines.append(
            f"  thrust              : mean {_fmt(m.thrust_mean)}  |mean| {_fmt(m.thrust_abs_mean)}  "
            f"reverse {_fmt(m.reverse_fraction * 100, 0)}%  reversals {m.thrust_reversals}  "
            f"effort {_fmt(m.control_effort, 1)}"
        )

    lines.append("")
    lines.append("-- Channels (min / mean / max) + sparkline over time --")
    for cs in channel_stats(log):
        spark = sparkline(log.series(cs.name))
        lines.append(
            f"  {cs.name:<20} {_fmt(cs.min):>8} {_fmt(cs.mean):>8} {_fmt(cs.max):>8}  {spark}"
        )
    return "\n".join(lines)


def print_report(log: SimLog, anchor: AnchorMetrics | None = None) -> None:
    print(text_report(log, anchor))


def write_csv(log: SimLog, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [f.name for f in fields(log.samples[0])] if log.samples else []
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for s in log.samples:
            writer.writerow(s.row())
    return path


def write_plots(log: SimLog, path: str | Path) -> Path | None:
    """Write a multi-panel PNG. Returns None (with a message) if matplotlib is
    not installed."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        print("matplotlib not installed; skipping plots (pip install matplotlib)")
        return None

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    t = log.times()
    fig, axes = plt.subplots(4, 1, figsize=(10, 11), sharex=True)
    fig.suptitle(f"Vanchor-NG simulation: {log.scenario.name}")

    axes[0].plot(t, log.series("dist_anchor_truth_m"), label="dist to anchor (truth)")
    axes[0].plot(t, log.series("dist_anchor_perc_m"), label="dist (perceived)", alpha=0.4)
    axes[0].plot(t, log.series("anchor_radius_m"), "k--", lw=0.8, label="radius")
    axes[0].plot(t, log.series("cross_track_m"), label="cross-track", alpha=0.6)
    axes[0].set_ylabel("metres"); axes[0].legend(loc="upper right", fontsize=8)

    axes[1].plot(t, log.series("truth_heading"), label="heading (truth)")
    axes[1].plot(t, log.series("target_heading"), label="target heading", alpha=0.6)
    axes[1].set_ylabel("deg"); axes[1].legend(loc="upper right", fontsize=8)

    axes[2].plot(t, log.series("thrust"), label="thrust")
    axes[2].plot(t, log.series("steering"), label="steering", alpha=0.6)
    axes[2].axhline(0, color="k", lw=0.6)
    axes[2].set_ylabel("-1..1"); axes[2].legend(loc="upper right", fontsize=8)

    axes[3].plot(t, log.series("truth_speed_mps"), label="speed (m/s)")
    axes[3].set_ylabel("m/s"); axes[3].set_xlabel("time (s)")
    axes[3].legend(loc="upper right", fontsize=8)

    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path
