"""Pure geodesy helpers.

All functions are side-effect free and fully unit-testable. Distances are in
metres, bearings in degrees clockwise from true north. We use a spherical-Earth
model which is accurate to well within a metre over the short distances
(tens to hundreds of metres) relevant to anchoring and close-quarters steering.
"""

from __future__ import annotations

import math

from .models import CrossTrackError, GeoPoint

EARTH_RADIUS_M = 6_371_000.0


def normalize_deg(angle: float) -> float:
    """Wrap an angle into the range [0, 360)."""
    return angle % 360.0


def angle_difference(from_deg: float, to_deg: float) -> float:
    """Shortest signed difference ``to - from`` in the range (-180, 180].

    Positive means ``to`` is clockwise (to starboard) of ``from``.
    """
    diff = (to_deg - from_deg + 180.0) % 360.0 - 180.0
    # ``%`` yields -180 for an exact half turn; normalise to +180.
    return diff if diff != -180.0 else 180.0


def haversine_m(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance between two points, in metres."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = math.radians(b.lat - a.lat)
    dlon = math.radians(b.lon - a.lon)
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def initial_bearing(a: GeoPoint, b: GeoPoint) -> float:
    """Initial great-circle bearing from ``a`` to ``b``, degrees [0, 360)."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlon = math.radians(b.lon - a.lon)
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(
        dlon
    )
    return normalize_deg(math.degrees(math.atan2(x, y)))


def destination_point(start: GeoPoint, distance_m: float, bearing_deg: float) -> GeoPoint:
    """Point reached by travelling ``distance_m`` from ``start`` on ``bearing``."""
    ang = distance_m / EARTH_RADIUS_M
    brg = math.radians(bearing_deg)
    lat1 = math.radians(start.lat)
    lon1 = math.radians(start.lon)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(ang) + math.cos(lat1) * math.sin(ang) * math.cos(brg)
    )
    lon2 = lon1 + math.atan2(
        math.sin(brg) * math.sin(ang) * math.cos(lat1),
        math.cos(ang) - math.sin(lat1) * math.sin(lat2),
    )
    return GeoPoint(math.degrees(lat2), normalize_deg(math.degrees(lon2) + 180.0) - 180.0)


def cross_track(start: GeoPoint, end: GeoPoint, point: GeoPoint) -> CrossTrackError:
    """Signed cross-track distance of ``point`` from the ``start``->``end`` leg.

    Positive distance => the boat is to the right (starboard) of the track and
    must steer left ("L") to return; negative => steer right ("R").
    """
    d13 = haversine_m(start, point) / EARTH_RADIUS_M
    brg13 = math.radians(initial_bearing(start, point))
    brg12 = math.radians(initial_bearing(start, end))
    xt = math.asin(math.sin(d13) * math.sin(brg13 - brg12)) * EARTH_RADIUS_M
    steer_to = "L" if xt > 0 else "R"
    return CrossTrackError(distance_m=xt, steer_to=steer_to)


def offset_meters(point: GeoPoint, east_m: float, north_m: float) -> GeoPoint:
    """Shift ``point`` by a local east/north offset in metres (equirectangular).

    Accurate for the small per-tick displacements used by the simulator.
    """
    dlat = north_m / EARTH_RADIUS_M
    dlon = east_m / (EARTH_RADIUS_M * math.cos(math.radians(point.lat)))
    return GeoPoint(point.lat + math.degrees(dlat), point.lon + math.degrees(dlon))


def knots_to_mps(knots: float) -> float:
    return knots * 0.514444


def mps_to_knots(mps: float) -> float:
    return mps / 0.514444
