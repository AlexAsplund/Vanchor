"""Track recording: breadcrumb the boat's path, then replay or retrace it.

This mirrors the "record-a-track" / "BackTrack" feature of GPS trolling motors
(Minn Kota iTracks, MotorGuide routes). A :class:`TrackRecorder` samples the
boat's position into a list of :class:`Waypoint`s while recording; replaying
feeds those points straight into :class:`~vanchor.controller.modes.WaypointMode`
(forward), and BackTrack feeds them reversed.
"""

from __future__ import annotations

import logging

from ..core.geo import haversine_m
from ..core.models import GeoPoint, Waypoint

logger = logging.getLogger("vanchor.track")


class TrackRecorder:
    """Records a breadcrumb track of GeoPoints, one every ``min_distance_m``."""

    def __init__(self, min_distance_m: float = 5.0, max_points: int = 2000) -> None:
        self.min_distance_m = min_distance_m
        self.max_points = max_points
        self.recording = False
        self.points: list[GeoPoint] = []

    def start(self, seed: GeoPoint | None = None) -> None:
        """Begin a fresh recording (optionally seeded with the current point)."""
        self.points = [seed] if seed is not None else []
        self.recording = True
        logger.info("track recording started")

    def stop(self) -> None:
        self.recording = False
        logger.info("track recording stopped (%d points)", len(self.points))

    def clear(self) -> None:
        self.points = []
        self.recording = False

    def maybe_record(self, point: GeoPoint | None) -> None:
        """Append ``point`` if recording and it is far enough from the last one."""
        if not self.recording or point is None:
            return
        if not self.points or haversine_m(self.points[-1], point) >= self.min_distance_m:
            self.points.append(point)
            if len(self.points) > self.max_points:
                # Keep the most recent track within bounds.
                self.points = self.points[-self.max_points :]

    def as_waypoints(self, *, reverse: bool = False) -> list[Waypoint]:
        pts = list(reversed(self.points)) if reverse else list(self.points)
        return [Waypoint(name=f"T{i}", point=p) for i, p in enumerate(pts)]
