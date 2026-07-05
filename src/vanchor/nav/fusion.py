"""GNSS/INS complementary fusion: a smooth, high-rate navigation state.

This is a small, loosely-coupled sensor-fusion filter that blends what a
u-blox M9N receiver and an HWT901B AHRS give us into one coherent state:

* the M9N supplies position + a clean NED ground-velocity vector
  (``vel_n``/``vel_e`` m/s) + accuracy estimates at ~10 Hz;
* the HWT901B supplies a real yaw-rate (deg/s) and a fused magnetic heading
  at a much higher rate.

Fusing them yields a state that (a) has a genuine yaw-rate sensor, (b) has a
clean low-speed velocity vector, (c) knows the boat's crab/leeway (the angle
between where the bow points and where it actually travels) and (d) can
dead-reckon through brief GPS gaps.

Design rules that make this reusable and testable:

* **Pure computation, no I/O.** Nothing here reads a clock or a device; every
  timestamp (``now``) and interval (``dt``) is passed in by the caller, which
  injects a monotonic clock. This keeps the filter deterministic.
* **Graceful with partial sensors.** Any subset of {IMU, compass, GPS} may be
  present. Missing sensors degrade the state (fields become ``None``) rather
  than raising.

The heading channel is a classic complementary filter: the gyro integrates the
heading at high rate (low-latency, drifts slowly) and each compass update nudges
it back toward the absolute magnetic heading (no drift, noisier). The velocity
channel is a first-order low-pass toward the GPS ground velocity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..core.geo import (
    EARTH_RADIUS_M,
    angle_difference,
    normalize_deg,
    offset_meters,
)
from ..core.models import GeoPoint


@dataclass(frozen=True)
class FusionState:
    """A snapshot of the fused navigation state.

    Every field is optional: it is ``None`` until the filter has the sensor(s)
    needed to compute it, so a consumer can treat this as a best-effort state.

    ``crab_deg`` is the signed angle from heading to course
    (``angle_difference(heading, course)``): **positive means the boat's track
    is to starboard of where the bow points** -- i.e. it is being set/slipping
    to the right. It is ``None`` below ``crab_min_sog_mps`` (course, hence crab,
    is meaningless when nearly stationary) or when the heading is unknown.
    """

    heading_deg: float | None
    yaw_rate_dps: float | None
    ground_vel_n_mps: float | None
    ground_vel_e_mps: float | None
    vertical_vel_mps: float | None
    sog_mps: float | None
    crab_deg: float | None
    dead_reckoning: bool
    position: GeoPoint | None
    # True when the ground velocity came from a MEASURED velocity vector (a real
    # 3D-velocity source) rather than being derived from SOG/COG or position
    # deltas -- lets consumers trust it (and the crab) at low speed.
    velocity_measured: bool = False


class NavFusion:
    """Complementary GNSS/INS fusion filter.

    All timing is injected: ``update_imu`` takes the interval ``dt`` since the
    last IMU sample, and ``update_gps``/``step`` take an absolute monotonic
    timestamp ``now`` (same clock for both). The filter never reads a clock.
    """

    def __init__(
        self,
        *,
        heading_gain: float = 0.05,
        vel_tau_s: float = 2.0,
        dr_timeout_s: float = 2.0,
        crab_min_sog_mps: float = 0.3,
        crab_min_sog_measured_mps: float = 0.05,
    ) -> None:
        """Configure the filter.

        Args:
            heading_gain: Complementary blend applied toward the compass on each
                compass update, in ``(0, 1]``. Each update moves the fused
                heading by this fraction of the signed gyro-vs-compass error.
                Small = trust the gyro more (smoother, more lag); large = trust
                the compass more (snappier, noisier). ``0.05`` is a gentle blend.
            vel_tau_s: Time constant (seconds) of the first-order low-pass on the
                ground-velocity vector. Larger = smoother but laggier velocity.
            dr_timeout_s: How long (seconds) since the last GPS fix before the
                filter declares ``dead_reckoning`` and coasts the position on the
                last known ground velocity.
            crab_min_sog_mps: Speed-over-ground (m/s) below which course, and
                therefore crab, is undefined and reported as ``None`` -- when the
                velocity is DERIVED (from SOG/COG or position deltas, noisy at low
                speed).
            crab_min_sog_measured_mps: The lower SOG threshold used when the fix
                supplied a MEASURED velocity vector (a real receiver Kalman
                velocity is trustworthy near-stationary), so crab stays valid to
                much lower speeds -- this is the extra functionality a 3D velocity
                unlocks.
        """
        self.heading_gain = heading_gain
        self.vel_tau_s = vel_tau_s
        self.dr_timeout_s = dr_timeout_s
        self.crab_min_sog_mps = crab_min_sog_mps
        self.crab_min_sog_measured_mps = crab_min_sog_measured_mps

        # Fused heading (deg, [0, 360)); None until a compass seeds it.
        self._heading: float | None = None
        # Latest yaw rate (deg/s); None until an IMU sample arrives.
        self._yaw_rate: float | None = None
        # Low-passed NED ground velocity (m/s); None until GPS provides it.
        self._vel_n: float | None = None
        self._vel_e: float | None = None
        # Vertical (down) velocity, and whether the latest fix carried a MEASURED
        # velocity vector (vs a derived one). Drives the capability-gated features.
        self._vel_d: float | None = None
        self._velocity_measured: bool = False
        # Last GPS fix position + the ``now`` it arrived at (None => no fix yet).
        self._position: GeoPoint | None = None
        self._last_gps_time: float | None = None

    # -- IMU (yaw rate) ----------------------------------------------------- #

    def update_imu(self, yaw_rate_dps: float, dt: float) -> None:
        """Feed a gyro yaw-rate sample and integrate it into the heading.

        Stores the yaw rate (so it is surfaced even before a heading exists) and,
        if the heading has already been seeded by a compass, advances it by
        ``yaw_rate * dt`` (wrapped to ``[0, 360)``). With no compass ever seen the
        heading stays ``None`` -- the gyro alone cannot know absolute heading.
        """
        self._yaw_rate = yaw_rate_dps
        if self._heading is not None:
            self._heading = normalize_deg(self._heading + yaw_rate_dps * dt)

    # -- Compass (absolute heading) ---------------------------------------- #

    def update_compass(self, heading_deg: float) -> None:
        """Complementary-correct the fused heading toward the compass.

        The first call seeds the heading directly. Later calls nudge the
        gyro-integrated heading toward the compass by ``heading_gain`` of the
        signed shortest error, so the result tracks the compass over time while
        staying smooth and filled-in by the gyro between compass updates.
        """
        if self._heading is None:
            self._heading = normalize_deg(heading_deg)
            return
        error = angle_difference(self._heading, heading_deg)
        self._heading = normalize_deg(self._heading + self.heading_gain * error)

    # -- GPS (position + ground velocity) ---------------------------------- #

    def update_gps(
        self,
        point: GeoPoint,
        now: float,
        *,
        vel_n_mps: float | None = None,
        vel_e_mps: float | None = None,
        vel_d_mps: float | None = None,
        cog_deg: float | None = None,
        sog_mps: float | None = None,
    ) -> None:
        """Ingest a GPS fix: update position and low-pass the ground velocity.

        The target ground velocity is taken, in order of preference, from an
        explicit NED velocity ``(vel_n, vel_e)``, else from ``sog``/``cog``, else
        derived from the position delta since the previous fix. The velocity is
        low-passed toward that target (seeded directly on the first sample).
        Records ``now`` as the last GPS time, which clears dead reckoning.

        Whether ``(vel_n, vel_e)`` was supplied is remembered as
        ``velocity_measured`` and unlocks the low-speed crab threshold -- the
        capability activates purely on the fix carrying a real velocity vector,
        regardless of which source produced it. ``vel_d`` (vertical velocity) is
        passed straight through.
        """
        self._velocity_measured = vel_n_mps is not None and vel_e_mps is not None
        self._vel_d = vel_d_mps
        prev_point = self._position
        prev_time = self._last_gps_time

        target = self._target_velocity(
            point,
            now,
            prev_point,
            prev_time,
            vel_n_mps,
            vel_e_mps,
            cog_deg,
            sog_mps,
        )

        if target is not None:
            if self._vel_n is None or self._vel_e is None or prev_time is None:
                # First velocity estimate: seed directly, no startup transient.
                self._vel_n, self._vel_e = target
            else:
                dt = now - prev_time
                alpha = dt / (self.vel_tau_s + dt) if dt > 0 else 1.0
                self._vel_n += alpha * (target[0] - self._vel_n)
                self._vel_e += alpha * (target[1] - self._vel_e)

        self._position = point
        self._last_gps_time = now

    @staticmethod
    def _target_velocity(
        point: GeoPoint,
        now: float,
        prev_point: GeoPoint | None,
        prev_time: float | None,
        vel_n_mps: float | None,
        vel_e_mps: float | None,
        cog_deg: float | None,
        sog_mps: float | None,
    ) -> tuple[float, float] | None:
        """Best available NED velocity target for this fix, or None."""
        if vel_n_mps is not None and vel_e_mps is not None:
            return (vel_n_mps, vel_e_mps)
        if sog_mps is not None and cog_deg is not None:
            c = math.radians(cog_deg)
            return (sog_mps * math.cos(c), sog_mps * math.sin(c))
        if prev_point is not None and prev_time is not None and now > prev_time:
            dt = now - prev_time
            # Flat-earth (equirectangular) delta in metres, same approximation
            # as ``geo.offset_meters`` and accurate over a single ~10 Hz fix gap.
            dn = math.radians(point.lat - prev_point.lat) * EARTH_RADIUS_M
            de = (
                math.radians(point.lon - prev_point.lon)
                * EARTH_RADIUS_M
                * math.cos(math.radians(point.lat))
            )
            return (dn / dt, de / dt)
        return None

    # -- Fused output ------------------------------------------------------- #

    def step(self, now: float) -> FusionState:
        """Return the fused state at time ``now`` (dead-reckoning if GPS is stale).

        If more than ``dr_timeout_s`` has elapsed since the last GPS fix, sets
        ``dead_reckoning`` and coasts the position forward from the last fix on
        the last known ground velocity (constant-velocity dead reckoning), using
        a local flat-earth conversion from m/s to lat/lon.
        """
        dead_reckoning = False
        position = self._position

        if (
            self._last_gps_time is not None
            and now - self._last_gps_time > self.dr_timeout_s
        ):
            dead_reckoning = True
            if (
                self._position is not None
                and self._vel_n is not None
                and self._vel_e is not None
            ):
                elapsed = now - self._last_gps_time
                north_m = self._vel_n * elapsed
                east_m = self._vel_e * elapsed
                position = offset_meters(self._position, east_m, north_m)

        sog: float | None = None
        crab: float | None = None
        if self._vel_n is not None and self._vel_e is not None:
            sog = math.hypot(self._vel_n, self._vel_e)
            # A measured velocity vector is trustworthy near-stationary, so crab
            # stays valid to a much lower speed -- the capability a real 3D
            # velocity unlocks over a COG-derived one.
            crab_min = (self.crab_min_sog_measured_mps if self._velocity_measured
                        else self.crab_min_sog_mps)
            if sog >= crab_min and self._heading is not None:
                course = normalize_deg(math.degrees(math.atan2(self._vel_e, self._vel_n)))
                crab = angle_difference(self._heading, course)

        return FusionState(
            heading_deg=self._heading,
            yaw_rate_dps=self._yaw_rate,
            ground_vel_n_mps=self._vel_n,
            ground_vel_e_mps=self._vel_e,
            vertical_vel_mps=self._vel_d,
            sog_mps=sog,
            crab_deg=crab,
            dead_reckoning=dead_reckoning,
            position=position,
            velocity_measured=self._velocity_measured,
        )
