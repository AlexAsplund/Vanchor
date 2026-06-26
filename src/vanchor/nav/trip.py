"""Trip log: record each outing's track, distance, duration and speed stats.

A *trip* is one continuous outing -- from the moment the boat starts making way
until it goes idle (or the helmsman stops it). While a trip is active the
:class:`TripLog` samples the boat's position into a breadcrumb track (min-distance
filtered like :class:`~vanchor.nav.track.TrackRecorder`), integrates the distance
travelled as the sum of segment lengths, and tracks the max speed-over-ground.

The log can **auto-start** a trip when the boat first makes way (SOG over a small
threshold) and **auto-stop** it after a stretch of idleness; a manual start/stop
always works and overrides the automatic behaviour. Finished trips are persisted
to ``<data_dir>/trips/<id>.json`` and can be listed, fetched, exported as GPX or
deleted.

All time comes in through ``now`` arguments (the Runtime feeds its injectable
``_now_fn``), so the auto-start/stop logic is fully deterministic in tests.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field

from ..core.geo import haversine_m
from ..core.models import GeoPoint

logger = logging.getLogger("vanchor.trip")


def _trip_id(started_at: float) -> str:
    """A filesystem-safe id derived from the start timestamp."""
    return "trip-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(started_at))


@dataclass
class Trip:
    """One outing's recorded track and summary statistics."""

    id: str
    name: str
    started_at: float
    ended_at: float | None = None
    distance_m: float = 0.0
    max_speed_kn: float = 0.0
    points: list[GeoPoint] = field(default_factory=list)
    # Whether this trip was started automatically (so auto-stop may end it).
    auto: bool = False

    def duration_s(self, now: float) -> float:
        end = self.ended_at if self.ended_at is not None else now
        return max(0.0, end - self.started_at)

    def avg_speed_kn(self, now: float) -> float:
        dur = self.duration_s(now)
        if dur <= 0.0:
            return 0.0
        # metres-per-second -> knots
        return (self.distance_m / dur) / 0.514444

    def summary(self, now: float) -> dict:
        """Summary fields only (no point array) for the list endpoint."""
        return {
            "id": self.id,
            "name": self.name,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "distance_m": round(self.distance_m, 1),
            "duration_s": round(self.duration_s(now), 1),
            "avg_speed_kn": round(self.avg_speed_kn(now), 2),
            "max_speed_kn": round(self.max_speed_kn, 2),
            "point_count": len(self.points),
        }

    def to_dict(self, now: float) -> dict:
        """Full record including the track points."""
        d = self.summary(now)
        d["points"] = [[p.lat, p.lon] for p in self.points]
        return d


class TripLog:
    """Records the current outing and persists finished trips to disk.

    Call :meth:`update` on every telemetry tick with the boat's current position
    and speed-over-ground (knots) plus the current time; it handles breadcrumb
    sampling, distance/max-speed accumulation and the auto-start/stop state
    machine. :meth:`start`/:meth:`stop` give the helmsman manual control.
    """

    def __init__(
        self,
        data_dir: str,
        *,
        min_distance_m: float = 5.0,
        auto: bool = True,
        start_speed_kn: float = 0.5,
        idle_timeout_s: float = 120.0,
        max_points: int = 5000,
    ) -> None:
        self.dir = os.path.join(data_dir, "trips")
        self.min_distance_m = min_distance_m
        self.auto = auto
        self.start_speed_kn = start_speed_kn
        self.idle_timeout_s = idle_timeout_s
        self.max_points = max_points
        self.current: Trip | None = None
        # Wall-clock time the boat was last seen making way (for auto-stop).
        self._last_moving_at: float | None = None

    # ------------------------------------------------------------------ #
    # Manual control
    # ------------------------------------------------------------------ #
    def start(self, name: str | None, now: float, *, auto: bool = False) -> Trip:
        """Begin a fresh trip, finalizing any trip already in progress."""
        if self.current is not None:
            self.stop(now)
        started = now
        trip = Trip(
            id=_trip_id(started),
            name=name or time.strftime("Trip %Y-%m-%d %H:%M", time.localtime(started)),
            started_at=started,
            auto=auto,
        )
        self.current = trip
        self._last_moving_at = now
        logger.info("trip started: %s (%s)", trip.id, "auto" if auto else "manual")
        return trip

    def stop(self, now: float) -> Trip | None:
        """Finalize the active trip and persist it. Returns the saved trip."""
        trip = self.current
        if trip is None:
            return None
        trip.ended_at = now
        self.current = None
        self._last_moving_at = None
        self._save(trip, now)
        logger.info(
            "trip stopped: %s (%.0f m, %.0f s)",
            trip.id,
            trip.distance_m,
            trip.duration_s(now),
        )
        return trip

    # ------------------------------------------------------------------ #
    # Per-tick recording + auto start/stop
    # ------------------------------------------------------------------ #
    def update(self, position: GeoPoint | None, sog_kn: float, now: float) -> None:
        """Advance the trip log one tick.

        Records a breadcrumb + integrates distance/max-speed for the active trip,
        and runs the auto-start/stop machine when ``auto`` is enabled.
        """
        making_way = sog_kn >= self.start_speed_kn and position is not None
        if making_way:
            self._last_moving_at = now

        if self.current is None:
            # Auto-start when the boat first makes way.
            if self.auto and making_way:
                self.start(None, now, auto=True)
            else:
                return
        else:
            self._accumulate(position, sog_kn)
            # Auto-stop an auto-started trip after a stretch of idleness.
            if (
                self.auto
                and self.current.auto
                and self._last_moving_at is not None
                and now - self._last_moving_at >= self.idle_timeout_s
            ):
                self.stop(now)

    def _accumulate(self, position: GeoPoint | None, sog_kn: float) -> None:
        trip = self.current
        assert trip is not None
        if sog_kn > trip.max_speed_kn:
            trip.max_speed_kn = sog_kn
        if position is None:
            return
        if not trip.points:
            trip.points.append(position)
            return
        seg = haversine_m(trip.points[-1], position)
        if seg >= self.min_distance_m:
            trip.distance_m += seg
            trip.points.append(position)
            if len(trip.points) > self.max_points:
                trip.points = trip.points[-self.max_points :]

    # ------------------------------------------------------------------ #
    # Telemetry
    # ------------------------------------------------------------------ #
    def snapshot(self, now: float) -> dict:
        """The CURRENT trip's live stats for telemetry (zeros when idle)."""
        trip = self.current
        if trip is None:
            return {
                "active": False,
                "name": None,
                "distance_m": 0.0,
                "duration_s": 0.0,
                "avg_speed_kn": 0.0,
                "max_speed_kn": 0.0,
            }
        return {
            "active": True,
            "name": trip.name,
            "distance_m": round(trip.distance_m, 1),
            "duration_s": round(trip.duration_s(now), 1),
            "avg_speed_kn": round(trip.avg_speed_kn(now), 2),
            "max_speed_kn": round(trip.max_speed_kn, 2),
        }

    # ------------------------------------------------------------------ #
    # Persistence + queries
    # ------------------------------------------------------------------ #
    def _path(self, trip_id: str) -> str:
        return os.path.join(self.dir, f"{trip_id}.json")

    def _save(self, trip: Trip, now: float) -> None:
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self._path(trip.id), "w", encoding="utf-8") as fh:
                json.dump(trip.to_dict(now), fh)
        except OSError as exc:  # pragma: no cover - disk failure
            logger.warning("could not save trip %s: %s", trip.id, exc)

    def list_trips(self) -> list[dict]:
        """Summaries of all saved trips, newest first."""
        out: list[dict] = []
        if not os.path.isdir(self.dir):
            return out
        for name in sorted(os.listdir(self.dir)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.dir, name), encoding="utf-8") as fh:
                    d = json.load(fh)
            except (OSError, ValueError):
                continue
            d.pop("points", None)
            out.append(d)
        out.sort(key=lambda d: d.get("started_at", 0.0), reverse=True)
        return out

    def get_trip(self, trip_id: str) -> dict | None:
        """Full saved trip (including points), or None if absent."""
        path = self._path(trip_id)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def delete_trip(self, trip_id: str) -> bool:
        """Remove a saved trip. Returns True if it existed."""
        path = self._path(trip_id)
        if not os.path.isfile(path):
            return False
        try:
            os.remove(path)
            return True
        except OSError:  # pragma: no cover - disk failure
            return False

    def gpx(self, trip_id: str) -> str | None:
        """Export a saved trip as a GPX ``<trk>``, or None if absent."""
        trip = self.get_trip(trip_id)
        if trip is None:
            return None
        return trip_to_gpx(trip)


def trip_to_gpx(trip: dict) -> str:
    """Render a trip dict (as persisted) into a GPX 1.1 document string."""
    from xml.sax.saxutils import escape

    pts = trip.get("points", [])
    seg = "".join(
        f'<trkpt lat="{lat}" lon="{lon}"></trkpt>' for lat, lon in pts
    )
    name = escape(str(trip.get("name", trip.get("id", "Trip"))))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gpx version="1.1" creator="vanchor-ng" '
        'xmlns="http://www.topografix.com/GPX/1/1">'
        f"<trk><name>{name}</name><trkseg>{seg}</trkseg></trk>"
        "</gpx>"
    )
