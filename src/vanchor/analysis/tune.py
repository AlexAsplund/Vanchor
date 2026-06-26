"""CLI for the auto-tuner.

    python -m vanchor.analysis.tune --list
    python -m vanchor.analysis.tune heading
    python -m vanchor.analysis.tune anchor --max-evals 120
    python -m vanchor.analysis.tune all
"""

from __future__ import annotations

import argparse

from .tuning import TUNING_JOBS, format_result, tune


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Vanchor-NG auto-assisted gain tuning")
    parser.add_argument("job", nargs="?", help="job name, or 'all' (see --list)")
    parser.add_argument("--list", action="store_true", help="list tuning jobs and exit")
    parser.add_argument("--max-evals", type=int, default=80)
    args = parser.parse_args(argv)

    if args.list or not args.job:
        print("Tuning jobs:")
        for name, builder in TUNING_JOBS.items():
            print(f"  {name:<10} {builder().description}")
        print("  all        run every job")
        return

    jobs = list(TUNING_JOBS) if args.job == "all" else [args.job]
    for name in jobs:
        result = tune(name, max_evals=args.max_evals)
        print(format_result(result))
        print()


if __name__ == "__main__":
    main()
