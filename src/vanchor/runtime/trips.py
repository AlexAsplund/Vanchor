"""Trip-recorder cluster extracted from Runtime (issue #78).

The 6 methods that handle trip management (start, stop, list, get, GPX export,
delete) live here.  ``TripService`` holds a back-reference to ``Runtime`` via
``self._rt`` for shared state that remains on Runtime (``trip``, ``_now_fn``).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("vanchor.app")


class TripService:
    """Trip-recorder cluster -- split out of Runtime."""

    def __init__(self, rt) -> None:
        self._rt = rt   # back-reference to Runtime for shared state

    # ------------------------------------------------------------------ #
    # Trip log (#66)
    # ------------------------------------------------------------------ #

    def trip_start(self, name: str | None = None) -> dict:
        """Manually start a trip (overrides/replaces any active one)."""
        rt = self._rt
        trip = rt.trip.start(name, rt._now_fn())
        return rt.trip.snapshot(rt._now_fn())

    def trip_stop(self) -> dict:
        """Manually stop + persist the active trip. No-op when none is active."""
        rt = self._rt
        rt.trip.stop(rt._now_fn())
        return rt.trip.snapshot(rt._now_fn())

    def trip_list(self) -> list[dict]:
        return self._rt.trip.list_trips()

    def trip_get(self, trip_id: str) -> dict | None:
        return self._rt.trip.get_trip(trip_id)

    def trip_gpx(self, trip_id: str) -> str | None:
        return self._rt.trip.gpx(trip_id)

    def trip_delete(self, trip_id: str) -> bool:
        return self._rt.trip.delete_trip(trip_id)
