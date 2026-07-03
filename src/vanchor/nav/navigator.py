"""The navigator turns raw NMEA sentences into updates on the shared state.

It is the only component that writes the *perceived* position/heading, keeping a
single, well-defined path from "bytes off the wire" to "what the controller
believes". It is driven both synchronously (``handle_sentence`` for tests) and
asynchronously (subscribed to the ``nmea.in`` topic at runtime).
"""

from __future__ import annotations

import logging
import math
import time

from ..core import events
from ..core.events import EventBus
from ..core.models import GeoPoint, GpsFix
from ..core.state import NavigationState
from . import nmea
from .guard import SensorGuard, SensorGuardConfig

logger = logging.getLogger("vanchor.navigator")

# --- COG-derived heading fallback (#17) -------------------------------------- #
# When the compass goes stale/lost a guided mode has no heading to steer on, so
# the safety governor coasts the boat. But if the GPS shows the boat making way,
# its course-over-ground is a usable heading proxy: fall back to COG so guided
# modes keep steering. Built-in (no config knob) so it is always available.
#
# Minimum speed-over-ground for COG to be trusted. Below this the boat is
# effectively at rest and COG is dominated by GPS position noise (it can point
# anywhere), so we do NOT fall back -- we keep coasting, exactly as before.
COG_HEADING_MIN_SOG_KNOTS = 0.5
# Seconds without a fresh compass heading before the COG fallback may take over.
# Mirrors the safety governor's default heading_stale_s so the fallback engages
# right when a guided mode would otherwise be coasted for a stale compass.
COMPASS_STALE_S = 3.0


# --- Magnetic declination model (#47) --------------------------------------- #
# A single, self-contained source of local magnetic declination (a.k.a.
# variation), used to convert a MAGNETIC compass heading (HDM / uncorrected HDG,
# reference="M") into the TRUE frame the whole control stack steers in. It is a
# low-degree spherical-harmonic (World Magnetic Model-style) approximation: the
# centered dipole plus quadrupole terms of IGRF-13, epoch 2020.0. That is enough
# to place declination within a few degrees across the populated mid-latitudes
# (hand-verified ~+5.9 deg for Stockholm vs. the ~+6.5 deg real value) while
# staying dependency-free and fully unit-testable.
#
# Deliberately NOT a config knob this wave: the source is internal so heading
# semantics stay in one place. Clear extension point -- ``magnetic_declination_deg``
# is a pure ``(lat, lon) -> degrees`` function; swap it for a full-degree
# WMM/IGRF evaluation (add the higher-degree Gauss coefficients + a Legendre
# recursion), or wrap it to prefer a survey/plotter-supplied variation, without
# touching a single call site.
#
# IGRF-13 Gauss coefficients (nT), epoch 2020.0, Schmidt semi-normalized.
_IGRF_G: dict[tuple[int, int], float] = {
    (1, 0): -29404.8,
    (1, 1): -1450.9,
    (2, 0): -2499.6,
    (2, 1): 2982.0,
    (2, 2): 1677.0,
}
_IGRF_H: dict[tuple[int, int], float] = {
    (1, 1): 4652.5,
    (2, 1): -2991.6,
    (2, 2): -734.6,
}
_SQRT3 = math.sqrt(3.0)


def magnetic_declination_deg(lat: float, lon: float) -> float:
    """Approximate local magnetic declination (variation) at *lat*/*lon*.

    Returns degrees, East-positive: ``true = magnetic + declination``. Uses the
    low-degree (dipole + quadrupole) IGRF-13/2020 spherical-harmonic model
    described in the module note above -- accurate to a few degrees over the
    populated mid-latitudes and degrading toward the magnetic poles, where the
    horizontal field vanishes and declination is inherently ill-conditioned.
    """
    theta = math.radians(90.0 - lat)  # geocentric colatitude
    phi = math.radians(lon)
    st, ct = math.sin(theta), math.cos(theta)
    if abs(st) < 1e-9:
        # A geographic pole: horizontal field (and thus declination) undefined.
        return 0.0
    # Schmidt semi-normalized associated Legendre functions P and dP/dtheta,
    # degrees 1-2 as closed forms (extend here for higher degree).
    legendre_p: dict[tuple[int, int], float] = {
        (1, 0): ct,
        (1, 1): st,
        (2, 0): (3.0 * ct * ct - 1.0) / 2.0,
        (2, 1): _SQRT3 * st * ct,
        (2, 2): (_SQRT3 / 2.0) * st * st,
    }
    legendre_dp: dict[tuple[int, int], float] = {
        (1, 0): -st,
        (1, 1): ct,
        (2, 0): -3.0 * st * ct,
        (2, 1): _SQRT3 * (ct * ct - st * st),
        (2, 2): _SQRT3 * st * ct,
    }
    x = 0.0  # geographic-north field component
    y = 0.0  # geographic-east field component
    for (n, m), p in legendre_p.items():
        g = _IGRF_G.get((n, m), 0.0)
        h = _IGRF_H.get((n, m), 0.0)
        cos_mphi, sin_mphi = math.cos(m * phi), math.sin(m * phi)
        x += (g * cos_mphi + h * sin_mphi) * legendre_dp[(n, m)]
        y += (m / st) * (g * sin_mphi - h * cos_mphi) * p
    return math.degrees(math.atan2(y, x))


class Navigator:
    def __init__(
        self,
        state: NavigationState,
        bus: EventBus | None = None,
        guard_config: SensorGuardConfig | None = None,
        *,
        declination_deg: float | None = 0.0,
        mono_fn=time.monotonic,
    ) -> None:
        self.state = state
        self.bus = bus
        # Local magnetic declination (degrees East-positive). Applied to MAGNETIC
        # headings (HDM/HDG with reference="M") to yield true before the control
        # stack uses state.heading_deg. True headings (HDT/HDG fully corrected,
        # reference="T") pass through unchanged.
        #
        #   * A float (default 0.0): a fixed manual declination. 0.0 is a no-op,
        #     matching a compass source that already emits true heading and the
        #     simulator (which uses true headings throughout) -- behaviour is
        #     unchanged from before this field learned to auto-compute.
        #   * None: AUTO -- derive declination from the boat's current fix via the
        #     internal ``magnetic_declination_deg`` model, recomputed per heading.
        #     Falls back to 0.0 until a position is known.
        self.declination_deg = declination_deg
        # MONOTONIC clock used to stamp each sensor's receive time on the state
        # (the freshness watchdog). Injectable so the runtime can drive it and
        # tests can advance it deterministically; matches the Runtime's mono_fn.
        self._mono_fn = mono_fn
        self.guard = SensorGuard(guard_config)
        # GPS offset calibration (#45): a constant (Δlat, Δlon) added to every
        # incoming fix so a known-wrong receiver can be corrected against a
        # surveyed truth position. Not persisted.
        self.gps_dlat = 0.0
        self.gps_dlon = 0.0
        if bus is not None:
            bus.subscribe(events.NMEA_IN, self._on_nmea)
            bus.subscribe(events.IMU_IN, self._on_imu)

    def _declination(self) -> float:
        """The single central declination (deg, East-positive) used to convert
        every MAGNETIC heading to true. A fixed value if one was configured,
        else the internal model evaluated at the current fix (0.0 with no fix)."""
        if self.declination_deg is not None:
            return self.declination_deg
        pos = self.state.position
        if pos is None:
            return 0.0
        return magnetic_declination_deg(pos.lat, pos.lon)

    @property
    def true_heading_deg(self) -> float:
        """The current best TRUE heading (deg, 0-360).

        ``state.heading_deg`` is always stored in the true frame -- magnetic
        sources are declination-corrected on ingest and true sources pass
        through -- so this feeds ``nmea.encode_hdt`` directly to emit a correct
        HDT sentence for downstream/telemetry consumers."""
        return self.state.heading_deg % 360.0

    async def _on_imu(self, sample) -> None:
        """Store the latest raw IMU sample (accel+gyro) from an AHRS device.

        Auxiliary: it's kept on the state for logging/analysis; the controller
        does not steer on it. Heading still comes via NMEA (HDM), so the nav path
        is unchanged whether or not an IMU is present."""
        self.state.imu = sample
        self.state.imu_received_mono = self._mono_fn()

    # ------------------------------------------------------------------ #
    # GPS offset calibration (#45)
    # ------------------------------------------------------------------ #
    def set_gps_offset(self, true_lat: float, true_lon: float) -> None:
        """Set the offset so the boat's CURRENT fix maps to (true_lat, true_lon).

        The offset is (true position − current corrected fix) and is applied to
        every subsequent fix. If there is no current fix the offset is taken
        relative to the raw (0,0) origin, which simply makes the true position
        the new reported position.
        """
        pos = self.state.position
        cur_lat = pos.lat if pos is not None else 0.0
        cur_lon = pos.lon if pos is not None else 0.0
        # ``pos`` already includes any active offset, so add to the existing one
        # to make the CURRENTLY reported position land exactly on the truth.
        step_lat = true_lat - cur_lat
        step_lon = true_lon - cur_lon
        self.gps_dlat += step_lat
        self.gps_dlon += step_lon
        # Shift the spike-filter's reference by the same step so the offset jump
        # itself isn't mistaken for a GPS glitch (subsequent fixes still get the
        # normal small-step continuity check against the corrected reference).
        self._shift_guard_reference(step_lat, step_lon)
        logger.info(
            "GPS offset set: dlat=%.7f dlon=%.7f", self.gps_dlat, self.gps_dlon
        )

    def clear_gps_offset(self) -> None:
        # Undo the offset on the guard reference too, keeping continuity.
        self._shift_guard_reference(-self.gps_dlat, -self.gps_dlon)
        self.gps_dlat = 0.0
        self.gps_dlon = 0.0
        logger.info("GPS offset cleared")

    def _shift_guard_reference(self, dlat: float, dlon: float) -> None:
        for attr in ("_last_point", "_pending_point"):
            pt = getattr(self.guard, attr)
            if pt is not None:
                setattr(self.guard, attr, GeoPoint(pt.lat + dlat, pt.lon + dlon))

    @property
    def gps_offset_active(self) -> bool:
        return self.gps_dlat != 0.0 or self.gps_dlon != 0.0

    def _apply_offset(self, point: "GeoPoint") -> "GeoPoint":
        if not self.gps_offset_active:
            return point
        return GeoPoint(point.lat + self.gps_dlat, point.lon + self.gps_dlon)

    def _maybe_cog_heading_fallback(self, fix: GpsFix) -> None:
        """Steer on GPS course-over-ground when the compass is stale but moving.

        Called after a fresh fix is ingested. A guided mode steers on
        ``state.heading_deg``; if the compass goes silent the safety governor
        coasts the boat (heading_stale). But when the GPS shows the boat making
        way, its COG is a usable heading proxy, so we adopt it and refresh
        ``heading_received_mono`` -- which the governor watches -- so the guided
        mode keeps steering instead of only coasting.

        Guards:
        * Never fires until a real compass heading has been seen at least once
          (``compass_received_mono is None``) -- a boat that has never had a
          compass keeps its existing behaviour rather than getting a synthesised
          heading out of nowhere.
        * Only fires once the compass has been stale for ``COMPASS_STALE_S``; a
          fresh compass is always preferred.
        * Only fires at/above ``COG_HEADING_MIN_SOG_KNOTS`` -- COG at rest is
          noise, so below that speed we leave the heading stale and let the
          governor coast (the conservative, unchanged behaviour).
        """
        compass_mono = self.state.compass_received_mono
        if compass_mono is None:
            return  # never had a compass; don't invent a heading
        if (self._mono_fn() - compass_mono) <= COMPASS_STALE_S:
            return  # compass still fresh -> keep using it
        if not fix.valid or fix.sog_knots < COG_HEADING_MIN_SOG_KNOTS:
            return  # at rest -> COG is meaningless, keep coasting
        self.state.heading_deg = fix.cog_deg % 360
        self.state.heading_received_mono = self._mono_fn()
        self.state.heading_from_cog = True

    async def _on_nmea(self, sentence: str) -> None:
        for topic, payload in self.handle_sentence(sentence):
            if self.bus is not None:
                await self.bus.publish(topic, payload)

    def handle_sentence(self, sentence: str) -> list[tuple[str, object]]:
        """Parse one sentence and update state. Returns the (topic, payload)
        events that should be published, so the async path can forward them and
        tests can assert on them directly."""
        try:
            parsed = nmea.parse(sentence)
        except nmea.NmeaError as exc:
            logger.debug("dropping bad NMEA: %s", exc)
            return []

        if parsed is None:
            return []

        events_out: list[tuple[str, object]] = []

        if isinstance(parsed, nmea.RMC):
            point = self._apply_offset(parsed.point)
            if parsed.valid and self.guard.check_position(point):
                fix = GpsFix(
                    point=point,
                    sog_knots=parsed.sog_knots,
                    cog_deg=parsed.cog_deg,
                    valid=True,
                )
                self.state.fix = fix
                self.state.fix_seq += 1
                self.state.fix_received_mono = self._mono_fn()
                self.state.sog_knots = parsed.sog_knots
                events_out.append((events.NAV_FIX, fix))
                self._maybe_cog_heading_fallback(fix)
        elif isinstance(parsed, nmea.GGA):
            point = self._apply_offset(parsed.point)
            if parsed.fix_quality > 0 and self.guard.check_position(point):
                # GGA has no course/speed fields.  Carry forward the previous
                # fix's cog so downstream consumers (e.g. anchor-mode closing-
                # speed damping) always have a meaningful course value.  If there
                # is no prior fix, cog defaults to 0.0 (unknown).
                prev = self.state.fix
                cog = prev.cog_deg if prev is not None else 0.0
                fix = GpsFix(
                    point=point,
                    sog_knots=self.state.sog_knots,
                    cog_deg=cog,
                    valid=True,
                )
                self.state.fix = fix
                self.state.fix_seq += 1
                self.state.fix_received_mono = self._mono_fn()
                events_out.append((events.NAV_FIX, fix))
                self._maybe_cog_heading_fallback(fix)
        elif isinstance(parsed, nmea.Heading):
            # Normalise to TRUE heading before storing.
            # Sign convention: True = Magnetic + declination (East-positive).
            # HDT (reference="T") and fully-corrected HDG already carry a true
            # heading — pass through unchanged to avoid double-correction.
            if parsed.reference == "M":
                true_deg = (parsed.heading_deg + self._declination()) % 360
            else:  # "T"
                true_deg = parsed.heading_deg
            if self.guard.check_heading(true_deg):
                now = self._mono_fn()
                self.state.heading_deg = true_deg
                self.state.heading_received_mono = now
                # The real compass just reported: record it and resume steering on
                # it, dropping any COG fallback that was in effect.
                self.state.compass_received_mono = now
                self.state.heading_from_cog = False
                events_out.append((events.NAV_HEADING, true_deg))
        elif isinstance(parsed, nmea.Depth):
            self.state.depth_m = parsed.depth_m
            self.state.depth_received_mono = self._mono_fn()
        elif isinstance(parsed, nmea.APB):
            self.state.last_apb = sentence.strip()
            self.state.has_apb = True
            self.state.apb_cross_track_m = parsed.cross_track_m
            self.state.apb_steer_to = parsed.steer_to
            self.state.apb_bearing_to_dest = parsed.bearing_to_dest
            events_out.append((events.NAV_APB, parsed))

        self.state.heading_rejected = self.guard.heading_rejected
        self.state.position_rejected = self.guard.position_rejected
        return events_out
