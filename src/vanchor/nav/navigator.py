"""The navigator turns raw NMEA sentences into updates on the shared state.

It is the only component that writes the *perceived* position/heading, keeping a
single, well-defined path from "bytes off the wire" to "what the controller
believes". It is driven both synchronously (``handle_sentence`` for tests) and
asynchronously (subscribed to the ``nmea.in`` topic at runtime).
"""

from __future__ import annotations

import logging

from ..core import events
from ..core.events import EventBus
from ..core.models import GeoPoint, GpsFix
from ..core.state import NavigationState
from . import nmea
from .guard import SensorGuard, SensorGuardConfig

logger = logging.getLogger("vanchor.navigator")


class Navigator:
    def __init__(
        self,
        state: NavigationState,
        bus: EventBus | None = None,
        guard_config: SensorGuardConfig | None = None,
    ) -> None:
        self.state = state
        self.bus = bus
        self.guard = SensorGuard(guard_config)
        # GPS offset calibration (#45): a constant (Δlat, Δlon) added to every
        # incoming fix so a known-wrong receiver can be corrected against a
        # surveyed truth position. Not persisted.
        self.gps_dlat = 0.0
        self.gps_dlon = 0.0
        if bus is not None:
            bus.subscribe(events.NMEA_IN, self._on_nmea)
            bus.subscribe(events.IMU_IN, self._on_imu)

    async def _on_imu(self, sample) -> None:
        """Store the latest raw IMU sample (accel+gyro) from an AHRS device.

        Auxiliary: it's kept on the state for logging/analysis; the controller
        does not steer on it. Heading still comes via NMEA (HDM), so the nav path
        is unchanged whether or not an IMU is present."""
        self.state.imu = sample

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
                self.state.sog_knots = parsed.sog_knots
                events_out.append((events.NAV_FIX, fix))
        elif isinstance(parsed, nmea.GGA):
            point = self._apply_offset(parsed.point)
            if parsed.fix_quality > 0 and self.guard.check_position(point):
                # GGA has no speed; preserve last known sog.
                fix = GpsFix(
                    point=point, sog_knots=self.state.sog_knots, valid=True
                )
                self.state.fix = fix
                self.state.fix_seq += 1
                events_out.append((events.NAV_FIX, fix))
        elif isinstance(parsed, nmea.Heading):
            if self.guard.check_heading(parsed.heading_deg):
                self.state.heading_deg = parsed.heading_deg
                events_out.append((events.NAV_HEADING, parsed.heading_deg))
        elif isinstance(parsed, nmea.Depth):
            self.state.depth_m = parsed.depth_m
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
