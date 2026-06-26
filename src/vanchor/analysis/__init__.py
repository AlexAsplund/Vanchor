"""Simulation analysis framework: run scenarios, record full time series, and
turn them into metrics, terminal reports, CSV, and plots.

Quick start::

    from vanchor.analysis import run_scenario, anchor_metrics, print_report
    from vanchor.analysis.scenarios import SCENARIOS

    log = run_scenario(SCENARIOS["anchor_tight"])
    print_report(log, anchor_metrics(log))

Or from the shell::

    python -m vanchor.analysis anchor_tight --plot out/anchor.png --csv out/anchor.csv
"""

from .metrics import (
    AnchorMetrics,
    ChannelStats,
    HeadingMetrics,
    SteeringActivity,
    anchor_metrics,
    channel_stats,
    heading_metrics,
    steering_activity,
)
from .report import print_report, sparkline, text_report, write_csv, write_plots
from .runner import Command, Sample, Scenario, SimLog, run_scenario
from .tuning import Param, TuningJob, TuningResult, format_result, optimize, tune

__all__ = [
    "Scenario",
    "Command",
    "Sample",
    "SimLog",
    "run_scenario",
    "anchor_metrics",
    "heading_metrics",
    "steering_activity",
    "SteeringActivity",
    "channel_stats",
    "AnchorMetrics",
    "HeadingMetrics",
    "ChannelStats",
    "text_report",
    "print_report",
    "sparkline",
    "write_csv",
    "write_plots",
    "tune",
    "optimize",
    "format_result",
    "Param",
    "TuningJob",
    "TuningResult",
]
