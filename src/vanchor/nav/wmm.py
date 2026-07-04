"""Magnetic declination (variation) via the full World Magnetic Model.

Declination converts a MAGNETIC compass heading to true: ``true = magnetic +
declination`` (East-positive). :func:`declination_deg` evaluates the full
degree-12 **WMM2025** spherical-harmonic model (via the pure-Python ``pygeomag``
package, which ships the official NOAA coefficients), accurate to a fraction of a
degree worldwide within the model's 2025-2030 validity window.

If ``pygeomag`` is somehow unavailable at runtime it degrades to a low-degree
(dipole + quadrupole) IGRF approximation so heading conversion never crashes --
but the full model is the shipped default (``pygeomag`` is a dependency).
"""
from __future__ import annotations

import datetime
import math

# --- Full model (WMM2025 via pygeomag) ------------------------------------- #
_geomag = None  # lazily-built pygeomag.GeoMag (loads the coefficient table once)
_WMM_MIN_YEAR = 2025.0
_WMM_MAX_YEAR = 2029.99  # model validity ceiling; clamp so pygeomag never raises


def _decimal_year(when: datetime.date | None = None) -> float:
    d = when or datetime.date.today()
    return d.year + (d - datetime.date(d.year, 1, 1)).days / 365.25


def declination_deg(lat: float, lon: float, year: float | None = None) -> float:
    """Magnetic declination at *lat*/*lon* (degrees, East-positive).

    Full WMM2025 when ``pygeomag`` is present; a coarse dipole+quadrupole fallback
    otherwise. ``year`` is a decimal year (defaults to today), clamped to the
    model's validity window so a stale clock can't push it out of range.
    """
    try:
        global _geomag
        if _geomag is None:
            from pygeomag import GeoMag
            _geomag = GeoMag()  # default coefficients = the latest shipped (WMM2025)
        yr = _decimal_year() if year is None else float(year)
        yr = max(_WMM_MIN_YEAR, min(yr, _WMM_MAX_YEAR))
        return float(_geomag.calculate(glat=lat, glon=lon, alt=0, time=yr).d)
    except Exception:  # noqa: BLE001 - never let heading conversion crash
        return _approx_declination_deg(lat, lon)


# --- Fallback: low-degree IGRF (dipole + quadrupole, epoch 2020.0) ---------- #
# Coarse (a few degrees over the mid-latitudes, worse in anomalous regions and
# near the poles). Only used if the full model can't be loaded.
_IGRF_G: dict[tuple[int, int], float] = {
    (1, 0): -29404.8, (1, 1): -1450.9,
    (2, 0): -2499.6, (2, 1): 2982.0, (2, 2): 1677.0,
}
_IGRF_H: dict[tuple[int, int], float] = {
    (1, 1): 4652.5, (2, 1): -2991.6, (2, 2): -734.6,
}
_SQRT3 = math.sqrt(3.0)


def _approx_declination_deg(lat: float, lon: float) -> float:
    theta = math.radians(90.0 - lat)
    phi = math.radians(lon)
    st, ct = math.sin(theta), math.cos(theta)
    if abs(st) < 1e-9:
        return 0.0
    legendre_p = {
        (1, 0): ct, (1, 1): st, (2, 0): (3.0 * ct * ct - 1.0) / 2.0,
        (2, 1): _SQRT3 * st * ct, (2, 2): (_SQRT3 / 2.0) * st * st,
    }
    legendre_dp = {
        (1, 0): -st, (1, 1): ct, (2, 0): -3.0 * st * ct,
        (2, 1): _SQRT3 * (ct * ct - st * st), (2, 2): _SQRT3 * st * ct,
    }
    x = y = 0.0
    for (n, m), p in legendre_p.items():
        g = _IGRF_G.get((n, m), 0.0)
        h = _IGRF_H.get((n, m), 0.0)
        cos_mphi, sin_mphi = math.cos(m * phi), math.sin(m * phi)
        x += (g * cos_mphi + h * sin_mphi) * legendre_dp[(n, m)]
        y += (m / st) * (g * sin_mphi - h * cos_mphi) * p
    return math.degrees(math.atan2(y, x))
