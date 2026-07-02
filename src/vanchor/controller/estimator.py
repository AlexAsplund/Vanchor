"""Shared wind/current (environmental drift) estimator.

A small, dependency-free estimator of the boat's *environmental drift velocity*
-- the part of its ground motion NOT explained by its own thrust (the combined
set/leeway of current + wind). It used to live privately inside
:class:`~vanchor.controller.modes.AnchorHoldMode`, where it reset every time
Spot-Lock engaged, forcing the boat to relearn the environment over ~10 s on each
activation. It is now promoted to a **persistent service** owned by the
:class:`~vanchor.controller.controller.Controller`: fed every control tick in
*every* mode, so the estimate is always warm. Consumers read it off the shared
:class:`~vanchor.core.state.NavigationState`:

* Waypoint mode adds a bounded **crab-angle feed-forward** so the ground track
  holds against a beam set (tightening low-speed legs where the cross-track
  feedback gain saturates).
* Drift mode gets a real **drift axis** to reason about.
* Anchor-hold / Spot-Lock engages already knowing the drift (no relearn delay).

Method
------
Each tick we form the *drift sample* = observed GPS ground velocity minus the
boat's own thrust-driven velocity (a coarse ``thrust * boat_max_speed`` along the
heading), then low-pass it with a **dt-scaled EMA** so the smoothing time constant
is fixed in *seconds* regardless of the control rate::

    alpha = dt / (tau + dt)

Subtracting the propulsion term is what lets the estimate stay correct while the
boat is actively holding station (thrusting into the set with ~zero SOG): the
observed velocity is ~0, but the thrust term reveals the drift it is cancelling.
Thrust decoupling is deliberately crude (a single scalar boat speed); a
hydrodynamic through-water model is a future refinement.

Learning is **gated during sharp turns** (via the IMU yaw rate when present, else
the compass heading rate): mid-turn the thrust/heading geometry is changing too
fast for the coarse decoupling to be trustworthy. The estimate is *frozen*, never
reset, during a turn.

The estimator NEVER resets on a mode change -- that persistence is the whole
point.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..core.geo import angle_difference, knots_to_mps
from ..core.state import NavigationState


@dataclass
class EstimatorConfig:
    # EMA time constant (s) for the drift vector. Converted to a per-tick weight
    # ``dt / (tau + dt)`` so smoothing is frame-rate independent. ~10 s matches
    # the original AnchorHoldMode estimator.
    tau_s: float = 10.0
    # Coarse scalar used to decouple our own propulsion: thrust (-1..1) is assumed
    # to produce ``thrust * boat_max_speed_mps`` of ground velocity along the
    # heading. Only its magnitude matters for the decoupling geometry.
    boat_max_speed_mps: float = 1.6
    # Freeze learning while |yaw rate| exceeds this (deg/s). Prefers the IMU gyro
    # z-axis when available; otherwise the compass heading rate. Generous so that
    # station-keeping jitter doesn't starve the estimator of samples.
    max_turn_dps: float = 25.0
    # --- settling / confidence gates ------------------------------------- #
    # Enough accepted samples AND accumulated learning time before the estimate
    # is trusted by feed-forward consumers.
    min_settle_samples: int = 10
    min_settle_time_s: float = 8.0
    # EMA time constant (s) for the residual-spread tracker, and the spread (m/s)
    # above which the samples are too inconsistent to call "settled".
    spread_tau_s: float = 15.0
    max_spread_mps: float = 0.2


class WindCurrentEstimator:
    """Persistent estimator of the environmental drift velocity (world frame).

    Read-outs (also mirrored onto :class:`NavigationState` each ``update``):
      * ``drift_east`` / ``drift_north`` -- the drift velocity components (m/s),
      * ``drift_speed_mps`` -- its magnitude,
      * ``drift_dir_deg`` -- the compass direction the drift pushes *toward*,
      * ``settled`` -- enough consistent samples to trust the estimate,
      * ``confidence`` -- 0..1 blend of accumulated time and low spread.
    """

    def __init__(self, config: EstimatorConfig | None = None) -> None:
        self.config = config or EstimatorConfig()
        self.drift_east = 0.0
        self.drift_north = 0.0
        self.settled = False
        self.confidence = 0.0
        self._spread = 0.0
        self._n_samples = 0
        self._elapsed_s = 0.0
        self._prev_heading: float | None = None

    # -- read-outs -------------------------------------------------------- #
    @property
    def drift_speed_mps(self) -> float:
        return math.hypot(self.drift_east, self.drift_north)

    @property
    def drift_dir_deg(self) -> float:
        """Compass direction the drift pushes TOWARD (0..360)."""
        return math.degrees(math.atan2(self.drift_east, self.drift_north)) % 360.0

    def _yaw_rate_dps(self, state: NavigationState, dt: float) -> float:
        """Yaw rate for the turn gate: IMU gyro-z when present (immediate), else
        the compass heading rate (lags a frame but avoids the meaningless COG of a
        near-stationary boat)."""
        if state.imu is not None:
            return abs(state.imu.gz)
        if self._prev_heading is None or dt <= 0.0:
            return 0.0
        return abs(angle_difference(self._prev_heading, state.heading_deg)) / dt

    def update(self, state: NavigationState, dt: float) -> None:
        """Fold one control tick into the drift estimate and publish it on
        ``state``. Cheap and side-effect-only; safe to call every tick in any
        mode. NEVER resets the accumulated estimate."""
        cfg = self.config
        fix = state.fix
        if fix is None or dt <= 0.0:
            self._publish(state)
            return

        # Turn gate: freeze (don't learn) during sharp turns, but keep publishing
        # the frozen estimate so consumers still see the last good drift.
        yaw = self._yaw_rate_dps(state, dt)
        self._prev_heading = state.heading_deg
        if yaw > cfg.max_turn_dps:
            self._publish(state)
            return

        # Observed GPS ground velocity (world east/north, m/s).
        v = knots_to_mps(fix.sog_knots)
        cog = math.radians(fix.cog_deg)
        v_e, v_n = v * math.sin(cog), v * math.cos(cog)
        # Decouple our own propulsion: thrust drives ~thrust*max_speed along the
        # heading. What's left of the ground velocity is the environmental drift.
        h = math.radians(state.heading_deg)
        thr = state.motor_command.thrust
        int_e = thr * cfg.boat_max_speed_mps * math.sin(h)
        int_n = thr * cfg.boat_max_speed_mps * math.cos(h)
        sample_e = v_e - int_e
        sample_n = v_n - int_n

        # Residual of this sample vs the current estimate (pre-update) -> spread.
        dev = math.hypot(sample_e - self.drift_east, sample_n - self.drift_north)

        # dt-scaled EMA so the time constant is fixed in seconds.
        a = dt / (cfg.tau_s + dt)
        self.drift_east += a * (sample_e - self.drift_east)
        self.drift_north += a * (sample_n - self.drift_north)

        sa = dt / (cfg.spread_tau_s + dt)
        self._spread += sa * (dev - self._spread)
        self._n_samples += 1
        self._elapsed_s += dt

        self.settled = (
            self._n_samples >= cfg.min_settle_samples
            and self._elapsed_s >= cfg.min_settle_time_s
            and self._spread <= cfg.max_spread_mps
        )
        # Confidence: ramps with accumulated learning time, discounted by spread.
        time_conf = min(1.0, self._elapsed_s / cfg.min_settle_time_s)
        spread_conf = max(0.0, 1.0 - self._spread / cfg.max_spread_mps)
        self.confidence = time_conf * spread_conf
        self._publish(state)

    def _publish(self, state: NavigationState) -> None:
        state.est_drift_east = self.drift_east
        state.est_drift_north = self.drift_north
        state.est_drift_mps = self.drift_speed_mps
        state.est_drift_dir = self.drift_dir_deg
        state.est_drift_settled = self.settled
        state.est_drift_confidence = self.confidence


def crab_offset_deg(
    bearing_deg: float,
    drift_east: float,
    drift_north: float,
    water_speed_mps: float,
    *,
    max_crab_deg: float = 25.0,
    min_water_speed_mps: float = 0.2,
) -> float:
    """Signed heading offset (deg) to ADD to a leg ``bearing`` so the GROUND track
    holds against a set/leeway drift -- the classic crab angle.

    The boat must point slightly *into* the cross-track component of the drift so
    its through-water velocity plus the drift sums back onto the desired track. We
    take the drift component perpendicular (to starboard) of the bearing and solve
    ``crab = asin(v_cross / v_water)``, pointing to port when the drift pushes the
    boat to starboard (so the returned offset opposes it). Bounded to
    ``max_crab_deg`` and clamped so the ``asin`` stays valid.
    """
    b = math.radians(bearing_deg)
    # Unit vector to STARBOARD of the bearing, in (east, north).
    star_e, star_n = math.cos(b), -math.sin(b)
    cross_right = drift_east * star_e + drift_north * star_n  # + => pushed starboard
    v = max(min_water_speed_mps, water_speed_mps)
    ratio = max(-0.9, min(0.9, cross_right / v))
    crab = math.degrees(math.asin(ratio))
    crab = max(-max_crab_deg, min(max_crab_deg, crab))
    # Pushed to starboard (+cross_right) -> aim to port -> NEGATIVE heading offset.
    return -crab


__all__ = ["EstimatorConfig", "WindCurrentEstimator", "crab_offset_deg"]
