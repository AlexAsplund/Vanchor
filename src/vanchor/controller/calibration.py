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
import json
import logging
import math
import os
from dataclasses import dataclass

import numpy as np

from ..core.prefs import _atomic_write_json

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
                # ignore[misc]: mypy can't infer the default-arg lambda's type.
                result = await loop.run_in_executor(None, lambda j=job: tune(j, max_evals=24))  # type: ignore[misc]
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


# ===================================================================== #
#  Magnetometer hard/soft-iron calibration (roadmap #41)
# ===================================================================== #
#
# A magnetometer reads the local field distorted by the vessel's own iron:
#
#   * **Hard iron** — a fixed additive bias (permanent magnets, DC wiring). It
#     shifts the *centre* of the sphere the readings trace as the boat rotates.
#   * **Soft iron** — a linear distortion (ferrous mass reshaping the field). It
#     turns that sphere into a tilted ellipsoid (scale + skew).
#
# Spin the boat through (ideally) a full circle and the samples land on an
# ellipsoid; fit it, and the correction is
#
#     corrected = W @ (raw - offset)
#
# where ``offset`` is the hard-iron bias (the learned COMPASS OFFSET we persist)
# and ``W`` is the soft-iron matrix that maps the ellipsoid back onto a sphere of
# the fitted field strength. Applied per-sample, ``atan2`` of the corrected
# horizontal components then yields an undistorted magnetic heading.
#
# The fit is a plain algebraic least-squares ellipsoid fit (numpy only). It is
# fully unit-testable with synthetic distorted data where the true offset/scale
# are known; the *live sample capture* is the only hardware-dependent part and is
# driven through an injected provider callable so it runs against a fake in tests.

# Minimum distinct samples for a trustworthy ellipsoid fit. Nine free parameters
# means nine is the algebraic floor; we demand a healthy margin so noise averages
# out and a near-degenerate (e.g. barely-rotated) capture is rejected.
_MAG_MIN_SAMPLES: int = 30

# Heading-coverage bins (horizontal angle of each sample). Full 360° rotation
# lights all of them; a partial sweep leaves gaps -> lower reported coverage.
_MAG_COVERAGE_BINS: int = 12


@dataclass
class MagCalibration:
    """A fitted hard/soft-iron magnetometer correction.

    ``offset`` is the hard-iron bias (the persisted compass offset); ``matrix``
    is the 3x3 soft-iron correction. Apply with :meth:`apply`::

        corrected = matrix @ (raw - offset)

    ``field_strength`` is the fitted sphere radius the correction maps onto,
    ``residual`` the RMS of ``|corrected| / field_strength - 1`` over the fit
    samples (0 = perfect), and ``quality`` a friendly ``max(0, 1 - residual)``
    score in ``[0, 1]``.
    """

    offset: tuple[float, float, float]
    matrix: tuple[tuple[float, float, float], ...]
    field_strength: float
    residual: float
    quality: float
    n_samples: int
    coverage: float = 0.0

    def apply(self, sample) -> tuple[float, float, float]:
        """Correct one raw ``(x, y, z)`` reading -> ``(x, y, z)`` on the sphere."""
        w = np.asarray(self.matrix, dtype=float)
        o = np.asarray(self.offset, dtype=float)
        v = w @ (np.asarray(sample, dtype=float) - o)
        return (float(v[0]), float(v[1]), float(v[2]))

    def heading_deg(self, sample) -> float:
        """Magnetic heading (deg, 0..360) of a raw ``(x, y, z)`` magnetometer
        reading, AFTER the hard/soft-iron correction is applied.

        This is the piece the raw-magnetometer heading path is missing (#41): a
        stored calibration only helps if it is applied to the raw vector *before*
        the heading is derived. It first maps the reading back onto the field
        sphere with :meth:`apply` (removing the vessel's hard- and soft-iron
        distortion), then takes the compass bearing of the corrected horizontal
        components.

        Convention: the sensor's body ``x`` axis points to the bow and ``y`` to
        starboard, so the bearing of the (magnetic-north-pointing) field relative
        to the bow is ``atan2(-y, x)`` -- i.e. as the boat turns to starboard the
        field appears to rotate to port. Declination (magnetic -> true) is applied
        separately downstream, exactly as for an HDM/HDG sentence. Tilt is not
        compensated here (no accelerometer input); this assumes a roughly level
        sensor, matching the rest of the current heading path.
        """
        cx, cy, _cz = self.apply(sample)
        return math.degrees(math.atan2(-cy, cx)) % 360.0

    def to_dict(self) -> dict:
        return {
            "offset": [float(x) for x in self.offset],
            "matrix": [[float(x) for x in row] for row in self.matrix],
            "field_strength": float(self.field_strength),
            "residual": float(self.residual),
            "quality": float(self.quality),
            "n_samples": int(self.n_samples),
            "coverage": float(self.coverage),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MagCalibration | None":
        """Rebuild from a persisted dict, or ``None`` if it is malformed."""
        try:
            offset = tuple(float(x) for x in d["offset"])
            matrix = tuple(tuple(float(x) for x in row) for row in d["matrix"])
            if len(offset) != 3 or len(matrix) != 3 or any(len(r) != 3 for r in matrix):
                return None
            cal = cls(
                offset=offset,
                matrix=matrix,  # type: ignore[arg-type]  # 3x3 validated by the len() guard above
                field_strength=float(d.get("field_strength", 1.0)),
                residual=float(d.get("residual", 0.0)),
                quality=float(d.get("quality", 0.0)),
                n_samples=int(d.get("n_samples", 0)),
                coverage=float(d.get("coverage", 0.0)),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if not (np.all(np.isfinite(cal.offset)) and np.all(np.isfinite(cal.matrix))):
            return None
        return cal


def fit_hard_soft_iron(samples) -> MagCalibration:
    """Least-squares hard/soft-iron fit of ``samples`` (an ``(N, 3)`` array).

    Fits the general ellipsoid ``[x y z 1] A [x y z 1]^T = 0`` the readings trace
    as the boat rotates, then extracts the hard-iron ``offset`` (its centre) and a
    soft-iron ``matrix`` that maps the ellipsoid back onto a sphere of the mean
    semi-axis (the fitted field strength).

    Raises :class:`ValueError` for a degenerate capture: too few samples, a
    rank-deficient / non-finite system (e.g. a coplanar or barely-rotated sweep),
    or a non-ellipsoidal solution (the fitted quadric isn't positive-definite).
    """
    pts = np.asarray(samples, dtype=float)
    if pts.size == 0:
        pts = pts.reshape(0, 3)  # empty capture -> falls through to the count floor
    if pts.ndim != 2 or pts.shape[1] != 3:
        raise ValueError("magnetometer samples must be an (N, 3) array")
    # Drop exact-duplicate rows so a stalled/repeating sensor can't look like a
    # rich capture, then apply the sample-count floor.
    pts = np.unique(pts, axis=0)
    n = pts.shape[0]
    if n < _MAG_MIN_SAMPLES:
        raise ValueError(
            f"need at least {_MAG_MIN_SAMPLES} distinct samples, got {n}"
        )
    if not np.all(np.isfinite(pts)):
        raise ValueError("magnetometer samples contain non-finite values")

    x, y, z = pts[:, 0], pts[:, 1], pts[:, 2]
    # Design matrix for a x^2 + b y^2 + c z^2 + 2(d yz + e xz + f xy)
    #                       + 2(g x + h y + i z) = 1
    design = np.column_stack(
        [x * x, y * y, z * z, 2 * y * z, 2 * x * z, 2 * x * y, 2 * x, 2 * y, 2 * z]
    )
    rhs = np.ones(n)
    try:
        v, _res, rank, _sv = np.linalg.lstsq(design, rhs, rcond=None)
    except np.linalg.LinAlgError as exc:  # pragma: no cover - numeric edge
        raise ValueError(f"ellipsoid fit failed: {exc}") from exc
    if rank < 9 or not np.all(np.isfinite(v)):
        raise ValueError("degenerate magnetometer capture (rank-deficient fit)")

    a4 = np.array(
        [
            [v[0], v[3], v[4], v[6]],
            [v[3], v[1], v[5], v[7]],
            [v[4], v[5], v[2], v[8]],
            [v[6], v[7], v[8], -1.0],
        ]
    )
    a3 = a4[:3, :3]
    try:
        center = np.linalg.solve(-a3, v[6:9])
    except np.linalg.LinAlgError as exc:
        raise ValueError(f"ellipsoid has no finite centre: {exc}") from exc
    if not np.all(np.isfinite(center)):
        raise ValueError("degenerate magnetometer capture (no finite centre)")

    # Translate the quadric to the centre, then normalise so the ellipsoid form
    # is ``(p - c)^T M (p - c) = 1``.
    t = np.eye(4)
    t[3, :3] = center
    r = t @ a4 @ t.T
    if abs(r[3, 3]) < 1e-12:
        raise ValueError("degenerate magnetometer capture (singular quadric)")
    m = r[:3, :3] / -r[3, 3]
    # Symmetrise to kill round-off asymmetry before eigendecomposition.
    m = 0.5 * (m + m.T)
    evals, evecs = np.linalg.eigh(m)
    if not np.all(evals > 0) or not np.all(np.isfinite(evals)):
        raise ValueError("degenerate magnetometer capture (not an ellipsoid)")

    radii = 1.0 / np.sqrt(evals)          # semi-axes of the fitted ellipsoid
    field = float(np.mean(radii))          # target sphere radius
    # W = V diag(1/radii) V^T maps the ellipsoid onto the UNIT sphere; scale by
    # ``field`` so corrected magnitudes sit at the mean field strength.
    w = evecs @ np.diag(1.0 / radii) @ evecs.T * field

    offset = (float(center[0]), float(center[1]), float(center[2]))
    corrected = (w @ (pts - center).T).T
    mag = np.linalg.norm(corrected, axis=1)
    residual = float(np.sqrt(np.mean((mag / field - 1.0) ** 2)))
    quality = max(0.0, 1.0 - residual)
    return MagCalibration(
        offset=offset,
        # 3x3 by construction; mypy sees a variable-length tuple generator.
        matrix=tuple(tuple(float(x) for x in row) for row in w),  # type: ignore[misc]
        field_strength=field,
        residual=residual,
        quality=quality,
        n_samples=n,
        coverage=_coverage_fraction(pts, offset),
    )


def _coverage_fraction(pts, offset) -> float:
    """Fraction of the 12 horizontal heading bins the samples visited.

    A full circular sweep lights every bin (1.0); a partial rotation leaves gaps.
    Used only as guidance for the operator — the fit itself already rejects a
    truly degenerate capture."""
    o = np.asarray(offset, dtype=float)
    dx = np.asarray(pts, dtype=float)[:, 0] - o[0]
    dy = np.asarray(pts, dtype=float)[:, 1] - o[1]
    ang = np.arctan2(dy, dx)  # [-pi, pi]
    bins = np.floor((ang + math.pi) / (2 * math.pi) * _MAG_COVERAGE_BINS)
    bins = np.clip(bins, 0, _MAG_COVERAGE_BINS - 1).astype(int)
    return float(len(np.unique(bins)) / _MAG_COVERAGE_BINS)


class MagCalibrationStore:
    """Persists the learned magnetometer calibration to
    ``<data_dir>/mag_calibration.json`` and reloads it on startup so the compass
    offset survives a restart.

    Uses the same atomic ``tmp + os.replace`` write as the other stores, so a
    crash mid-write can never leave a half-written file. A malformed or missing
    file loads as "no calibration" (``calibration is None``) rather than raising.
    """

    def __init__(self, data_dir: str) -> None:
        self._dir = data_dir
        self._path = os.path.join(data_dir, "mag_calibration.json")
        self.calibration: MagCalibration | None = None
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self.calibration = MagCalibration.from_dict(data)

    def save(self, calibration: MagCalibration) -> None:
        """Persist ``calibration`` atomically and keep it as the live value."""
        self.calibration = calibration
        _atomic_write_json(self._path, calibration.to_dict())

    def clear(self) -> None:
        """Forget the calibration (and delete the file) — revert to raw readings."""
        self.calibration = None
        try:
            os.remove(self._path)
        except OSError:
            pass


class MagCalibrationRunner:
    """Interactive magnetometer-calibration session (roadmap #41).

    Collects magnetometer samples while the operator rotates the boat through a
    circle, then fits + persists the hard/soft-iron correction. Samples come from
    an injected ``sample_provider`` — a ``() -> (x, y, z) | None`` callable — so
    the capture is testable against a fake (the live wiring reads the AHRS
    magnetometer from the runtime). A background poll loop accumulates samples at
    ``poll_hz`` between :meth:`start` and :meth:`stop`; :meth:`add_sample` feeds
    one directly (used by tests and any push source).

    ``stop`` runs the fit: on success it saves to the store and returns the fit
    quality; on a degenerate capture it returns ``ok=False`` with the reason and
    leaves any previously-saved calibration untouched.
    """

    def __init__(
        self,
        sample_provider,
        store: MagCalibrationStore,
        *,
        poll_hz: float = 10.0,
        max_samples: int = 3000,
        min_gap: float = 1e-6,
    ) -> None:
        self._provider = sample_provider
        self.store = store
        self.poll_hz = max(0.5, poll_hz)
        self.max_samples = max_samples
        self._min_gap = min_gap
        self.running = False
        self.phase = "idle"
        self.message = ""
        self.samples: list[tuple[float, float, float]] = []
        self.result: MagCalibration | None = None
        self._task: asyncio.Task | None = None

    # -- public ----------------------------------------------------------- #
    def snapshot(self) -> dict:
        """Live status for the UI: progress + the last/persisted fit quality."""
        saved = self.store.calibration
        return {
            "running": self.running,
            "phase": self.phase,
            "message": self.message,
            "n_samples": len(self.samples),
            "coverage": round(self._live_coverage(), 3),
            "min_samples": _MAG_MIN_SAMPLES,
            "result": self.result.to_dict() if self.result else None,
            "saved": saved.to_dict() if saved else None,
        }

    def start(self) -> bool:
        """Begin a capture (clears any prior in-progress samples). Returns False
        if a capture is already running."""
        if self.running:
            return False
        self.samples = []
        self.result = None
        self.running = True
        self.phase = "collecting"
        self.message = "Slowly rotate the boat through a full circle…"
        # Poll the live provider on a background task when an event loop is
        # running (the server). With no running loop (sync tests / a push-only
        # source) the capture is fed via add_sample instead.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._task = None
        else:
            self._task = asyncio.ensure_future(self._run())
        return True

    def add_sample(self, x: float, y: float, z: float) -> bool:
        """Record one raw sample. Ignored when not collecting, at the cap, or when
        it duplicates the previous point (a stalled sensor). Returns True if kept."""
        if not self.running or len(self.samples) >= self.max_samples:
            return False
        p = (float(x), float(y), float(z))
        if not all(math.isfinite(c) for c in p):
            return False
        if self.samples:
            last = self.samples[-1]
            if math.dist(p, last) < self._min_gap:
                return False
        self.samples.append(p)
        return True

    def cancel(self) -> dict:
        """Abort the capture WITHOUT fitting; keeps any saved calibration."""
        self._stop_task()
        self.running = False
        self.phase = "idle"
        self.message = "Calibration cancelled."
        self.samples = []
        self.result = None
        return self.snapshot()

    def stop(self) -> dict:
        """Finish the capture: fit + persist, and return the result.

        On success ``{ok: True, result: {...quality...}}``; on a degenerate
        capture ``{ok: False, message: <reason>}`` with the saved calibration
        left untouched."""
        self._stop_task()
        self.running = False
        try:
            cal = fit_hard_soft_iron(self.samples)
        except ValueError as exc:
            self.phase = "error"
            self.message = str(exc)
            out = self.snapshot()
            out["ok"] = False
            out["message"] = str(exc)
            return out
        self.result = cal
        self.store.save(cal)
        self.phase = "done"
        self.message = (
            f"Calibrated — quality {cal.quality:.2f}, "
            f"coverage {cal.coverage * 100:.0f}%."
        )
        out = self.snapshot()
        out["ok"] = True
        return out

    # -- internals -------------------------------------------------------- #
    def _stop_task(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _live_coverage(self) -> float:
        if len(self.samples) < 3:
            return 0.0
        pts = np.asarray(self.samples, dtype=float)
        return _coverage_fraction(pts, pts.mean(axis=0))

    async def _run(self) -> None:
        period = 1.0 / self.poll_hz
        try:
            while self.running and len(self.samples) < self.max_samples:
                try:
                    sample = self._provider()
                except Exception:  # noqa: BLE001 - a flaky provider must not kill us
                    logger.debug("mag sample provider raised", exc_info=True)
                    sample = None
                if sample is not None:
                    try:
                        self.add_sample(sample[0], sample[1], sample[2])
                    except (TypeError, IndexError, ValueError):
                        logger.debug("bad mag sample %r", sample)
                await asyncio.sleep(period)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            logger.exception("mag calibration capture loop error")
