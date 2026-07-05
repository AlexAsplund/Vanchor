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
from dataclasses import replace

from ..core import events
from ..core.events import EventBus
from ..core.models import GeoPoint, GpsFix
from ..core.state import NavigationState
from . import nmea
from .calibration import GAIN_KEYS, CaptureBuffer, FusionCalibration
from .fusion import NavFusion
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


# --- Magnetic declination (#47) --------------------------------------------- #
# Converts a MAGNETIC compass heading (HDM / uncorrected HDG, reference="M") into
# the TRUE frame the control stack steers in, via the full degree-12 WMM2025
# model (see vanchor.nav.wmm), accurate to a fraction of a degree worldwide.
# AUTO declination (``declination_deg=None``) is now the DEFAULT for real magnetic
# compasses; the app forces a fixed 0.0 when the compass is the simulator (a
# zero-declination, true-heading world) so simulator behaviour is unchanged.
from .wmm import declination_deg as magnetic_declination_deg  # re-export  # noqa: E402


class Navigator:
    def __init__(
        self,
        state: NavigationState,
        bus: EventBus | None = None,
        guard_config: SensorGuardConfig | None = None,
        *,
        declination_deg: float | None = 0.0,
        mono_fn=time.monotonic,
        fusion: "NavFusion | None" = None,
    ) -> None:
        self.state = state
        self.bus = bus
        # Optional GNSS/INS fusion (M9N UBX velocity + HWT901B IMU). ADDITIVE: it
        # only fills state.yaw_rate_dps / ground_vel_* / crab_deg / dead_reckoning;
        # heading, position and control are unchanged, so every existing hardware
        # combo behaves exactly as before. Fed from whatever sensors are present
        # (works partially for NMEA + IMU too).
        self.fusion = fusion
        self._last_imu_mono: float | None = None
        # Fusion calibration (nav.calibration): a gyro-bias correction subtracted
        # from the IMU yaw rate, plus a still-capture buffer. Gains are applied to
        # ``fusion`` directly. All no-ops until a calibration is applied / a
        # capture is running.
        self._gyro_bias = 0.0
        self._capture: CaptureBuffer | None = None
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
            bus.subscribe(events.GPS_FIX_IN, self._on_gps_fix)
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
        """Store the latest raw IMU sample (accel+gyro) from an AHRS device, and
        (when fusion is enabled) feed its yaw rate into the GNSS/INS filter.

        Still auxiliary to the control path -- fusion only fills the additive
        state.fusion_* fields; heading/steering are unchanged whether or not an
        IMU is present."""
        now = self._mono_fn()
        self.state.imu = sample
        self.state.imu_received_mono = now
        if self._capture is not None:
            self._capture.add_imu(sample.gz, now)   # RAW rate -> measures the bias
        if self.fusion is not None:
            dt = (now - self._last_imu_mono) if self._last_imu_mono is not None else 0.0
            self._last_imu_mono = now
            # Feed the bias-corrected rate (calibration removes the resting offset).
            self.fusion.update_imu(sample.gz - self._gyro_bias, dt)
            self._apply_fusion(now)

    async def _on_gps_fix(self, fix: GpsFix) -> None:
        """Ingest a rich GpsFix (with velocity/accuracy) from a UBX GPS driver --
        the path NMEA can't carry. Mirrors the RMC ingestion but preserves the
        velocity vector, and feeds the fusion filter."""
        point = self._apply_offset(fix.point)
        if not (fix.valid and self.guard.check_position(point)):
            return
        # keep the receiver's velocity/accuracy, re-point through the GPS offset
        fix = replace(fix, point=point)
        self.state.fix = fix
        self.state.fix_seq += 1
        self.state.fix_received_mono = self._mono_fn()
        self.state.sog_knots = fix.sog_knots
        self._maybe_cog_heading_fallback(fix, has_fresh_course=True)
        self._feed_fusion_gps(fix)
        if self.bus is not None:
            await self.bus.publish(events.NAV_FIX, fix)

    def _feed_fusion_gps(self, fix: GpsFix) -> None:
        now = self._mono_fn()
        if self._capture is not None:
            # Record the best available velocity (measured vector, else derived
            # from SOG/COG) + position, so a still capture measures GPS noise.
            if fix.has_velocity:
                vn, ve = fix.vel_n_mps, fix.vel_e_mps
            else:
                sog = fix.sog_knots * 0.5144444
                c = math.radians(fix.cog_deg)
                vn, ve = sog * math.cos(c), sog * math.sin(c)
            self._capture.add_gps(fix.point.lat, fix.point.lon, vn, ve, now)
        if self.fusion is None:
            return
        self.fusion.update_gps(
            fix.point, now,
            vel_n_mps=fix.vel_n_mps, vel_e_mps=fix.vel_e_mps, vel_d_mps=fix.vel_d_mps,
            cog_deg=fix.cog_deg, sog_mps=fix.sog_knots * 0.5144444,
        )
        self._apply_fusion(now)

    # -- fusion calibration (still-capture system-ID; see nav.calibration) --- #
    def apply_calibration(self, cal: FusionCalibration) -> None:
        """Apply a calibration live: subtract the gyro bias and set the fusion
        gains (a ``None`` override resets that gain to the NavFusion default)."""
        self._gyro_bias = cal.gyro_bias_dps
        if self.fusion is not None:
            overrides = cal.gain_overrides()
            defaults = NavFusion()  # fresh instance carries the default gains
            for key in GAIN_KEYS:
                setattr(self.fusion, key, overrides.get(key, getattr(defaults, key)))

    def start_capture(self) -> None:
        """Begin buffering raw sensor samples for a calibration capture."""
        self._capture = CaptureBuffer()

    def stop_capture(self) -> CaptureBuffer | None:
        """End the capture and return its buffer (None if none was running)."""
        buf, self._capture = self._capture, None
        return buf

    def capture_status(self) -> tuple[bool, int, float]:
        """(capturing, samples, seconds) for the live capture."""
        if self._capture is None:
            return (False, 0, 0.0)
        return (True, self._capture.count, round(self._capture.duration_s, 1))

    def _apply_fusion(self, now: float) -> None:
        """Publish the fusion outputs into the (additive) state fields."""
        if self.fusion is None:
            return
        fs = self.fusion.step(now)
        self.state.yaw_rate_dps = fs.yaw_rate_dps
        self.state.ground_vel_n_mps = fs.ground_vel_n_mps
        self.state.ground_vel_e_mps = fs.ground_vel_e_mps
        self.state.vertical_vel_mps = fs.vertical_vel_mps
        self.state.crab_deg = fs.crab_deg
        self.state.dead_reckoning = fs.dead_reckoning
        self.state.velocity_measured = fs.velocity_measured

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

    def _maybe_cog_heading_fallback(
        self, fix: GpsFix, *, has_fresh_course: bool
    ) -> None:
        """Steer on GPS course-over-ground when the compass is stale but moving.

        Called after a fresh fix is ingested. A guided mode steers on
        ``state.heading_deg``; if the compass goes silent the safety governor
        coasts the boat (heading_stale). But when the GPS shows the boat making
        way, its COG is a usable heading proxy, so we adopt it and refresh
        ``heading_received_mono`` -- which the governor watches -- so the guided
        mode keeps steering instead of only coasting.

        Guards:
        * Only fires for a fix that genuinely carries fresh course/speed
          (``has_fresh_course`` -- an RMC/VTG fix). A GGA has no course/speed
          fields, so a GGA-sourced fix carries the PREVIOUS fix's forwarded
          ``cog``/``sog``; if the boat has actually stopped but only GGA is still
          arriving, that stale sog can sit above the trust threshold forever and
          keep refreshing the heading on a dead course. Never let a fix without
          real course/speed drive the fallback.
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
        if not has_fresh_course:
            return  # GGA carries a stale forwarded cog/sog -- never trust it here
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
                # RMC genuinely carries course/speed over ground -> may drive the
                # COG heading fallback.
                self._maybe_cog_heading_fallback(fix, has_fresh_course=True)
                self._feed_fusion_gps(fix)
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
                self._feed_fusion_gps(fix)
                # GGA has no course/speed of its own -- the cog/sog above are
                # forwarded from an earlier fix and may be stale (the boat may
                # have since stopped), so it must NOT drive the COG fallback.
                self._maybe_cog_heading_fallback(fix, has_fresh_course=False)
        elif isinstance(parsed, nmea.Heading):
            # NOTE (#41 mag calibration wiring -- BLOCKED, no raw-mag pipeline):
            # the stored hard/soft-iron calibration must be applied to a RAW
            # magnetometer vector *before* the magnetic heading is derived. That
            # correction now lives, tested, in
            # controller.calibration.MagCalibration.heading_deg(raw). It cannot be
            # applied here yet: this branch only ever receives an ALREADY-COMPUTED
            # heading (the hwt901b driver fuses the AHRS yaw and emits HDT/HDM),
            # and NavigationState carries no mag_x/y/z. To finish the wiring:
            #   1. surface the raw magnetometer vector (state.mag_x/y/z or an
            #      IMU-style sample) from the compass driver into the nav pipeline;
            #   2. give the Navigator the loaded MagCalibration (from
            #      MagCalibrationStore) and, when a raw vector is present, derive
            #      the magnetic heading via cal.heading_deg(raw) instead of trusting
            #      the driver's fused heading, then declination-correct as below.
            # Until (1) exists the correction is a no-op no matter where it is
            # called, so it stays out of the hot path rather than guessing a frame.
            #
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
                if self._capture is not None:
                    self._capture.add_heading(true_deg, now)
                if self.fusion is not None:
                    self.fusion.update_compass(true_deg)
                    self._apply_fusion(now)
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
