"""Passive anchor-alarm: motor-OFF GPS watch circle (roadmap adoption #10).

The operator drops an alarm anchor point (persisted in
``<data_dir>/anchor_alarm.json``); the server-side 1 Hz supervisor
(:meth:`~vanchor.app.Runtime.evaluate_anchor_alarm`) checks whether the live
position is still inside the watch circle.  If the boat strays outside,
``AnchorAlarmWatcher`` latches ``firing=True`` and calls each hook in
``on_breach`` exactly once per False→True transition.

**Safety guarantee**: this module imports NOTHING from the controller,
helm, governor, or motor paths.  It cannot move the boat — it is a pure
observer of :class:`~vanchor.core.models.GeoPoint`.  The
``anchor_alarm_recover`` command goes through the normal
``controller.handle_command`` entry point (all failsafes apply); this
module does not call it.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable

from .geo import haversine_m
from .models import GeoPoint
from .prefs import _atomic_write_json

logger = logging.getLogger("vanchor.anchor_alarm")


class AnchorAlarmStore:
    """Persistent mirror of the passive anchor alarm at
    ``<data_dir>/anchor_alarm.json`` (same atomic-write pattern as
    ``safety.json``).  ``armed`` is False on a fresh install."""

    def __init__(self, data_dir: str) -> None:
        self._path = os.path.join(data_dir, "anchor_alarm.json")
        self.armed: bool = False
        self.lat: float | None = None
        self.lon: float | None = None
        self.radius_m: float = 30.0
        self.set_at: float | None = None  # epoch seconds (time.time())
        self._load()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        armed = data.get("armed")
        if isinstance(armed, bool):
            self.armed = armed
        lat = data.get("lat")
        if isinstance(lat, (int, float)) and not isinstance(lat, bool):
            self.lat = float(lat)
        lon = data.get("lon")
        if isinstance(lon, (int, float)) and not isinstance(lon, bool):
            self.lon = float(lon)
        radius = data.get("radius_m")
        if isinstance(radius, (int, float)) and not isinstance(radius, bool):
            self.radius_m = float(radius)
        set_at = data.get("set_at")
        if isinstance(set_at, (int, float)) and not isinstance(set_at, bool):
            self.set_at = float(set_at)
        # An armed record missing lat/lon is treated as disarmed.
        if self.armed and (self.lat is None or self.lon is None):
            self.armed = False

    def _save(self) -> None:
        _atomic_write_json(self._path, self.to_dict())

    def to_dict(self) -> dict:
        return {
            "armed": self.armed,
            "lat": self.lat,
            "lon": self.lon,
            "radius_m": self.radius_m,
            "set_at": self.set_at,
        }

    # ------------------------------------------------------------------ #
    # Mutations (each persists immediately)
    # ------------------------------------------------------------------ #
    def set(self, point: GeoPoint, radius_m: float, set_at: float) -> None:
        """Arm the alarm at ``point`` with watch-circle ``radius_m``."""
        self.armed = True
        self.lat = point.lat
        self.lon = point.lon
        self.radius_m = radius_m
        self.set_at = set_at
        self._save()

    def clear(self) -> None:
        """Disarm the alarm; keeps the last lat/lon/radius for re-arm UX."""
        self.armed = False
        self._save()


class AnchorAlarmWatcher:
    """Motor-OFF GPS watch circle (passive anchor alarm, roadmap #10).

    Pure observer: reads a position, compares against the armed circle,
    latches ``firing`` on breach.  It holds NO reference to the controller,
    helm, governor or motor and can therefore never move the boat.  Breach
    hooks (``on_breach``) let the Runtime log/telemetrise now and Task 3
    add Web Push later.
    """

    def __init__(self, store: AnchorAlarmStore, *, stale_fix_s: float = 30.0) -> None:
        self.store = store
        self.stale_fix_s = stale_fix_s
        self.firing: bool = False
        self.stale: bool = False
        self.distance_m: float | None = None
        self.breach_count: int = 0
        self._last_fix_age_s: float | None = None
        # Called with the snapshot dict on each False->True firing transition.
        # Each hook is isolated (try/except log) so one bad hook can't stop
        # the others or the supervisor step.  Task 3 appends the push sender.
        self.on_breach: list[Callable[[dict], None]] = []

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def set(self, point: GeoPoint, radius_m: float, *, now: float) -> dict:
        """Arm the alarm at ``point`` with ``radius_m`` clamped to [5, 500]."""
        radius_m = max(5.0, min(500.0, radius_m))
        self.store.set(point, radius_m, now)
        self.firing = False
        self.distance_m = None
        self.stale = False
        self._last_fix_age_s = None
        return self.snapshot()

    def clear(self) -> dict:
        """Disarm the alarm and reset live state."""
        self.store.clear()
        self.firing = False
        self.stale = False
        self.distance_m = None
        self._last_fix_age_s = None
        return self.snapshot()

    def evaluate(self, position: GeoPoint | None, fix_age_s: float | None) -> dict:
        """1 Hz step: check position against the armed circle.

        * If not armed: return an all-quiet snapshot.
        * If GPS is stale: keep the previous firing latch (a breach must not
          be silenced by losing GPS) and the previous distance; set
          ``stale=True``.
        * Otherwise: compute distance, latch breach with hysteresis, fire
          hooks on the False→True transition.
        """
        if not self.store.armed:
            return self.snapshot()

        # Stale GPS: position absent or fix too old.
        stale = position is None or (
            fix_age_s is not None and fix_age_s > self.stale_fix_s
        )
        self._last_fix_age_s = fix_age_s
        if stale:
            self.stale = True
            # Keep previous firing latch and distance — don't silence a breach.
            return self.snapshot()

        self.stale = False
        self._last_fix_age_s = fix_age_s
        self.distance_m = haversine_m(
            position, GeoPoint(self.store.lat, self.store.lon)
        )

        # Latch with hysteresis (mirror AnchorHoldMode.update idiom).
        if self.distance_m > self.store.radius_m and not self.firing:
            self.firing = True
            self.breach_count += 1
            snap = self.snapshot()
            for hook in self.on_breach:
                try:
                    hook(snap)
                except Exception:  # noqa: BLE001
                    logger.exception("anchor alarm on_breach hook raised")
        elif self.firing and self.distance_m < self.store.radius_m * 0.8:
            self.firing = False

        return self.snapshot()

    def snapshot(self) -> dict:
        """Current alarm state as a plain dict (telemetry/UI shape)."""
        dm = self.distance_m
        fa = self._last_fix_age_s
        return {
            "armed": self.store.armed,
            "lat": self.store.lat,
            "lon": self.store.lon,
            "radius_m": self.store.radius_m,
            "distance_m": round(dm, 1) if dm is not None else None,
            "firing": self.firing,
            "stale": self.stale,
            "fix_age_s": round(fa, 1) if fa is not None else None,
            "set_at": self.store.set_at,
            "breach_count": self.breach_count,
        }
