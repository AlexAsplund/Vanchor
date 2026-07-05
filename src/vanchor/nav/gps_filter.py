"""Accuracy-weighted GPS position low-pass.

A first-order low-pass on the fix position whose time constant scales with the
receiver's reported horizontal accuracy (``hAcc``): a good fix (small hAcc) passes
through nearly unchanged, a poor fix (large hAcc -- e.g. indoor multipath) is
smoothed toward the running estimate. It is deliberately NOT gated on motion, so
sustained real drift still reaches the controller (merely delayed) -- a spot-lock
must see genuine drift to correct it.

Honest limits: this attenuates high-frequency jitter and the phantom-velocity
component, but it CANNOT remove slow multipath *wander* (a random walk that a
causal filter can't tell from real slow drift). The complementary mitigation is to
scale the control tolerance / spot-lock radius to hAcc rather than to fight the
wander. Passthrough when no accuracy is reported (e.g. plain NMEA), so it is safe
to leave enabled.
"""
from __future__ import annotations

from ..core.models import GeoPoint


class GpsPositionFilter:
    def __init__(self, *, good_hacc_m: float = 3.0, tau_per_m_s: float = 0.5,
                 max_tau_s: float = 8.0) -> None:
        """Args:
            good_hacc_m: at/below this hAcc the fix is trusted -> passthrough.
            tau_per_m_s: seconds of low-pass time constant added per metre of hAcc
                *above* ``good_hacc_m`` (so a good fix has ~0 lag).
            max_tau_s: cap on the time constant (bounds the lag on real motion).
        """
        self.good_hacc_m = good_hacc_m
        self.tau_per_m_s = tau_per_m_s
        self.max_tau_s = max_tau_s
        self._lat: float | None = None
        self._lon: float | None = None
        self._last_now: float | None = None

    def reset(self) -> None:
        self._lat = self._lon = self._last_now = None

    def update(self, point: GeoPoint, hacc_m: float | None, now: float) -> GeoPoint:
        """Filter ``point`` given the fix's ``hAcc`` and a monotonic ``now``."""
        if self._lat is None or self._lon is None or self._last_now is None:
            self._lat, self._lon, self._last_now = point.lat, point.lon, now
            return point
        dt = now - self._last_now
        self._last_now = now
        if hacc_m is None or dt <= 0.0:
            # No accuracy to weight on (or no time elapsed): pass through.
            self._lat, self._lon = point.lat, point.lon
            return point
        # Time constant grows with hAcc ABOVE the "good" threshold; a good fix
        # (hacc <= good) gives tau 0 -> alpha 1 -> passthrough.
        tau = min(self.max_tau_s, max(0.0, hacc_m - self.good_hacc_m) * self.tau_per_m_s)
        alpha = 1.0 if tau <= 0.0 else dt / (tau + dt)
        lat = self._lat + alpha * (point.lat - self._lat)  # _lat/_lon set by the guard above
        lon = self._lon + alpha * (point.lon - self._lon)
        self._lat, self._lon = lat, lon
        return GeoPoint(lat, lon)
