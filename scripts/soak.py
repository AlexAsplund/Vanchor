#!/usr/bin/env python3
"""Long-running headless sim soak (roadmap #40, soak half).

Drives a full-sim :class:`~vanchor.app.Runtime` (the real navigator + controller
+ Fossen simulator + all the background safety tasks) in-process for a
configurable wall-clock duration while continuously:

  * **churning modes** -- cycling manual / heading-hold / anchor / waypoint /
    drift / orbit / trolling / stop commands, the way an operator jabbing the UI
    would, so mode transitions and every guided law get exercised back to back;
  * **injecting link drops** -- forcing the lost-connection failsafe (#64) to
    engage while underway and then reconnecting, over and over.

Throughout, it asserts the invariants that matter for an unattended boat:

  * **no crash** -- none of the runtime's background tasks (controller loop,
    simulator loop, safety supervisor) died with an exception;
  * **no stuck motor** -- the normalized thrust/steering stay clamped in
    [-1, 1], and every time we command STOP the motor actually goes quiet;
  * **bounded memory** -- resident set size never grows past a generous cap over
    its post-warmup baseline (catches an unbounded ring/buffer/task leak).

Default duration is short (a handful of seconds) so it runs in CI on every
schedule; pass ``--duration`` (seconds) for a real overnight soak. The sim
physics run faster than wall-clock via ``--time-scale`` so even a short soak
covers a lot of simulated travel and many mode transitions.

Usage::

    python scripts/soak.py                     # ~15 s smoke soak
    python scripts/soak.py --duration 3600     # 1 h soak
    python scripts/soak.py --duration 60 --time-scale 20 -v

Exit code is non-zero if any invariant is violated.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import random
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# Make ``vanchor`` importable when run as a bare script.
_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from vanchor.app import Runtime  # noqa: E402
from vanchor.core.config import load  # noqa: E402
from vanchor.core.geo import destination_point  # noqa: E402
from vanchor.core.models import GeoPoint  # noqa: E402


# --------------------------------------------------------------------------- #
# Memory sampling (no third-party deps)
# --------------------------------------------------------------------------- #
def _rss_mb() -> float:
    """Current resident set size in MiB, or nan if unavailable."""
    try:
        with open(f"/proc/{os.getpid()}/statm") as fh:
            resident_pages = int(fh.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        try:
            import resource

            # ru_maxrss is KiB on Linux, bytes on macOS. Peak, not current.
            kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return kb / 1024 if kb > 1 << 20 else kb / 1024
        except Exception:
            return float("nan")


# --------------------------------------------------------------------------- #
# Result / failure model
# --------------------------------------------------------------------------- #
class SoakFailure(AssertionError):
    """A soak invariant was violated."""


@dataclass
class SoakResult:
    duration_s: float
    commands_issued: int = 0
    link_drops: int = 0
    failsafe_engagements: int = 0
    stop_checks: int = 0
    rss_baseline_mb: float = float("nan")
    rss_peak_mb: float = float("nan")
    rss_final_mb: float = float("nan")
    max_abs_thrust: float = 0.0
    max_abs_steering: float = 0.0
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def summary(self) -> str:
        g = self.rss_peak_mb - self.rss_baseline_mb
        lines = [
            "=== soak summary ===",
            f"  duration            : {self.duration_s:.1f} s",
            f"  commands issued     : {self.commands_issued}",
            f"  link drops injected : {self.link_drops} "
            f"(failsafe engaged {self.failsafe_engagements}x)",
            f"  STOP checks         : {self.stop_checks}",
            f"  motor peak |thrust| : {self.max_abs_thrust:.3f}   "
            f"|steering| {self.max_abs_steering:.3f}",
            f"  RSS base/peak/final : {self.rss_baseline_mb:.1f} / "
            f"{self.rss_peak_mb:.1f} / {self.rss_final_mb:.1f} MiB "
            f"(growth {g:+.1f})",
            f"  result              : {'PASS' if self.ok else 'FAIL'}",
        ]
        for v in self.violations:
            lines.append(f"    ! {v}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Mode-churn command generator
# --------------------------------------------------------------------------- #
def _churn_command(rt: Runtime, rng: random.Random) -> dict:
    """Build one random mode-change command, anchored to the boat's position."""
    pos = rt.state.position or GeoPoint(rt.config.sim.start_lat, rt.config.sim.start_lon)
    hdg = rng.uniform(0.0, 360.0)
    near = destination_point(pos, rng.uniform(20.0, 80.0), rng.uniform(0.0, 360.0))
    far = destination_point(near, rng.uniform(20.0, 80.0), rng.uniform(0.0, 360.0))
    choices: list[dict] = [
        {"type": "manual", "thrust": rng.uniform(-0.5, 1.0), "steering": rng.uniform(-1.0, 1.0)},
        {"type": "heading_hold", "heading": hdg, "throttle": rng.uniform(0.2, 0.8)},
        {"type": "anchor_hold", "radius_m": rng.uniform(2.0, 8.0)},
        {
            "type": "goto",
            "throttle": rng.uniform(0.4, 0.9),
            "waypoints": [
                {"name": "A", "lat": near.lat, "lon": near.lon},
                {"name": "B", "lat": far.lat, "lon": far.lon},
            ],
        },
        {"type": "drift", "heading": hdg, "knots": rng.uniform(0.2, 1.0)},
        {
            "type": "orbit",
            "center_lat": pos.lat,
            "center_lon": pos.lon,
            "radius_m": rng.uniform(15.0, 40.0),
            "direction": rng.choice(["cw", "ccw"]),
            "speed_knots": rng.uniform(0.5, 1.5),
        },
        {
            "type": "trolling",
            "base_heading": hdg,
            "amplitude_deg": rng.uniform(10.0, 30.0),
            "period_s": rng.uniform(10.0, 30.0),
            "speed_knots": rng.uniform(0.5, 1.5),
        },
        {"type": "set_environment", "wind_speed": rng.uniform(0.0, 8.0), "wind_dir": hdg},
    ]
    return rng.choice(choices)


# --------------------------------------------------------------------------- #
# Soak driver
# --------------------------------------------------------------------------- #
async def soak(
    *,
    duration_s: float,
    time_scale: float,
    churn_period_s: float,
    drop_period_s: float,
    seed: int,
    verbose: bool,
) -> SoakResult:
    rng = random.Random(seed)
    result = SoakResult(duration_s=duration_s)

    # Full-sim config in an isolated temp data dir (so the soak never touches the
    # user's real depth map / debug recordings). Physics run faster than wall
    # clock so a short soak still covers a lot of travel + transitions.
    with tempfile.TemporaryDirectory(prefix="vanchor_soak_") as tmp:
        cfg = load(None)
        cfg.data_dir = tmp
        cfg.sim.time_scale = time_scale
        cfg.log_level = "WARNING"  # keep the soak console readable

        rt = Runtime(cfg)
        await rt.start()
        try:
            await _run_loop(
                rt,
                rng,
                result,
                duration_s=duration_s,
                churn_period_s=churn_period_s,
                drop_period_s=drop_period_s,
                verbose=verbose,
            )
        finally:
            # Final STOP + settle, then assert the motor actually goes quiet.
            rt.handle_command({"type": "stop"})
            result.stop_checks += 1
            await _await_stopped(rt, result, "final")
            _check_tasks(rt, result)
            with contextlib.suppress(Exception):
                await rt.stop()

    return result


async def _run_loop(
    rt: Runtime,
    rng: random.Random,
    result: SoakResult,
    *,
    duration_s: float,
    churn_period_s: float,
    drop_period_s: float,
    verbose: bool,
) -> None:
    tick = 0.25  # health-check cadence (wall seconds)
    start = time.monotonic()
    warmup = min(2.0, duration_s / 4.0)  # let RSS settle before baselining
    next_churn = warmup
    next_drop = warmup + drop_period_s / 2.0
    baseline_set = False

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= duration_s:
            break

        # --- mode churn ------------------------------------------------- #
        if elapsed >= next_churn:
            cmd = _churn_command(rt, rng)
            rt.handle_command(cmd)
            result.commands_issued += 1
            if verbose:
                print(f"[{elapsed:6.1f}s] churn -> {cmd['type']:<14} mode={rt.state.mode.value}")
            next_churn += churn_period_s

        # --- injected link drop while underway -------------------------- #
        if elapsed >= next_drop:
            _inject_link_drop(rt, result, elapsed, verbose)
            next_drop += drop_period_s

        # --- periodic STOP / stuck-motor check -------------------------- #
        # Occasionally slam STOP and verify the motor obeys within a settle.
        if result.commands_issued and result.commands_issued % 7 == 0:
            rt.handle_command({"type": "stop"})
            result.stop_checks += 1
            await _await_stopped(rt, result, f"t={elapsed:.1f}s")
            await asyncio.sleep(tick)  # pace the loop until the next churn
            continue

        # --- health checks ---------------------------------------------- #
        _check_tasks(rt, result)
        _track_motor(rt, result)

        rss = _rss_mb()
        if not baseline_set and elapsed >= warmup:
            result.rss_baseline_mb = rss
            result.rss_peak_mb = rss
            baseline_set = True
        if baseline_set:
            result.rss_peak_mb = max(result.rss_peak_mb, rss)
        result.rss_final_mb = rss

        await asyncio.sleep(tick)

    # Bounded-memory verdict (generous cap; a real leak balloons well past this).
    _check_memory(result, cap_growth_mb=120.0)


def _inject_link_drop(rt: Runtime, result: SoakResult, elapsed: float, verbose: bool) -> None:
    """Put the boat underway, then force a lost-link failsafe and reconnect.

    Uses ``evaluate_link_failsafe(now=...)`` with an explicit forced clock so the
    drop fires deterministically regardless of the (20 s) real timeout -- we are
    exercising the failsafe *path*, not waiting out the wall clock.
    """
    # Ensure we're in an underway (guided) mode so the failsafe has teeth.
    rt.handle_command({"type": "heading_hold", "heading": 90.0, "throttle": 0.5})
    rt.client_connected()      # a UI client is present...
    rt.client_disconnected()   # ...and now drops (stamps _last_client_seen)
    result.link_drops += 1

    seen = rt._last_client_seen or rt._mono_fn()
    forced_now = seen + rt.config.safety.link_loss_timeout_s + 1.0
    engaged = rt.evaluate_link_failsafe(now=forced_now)
    if engaged:
        result.failsafe_engagements += 1
    if verbose:
        print(f"[{elapsed:6.1f}s] link drop -> failsafe engaged={engaged} mode={rt.state.mode.value}")
    # Reconnect clears the failsafe latch, then drop again so the client count
    # returns to zero (leaving it >0 would suppress the next drop's failsafe).
    rt.client_connected()
    rt.client_disconnected()


def _track_motor(rt: Runtime, result: SoakResult) -> None:
    cmd = rt.state.motor_command
    result.max_abs_thrust = max(result.max_abs_thrust, abs(cmd.thrust))
    result.max_abs_steering = max(result.max_abs_steering, abs(cmd.steering))
    # Clamp invariant: a runaway command means a broken governor.
    if abs(cmd.thrust) > 1.0 + 1e-6 or abs(cmd.steering) > 1.0 + 1e-6:
        _add(result, f"motor command out of range: thrust={cmd.thrust} steering={cmd.steering}")


async def _await_stopped(
    rt: Runtime, result: SoakResult, when: str, *, timeout_s: float = 3.0
) -> None:
    """After a STOP, the motor must go quiet — but not necessarily instantly.

    The safety governor slew-limits thrust (``max_thrust_slew_per_s``, 1.0/s by
    default), so a STOP issued while the prop is near full thrust ramps to zero
    over up to ~1 s rather than snapping — that ramp is the intended
    prop-protection behaviour, not a stuck motor.  Poll until the motor settles
    within the epsilon and fail only if it never does within ``timeout_s`` (a
    genuinely stuck motor).  Returns as soon as it is quiet, so the common case
    stays fast.
    """
    deadline = time.monotonic() + timeout_s
    thr = rt.state.motor_command.thrust
    while True:
        thr = rt.state.motor_command.thrust
        if abs(thr) <= 0.02:
            return
        if time.monotonic() >= deadline:
            _add(
                result,
                f"stuck motor after STOP ({when}): thrust={thr:.3f} "
                f"(never settled within {timeout_s:.1f}s)",
            )
            return
        await asyncio.sleep(0.1)


def _check_tasks(rt: Runtime, result: SoakResult) -> None:
    """Any background task that finished with an exception is a crash."""
    for task in list(rt._tasks):
        if task.done() and not task.cancelled():
            exc = task.exception()
            if exc is not None:
                _add(result, f"background task crashed: {exc!r}")


def _check_memory(result: SoakResult, *, cap_growth_mb: float) -> None:
    growth = result.rss_peak_mb - result.rss_baseline_mb
    if growth == growth and growth > cap_growth_mb:  # NaN-safe
        _add(
            result,
            f"RSS grew {growth:.1f} MiB (> {cap_growth_mb:.0f} cap) "
            f"[{result.rss_baseline_mb:.1f} -> {result.rss_peak_mb:.1f}]",
        )


def _add(result: SoakResult, msg: str) -> None:
    if msg not in result.violations:
        result.violations.append(msg)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--duration", type=float, default=15.0, help="wall-clock seconds (default 15)")
    parser.add_argument("--time-scale", type=float, default=10.0, help="sim physics speed-up (default 10x)")
    parser.add_argument("--churn-period", type=float, default=1.0, help="seconds between mode changes")
    parser.add_argument("--drop-period", type=float, default=3.0, help="seconds between injected link drops")
    parser.add_argument("--seed", type=int, default=1234, help="RNG seed for the command churn")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    result = asyncio.run(
        soak(
            duration_s=args.duration,
            time_scale=args.time_scale,
            churn_period_s=args.churn_period,
            drop_period_s=args.drop_period,
            seed=args.seed,
            verbose=args.verbose,
        )
    )
    print(result.summary())
    if not result.ok:
        raise SoakFailure(f"{len(result.violations)} soak invariant(s) violated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
