"""CLI: run a named scenario and report on it.

    python -m vanchor.analysis --list
    python -m vanchor.analysis anchor_tight
    python -m vanchor.analysis anchor_tight --csv out/a.csv --plot out/a.png
"""

from __future__ import annotations

import argparse
import math

from .metrics import anchor_metrics, heading_metrics
from .report import print_report, write_csv, write_plots
from .runner import run_scenario
from .scenarios import SCENARIOS


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Vanchor-NG simulation analysis")
    parser.add_argument("scenario", nargs="?", help="scenario name (see --list)")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("--csv", help="write the full time series to this CSV path")
    parser.add_argument("--plot", help="write a multi-panel PNG to this path")
    args = parser.parse_args(argv)

    if args.list or not args.scenario:
        print("Available scenarios:")
        for name in SCENARIOS:
            print(f"  {name}")
        return

    if args.scenario not in SCENARIOS:
        raise SystemExit(f"unknown scenario {args.scenario!r}; try --list")

    scenario = SCENARIOS[args.scenario]
    log = run_scenario(scenario)

    anchor = None
    if any(not math.isnan(s.dist_anchor_truth_m) for s in log.samples):
        anchor = anchor_metrics(log)
    print_report(log, anchor)

    # If a heading_hold command was issued, also print heading metrics.
    for cmd in scenario.commands:
        if cmd.command.get("type") == "heading_hold" and "heading" in cmd.command:
            hm = heading_metrics(log, float(cmd.command["heading"]), start_t=cmd.t)
            print("\n-- Heading hold --")
            for k, v in hm.to_dict().items():
                print(f"  {k:<22}: {v}")
            break

    if args.csv:
        print(f"\nwrote CSV  -> {write_csv(log, args.csv)}")
    if args.plot:
        out = write_plots(log, args.plot)
        if out:
            print(f"wrote plot -> {out}")


if __name__ == "__main__":
    main()
