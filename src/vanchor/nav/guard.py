"""Sensor-anomaly protection.

Cheap GPS/compass hardware throws occasional garbage: the heading sensor flips
180 deg in a sample, the GPS position jumps tens of metres on a poor fix. Feeding
those straight to the controller causes violent, wrong manoeuvres.

:class:`SensorGuard` is a small spike filter that sits in the navigator. It
rejects a single implausible reading (one that would imply impossible motion)
but **accepts it if the next reading confirms it** -- so a genuine large move
(or a hard turn) gets through after one sample, while an isolated glitch is
dropped. It also rejects out-of-range coordinates outright.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.geo import angle_difference, haversine_m
from ..core.models import GeoPoint


@dataclass
class SensorGuardConfig:
    # Reject a position that jumps more than this from the last accepted fix,
    # unless the next fix confirms the new location.
    position_jump_max_m: float = 15.0
    # Reject a heading that jumps more than this (deg) in one sample, unless
    # confirmed by the next reading. ~30 deg/sample at 5 Hz = 150 deg/s.
    heading_jump_max_deg: float = 30.0


class SensorGuard:
    def __init__(self, config: SensorGuardConfig | None = None) -> None:
        self.config = config or SensorGuardConfig()
        self._last_point: GeoPoint | None = None
        self._pending_point: GeoPoint | None = None
        self._last_heading: float | None = None
        self._pending_heading: float | None = None
        self.position_rejected = 0
        self.heading_rejected = 0

    def check_position(self, point: GeoPoint) -> bool:
        """Return True if ``point`` should be accepted as the boat's position."""
        if not (-90.0 <= point.lat <= 90.0 and -180.0 <= point.lon <= 180.0):
            self.position_rejected += 1
            return False
        if self._last_point is None:
            self._last_point = point
            return True
        if haversine_m(point, self._last_point) <= self.config.position_jump_max_m:
            self._last_point = point
            self._pending_point = None
            return True
        # Far from the last accepted fix: accept only if it confirms a prior
        # outlier (a real move), otherwise treat as a one-off glitch.
        if (
            self._pending_point is not None
            and haversine_m(point, self._pending_point) <= self.config.position_jump_max_m
        ):
            self._last_point = point
            self._pending_point = None
            return True
        self._pending_point = point
        self.position_rejected += 1
        return False

    def check_heading(self, heading_deg: float) -> bool:
        if self._last_heading is None:
            self._last_heading = heading_deg
            return True
        if abs(angle_difference(self._last_heading, heading_deg)) <= self.config.heading_jump_max_deg:
            self._last_heading = heading_deg
            self._pending_heading = None
            return True
        if (
            self._pending_heading is not None
            and abs(angle_difference(self._pending_heading, heading_deg))
            <= self.config.heading_jump_max_deg
        ):
            self._last_heading = heading_deg
            self._pending_heading = None
            return True
        self._pending_heading = heading_deg
        self.heading_rejected += 1
        return False
