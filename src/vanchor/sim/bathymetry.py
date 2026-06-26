"""A simple synthetic lake bottom for the simulator.

There is no real depth sensor in simulation, so we model a smooth, deterministic
bathymetry as a function of position. It is varied enough to make the depth HUD
and the auto depth-map overlay interesting without pretending to be a real chart.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..core.geo import EARTH_RADIUS_M
from ..core.models import GeoPoint


@dataclass
class Bathymetry:
    """Depth (m) as a smooth function of position around a reference point."""

    origin: GeoPoint = GeoPoint(59.66275, 13.32247)
    base_m: float = 8.0
    min_m: float = 1.5
    max_m: float = 18.0

    def _local_meters(self, point: GeoPoint) -> tuple[float, float]:
        """East/north offset (m) of ``point`` from the origin (equirectangular)."""
        east = (
            math.radians(point.lon - self.origin.lon)
            * EARTH_RADIUS_M
            * math.cos(math.radians(self.origin.lat))
        )
        north = math.radians(point.lat - self.origin.lat) * EARTH_RADIUS_M
        return east, north

    def depth_at(self, point: GeoPoint) -> float:
        east, north = self._local_meters(point)
        d = (
            self.base_m
            + 4.0 * math.sin(east / 60.0)
            + 3.0 * math.cos(north / 45.0)
            + 2.0 * math.sin((east + north) / 80.0)
            - 1.5 * math.cos(east / 25.0)
        )
        return max(self.min_m, min(self.max_m, d))
