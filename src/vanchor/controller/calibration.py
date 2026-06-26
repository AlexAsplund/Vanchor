"""Auto-calibration drive for the 'Init boat' wizard.

Runs a short, scripted sequence of maneuvers on the live runtime (sim or real
hardware), measures the boat's response, then runs the auto-tuner and applies the
result. Progress is exposed as a snapshot the UI streams via telemetry.

  straight  full ahead -> measure top speed + acceleration time constant
  coast     thrust off -> measure deceleration (drag) time constant
  turn      steer hard -> measure max turn rate + steering sign (bow vs stern)
  tuning    run the heading + anchor auto-tuners with the measured params
  done      apply measured params + tuned gains

It only issues ordinary manual commands, so it is hardware-agnostic. A safety
disclaimer is the UI's job (it drives the boat).
"""

from __future__ import annotations

import asyncio
import logging
import math

logger = logging.getLogger("vanchor.calibration")


class CalibrationRunner:
    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.running = False
        self.phase = "idle"
        self.progress = 0.0
        self.message = ""
        self.results: dict | None = None
        self._task: asyncio.Task | None = None
        self._cancel = False

    # -- public ----------------------------------------------------------- #
    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "phase": self.phase,
            "progress": round(self.progress, 3),
            "message": self.message,
            "results": self.results,
        }

    def start(self, mode: str = "quick") -> bool:
        if self.running:
            return False
        self._cancel = False
        self.results = None
        self._task = asyncio.ensure_future(self._run(mode))
        return True

    def cancel(self) -> None:
        self._cancel = True

    # -- internals -------------------------------------------------------- #
    def _set(self, phase: str, progress: float, message: str) -> None:
        self.phase, self.progress, self.message = phase, progress, message
        logger.info("calibration[%s] %.0f%% — %s", phase, progress * 100, message)

    def _manual(self, thrust: float, steering: float) -> None:
        self.runtime.handle_command({"type": "manual", "thrust": thrust, "steering": steering})

    async def _settle(self, seconds: float, phase: str, base: float, span: float) -> None:
        """Wait, updating progress, aborting early if cancelled."""
        steps = max(1, int(seconds / 0.1))
        for i in range(steps):
            if self._cancel:
                raise asyncio.CancelledError
            self.progress = base + span * (i + 1) / steps
            await asyncio.sleep(0.1)

    async def _run(self, mode: str) -> None:
        self.running = True
        st = self.runtime.state
        scale = 0.6 if mode == "quick" else 1.0
        try:
            # --- straight line: top speed + acceleration ----------------- #
            self._set("straight", 0.02, "Full ahead — measuring top speed…")
            self._manual(1.0, 0.0)
            t0 = 0.0
            v_samples: list[tuple[float, float]] = []
            h_samples: list[tuple[float, float]] = []
            # Long enough for a slow trolling-motor boat to actually reach steady
            # top speed (calibration is now the only source of max speed).
            steps = max(1, int(25 * scale / 0.1))
            for i in range(steps):
                if self._cancel:
                    raise asyncio.CancelledError
                v_samples.append((t0, _mps(st.sog_knots)))
                h_samples.append((t0, st.heading_deg))
                t0 += 0.1
                self.progress = 0.05 + 0.30 * (i + 1) / steps
                await asyncio.sleep(0.1)
            v_max = max(v for _, v in v_samples) if v_samples else 0.0
            accel_tau = _time_constant(v_samples, v_max, rising=True)
            # Residual straight-line yaw drift (deg/s), measured over the second
            # half once the boat is up to speed, with steering centred + the
            # current feed-forward active -> the leftover lateral-offset bias.
            yaw_drift_dps = _yaw_drift_rate(h_samples)
            # Estimate the feed-forward effectiveness: briefly drive straight with
            # the FF turned OFF, so the drift difference gives a direct gain
            # (deg/s per radian of FF deflection) to refine the trim with. This is
            # model-agnostic (no reliance on the full-swing turn rate, which is
            # degenerate for the small FF angle).
            self._set("yaw_ff", 0.35, "Checking off-centre thrust yaw…")
            ff_drift_off, ff_drift_on, ff_angle_used = await self._measure_ff(scale)
            self._manual(0.0, 0.0)

            # --- coast down: drag ---------------------------------------- #
            self._set("coast", 0.50, "Engine off — measuring drag…")
            self._manual(0.0, 0.0)
            d_samples = []
            t0 = 0.0
            steps = max(1, int(14 * scale / 0.1))
            for i in range(steps):
                if self._cancel:
                    raise asyncio.CancelledError
                d_samples.append((t0, _mps(st.sog_knots)))
                t0 += 0.1
                self.progress = 0.50 + 0.06 * (i + 1) / steps
                await asyncio.sleep(0.1)
            v0 = d_samples[0][1] if d_samples else v_max
            drag_tau = _time_constant(d_samples, v0, rising=False)

            # --- turn: max turn rate + steering sign --------------------- #
            self._set("turn", 0.56, "Hard turn — measuring steering response…")
            self._manual(0.5, 1.0)
            await asyncio.sleep(1.0)  # let it spin up
            headings = []
            t0 = 0.0
            steps = max(1, int(12 * scale / 0.1))
            for i in range(steps):
                if self._cancel:
                    raise asyncio.CancelledError
                headings.append((t0, st.heading_deg))
                t0 += 0.1
                self.progress = 0.58 + 0.17 * (i + 1) / steps
                await asyncio.sleep(0.1)
            turn_rate, sign = _turn_rate(headings)
            self._manual(0.0, 0.0)
            self.runtime.handle_command({"type": "stop"})

            # --- reverse: astern top speed + turn rate ------------------- #
            # The forward/reverse manoeuvre decision (modes.maneuver_to_bearing)
            # needs to know how much weaker the boat is astern, so profile it
            # rather than assuming the 0.6 default.
            self._set("reverse", 0.74, "Full astern — measuring reverse…")
            self._manual(-1.0, 0.0)
            rv_samples: list[tuple[float, float]] = []
            t0 = 0.0
            steps = max(1, int(12 * scale / 0.1))
            for i in range(steps):
                if self._cancel:
                    raise asyncio.CancelledError
                rv_samples.append((t0, _mps(st.sog_knots)))
                t0 += 0.1
                self.progress = 0.74 + 0.02 * (i + 1) / steps
                await asyncio.sleep(0.1)
            v_rev = max((v for _, v in rv_samples), default=0.0)
            # Reverse turn rate: steer hard while making way astern.
            self._manual(-0.5, 1.0)
            await asyncio.sleep(1.0)
            rev_headings: list[tuple[float, float]] = []
            t0 = 0.0
            steps = max(1, int(8 * scale / 0.1))
            for i in range(steps):
                if self._cancel:
                    raise asyncio.CancelledError
                rev_headings.append((t0, st.heading_deg))
                t0 += 0.1
                self.progress = 0.76 + 0.01 * (i + 1) / steps
                await asyncio.sleep(0.1)
            rev_turn_rate, _ = _turn_rate(rev_headings)
            self._manual(0.0, 0.0)
            self.runtime.handle_command({"type": "stop"})
            reverse_eff = (
                round(min(1.0, max(0.2, v_rev / v_max)), 3)
                if v_max > 0.05
                else self.runtime.config.boat.reverse_efficiency
            )

            # --- auto-tune ----------------------------------------------- #
            self._set("tuning", 0.78, "Auto-tuning heading + anchor gains…")
            tuned = await self._auto_tune()

            # --- thrust-yaw feed-forward trim ---------------------------- #
            # Refine the feed-forward angle so the boat tracks straight with the
            # steering centred. The two short straight runs (FF off vs FF on) give
            # the FF's effectiveness directly; from that gain, solve for the extra
            # angle that nulls the still-remaining FF-on drift (``ff_drift_on``).
            # ``yaw_drift_dps`` from the long phase is reported as the residual.
            bc = self.runtime.config.boat
            ff_trim_delta = _ff_trim_delta(
                ff_drift_off, ff_drift_on, ff_angle_used
            )
            new_trim = round(bc.thrust_yaw_ff_trim + ff_trim_delta, 5)

            # --- apply --------------------------------------------------- #
            self._set("done", 0.98, "Applying calibration…")
            self.runtime.update_boat({
                "max_speed_mps": round(v_max, 3) or self.runtime.config.boat.max_speed_mps,
                "max_turn_rate_deg": round(turn_rate, 2) or self.runtime.config.boat.max_turn_rate_deg,
                "reverse_efficiency": reverse_eff,
                "thrust_yaw_ff_trim": new_trim,
            })
            for job, params in tuned.items():
                try:
                    self.runtime.apply_tuned_gains(job, params)
                except Exception:
                    logger.exception("failed to apply tuned %s", job)

            self.results = {
                "max_speed_mps": round(v_max, 3),
                "accel_tau_s": round(accel_tau, 2),
                "drag_tau_s": round(drag_tau, 2),
                "max_turn_rate_dps": round(turn_rate, 2),
                "reverse_speed_mps": round(v_rev, 3),
                "reverse_efficiency": reverse_eff,
                "reverse_turn_rate_dps": round(abs(rev_turn_rate), 2),
                "steering_sign": sign,
                "yaw_drift_dps": round(yaw_drift_dps, 3),
                "thrust_yaw_ff_trim": new_trim,
                "tuned": {k: _round(v) for k, v in tuned.items()},
            }
            self._set("done", 1.0, "Calibration complete.")
        except asyncio.CancelledError:
            self._manual(0.0, 0.0)
            self.runtime.handle_command({"type": "stop"})
            self._set("idle", 0.0, "Calibration cancelled.")
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("calibration failed")
            self._set("error", self.progress, f"Calibration error: {exc}")
        finally:
            self.running = False

    async def _measure_ff(self, scale: float) -> tuple[float, float, float]:
        """Measure the straight-line yaw drift with the thrust-yaw feed-forward
        OFF then ON, full ahead, steering centred.

        Returns ``(drift_off_dps, drift_on_dps, ff_angle_rad)`` where
        ``ff_angle_rad`` is the (geometric) FF deflection that was active during
        the ON run. The drift difference over that deflection gives a direct
        deg/s-per-radian gain to refine the FF trim from. Restores the helm's FF
        to its starting value before returning.
        """
        helm = getattr(self.runtime.controller, "helm", None)
        bc = self.runtime.config.boat
        if helm is None or bc.max_steer_angle_deg <= 0:
            return 0.0, 0.0, 0.0
        ff_norm = getattr(helm, "thrust_yaw_ff", 0.0)
        ff_angle = ff_norm * math.radians(bc.max_steer_angle_deg)
        secs = max(3.0, 6.0 * scale)
        try:
            helm.thrust_yaw_ff = 0.0
            drift_off = await self._drift_run(secs, 0.36, 0.06)
            helm.thrust_yaw_ff = ff_norm
            drift_on = await self._drift_run(secs, 0.43, 0.06)
        finally:
            helm.thrust_yaw_ff = ff_norm
        return drift_off, drift_on, ff_angle

    async def _drift_run(self, seconds: float, base: float, span: float) -> float:
        """Drive full ahead with steering centred for ``seconds`` and return the
        net yaw rate (deg/s) over the latter half (past the spin-up transient)."""
        st = self.runtime.state
        self._manual(1.0, 0.0)
        await self._settle(seconds * 0.4, "ff", base, span * 0.4)  # let it settle
        samples: list[tuple[float, float]] = []
        steps = max(2, int(seconds * 0.6 / 0.1))
        t0 = 0.0
        for i in range(steps):
            if self._cancel:
                raise asyncio.CancelledError
            samples.append((t0, st.heading_deg))
            t0 += 0.1
            self.progress = base + span * 0.4 + span * 0.6 * (i + 1) / steps
            await asyncio.sleep(0.1)
        return _yaw_drift_rate(samples)

    async def _auto_tune(self) -> dict:
        """Run the heading + anchor tuners off the event loop."""
        from ..analysis.tuning import tune

        loop = asyncio.get_event_loop()
        out: dict = {}
        for job in ("heading", "anchor"):
            if self._cancel:
                break
            try:
                result = await loop.run_in_executor(None, lambda j=job: tune(j, max_evals=24))
                out[job] = result.tuned_params
            except Exception:
                logger.exception("auto-tune %s failed", job)
        return out


def _mps(knots: float) -> float:
    return knots * 0.514444


def _time_constant(samples, target, *, rising: bool) -> float:
    """First-order time constant: time to reach 63% of the step."""
    if not samples or target <= 1e-6:
        return 0.0
    if rising:
        thresh = 0.63 * target
        for t, v in samples:
            if v >= thresh:
                return max(0.1, t)
    else:
        thresh = 0.37 * target
        for t, v in samples:
            if v <= thresh:
                return max(0.1, t)
    return samples[-1][0]


def _turn_rate(headings) -> tuple[float, int]:
    """Max |deg/s| over the samples and the sign of the net change."""
    if len(headings) < 2:
        return 0.0, 1
    rates = []
    net = 0.0
    for (t0, h0), (t1, h1) in zip(headings, headings[1:]):
        d = ((h1 - h0 + 180) % 360) - 180
        dt = max(1e-3, t1 - t0)
        rates.append(d / dt)
        net += d
    peak = max(abs(r) for r in rates) if rates else 0.0
    return peak, (1 if net >= 0 else -1)


def _yaw_drift_rate(headings) -> float:
    """Net signed yaw rate (deg/s) over the second half of the samples.

    Uses only the later samples so the boat is up to speed and past the
    spin-up transient, giving the steady straight-line drift caused by a
    residual (uncompensated) lateral thruster offset. 0 when there isn't enough
    data.
    """
    if len(headings) < 4:
        return 0.0
    half = headings[len(headings) // 2:]
    (t0, h0) = half[0]
    (t1, h1) = half[-1]
    span = t1 - t0
    if span <= 1e-3:
        return 0.0
    net = ((h1 - h0 + 180.0) % 360.0) - 180.0
    return net / span


def _ff_trim_delta(drift_off_dps: float, drift_on_dps: float, ff_angle_rad: float) -> float:
    """Feed-forward angle correction (radians) that nulls the straight-line yaw.

    Two short straight runs give the yaw drift with the feed-forward OFF and ON.
    The FF deflection ``ff_angle_rad`` changed the drift by
    ``drift_on - drift_off``, so the local gain is ``g = (drift_on - drift_off)/
    ff_angle`` (deg/s per radian of FF angle). The extra angle that drives the
    remaining ``drift_on`` to zero is ``-drift_on / g``. The result is the same
    sign convention as the FF angle itself (geometric, helm re-applies
    ``steer_sign``). Returns 0 when there isn't enough signal to estimate a gain.
    """
    d_drift = drift_on_dps - drift_off_dps
    if abs(ff_angle_rad) < 1e-4 or abs(d_drift) < 1e-3:
        return 0.0
    gain = d_drift / ff_angle_rad  # deg/s per radian
    if abs(gain) < 1e-3:
        return 0.0
    delta = -drift_on_dps / gain
    # Clamp to a sane band so a noisy single shot can't swing the motor wildly.
    return max(-math.radians(30.0), min(math.radians(30.0), delta))


def _round(params: dict) -> dict:
    return {k: round(v, 4) if isinstance(v, float) else v for k, v in params.items()}
