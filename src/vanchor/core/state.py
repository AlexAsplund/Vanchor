"""The single shared navigation state.

This replaces the old project's stringly-typed nested-dict "DataNode". It is a
plain typed object that the navigator writes to and that control modes read
from. ``to_dict`` produces the telemetry payload streamed to the UI.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .models import (
    ControlModeName,
    Environment,
    GeoPoint,
    GpsFix,
    ImuSample,
    MotorCommand,
    Waypoint,
)


@dataclass
class NavigationState:
    """Everything the controller knows about the world *as reported by sensors*.

    Crucially this is the boat's *perceived* state (from GPS/compass), not the
    simulator's ground truth -- the controller only ever steers on what the
    sensors tell it, exactly as it would with real hardware.
    """

    # Latest perceived position / motion.
    fix: GpsFix | None = None
    fix_seq: int = 0  # bumped by the navigator on every fresh fix (freshness)
    heading_deg: float = 0.0  # latest compass heading
    # True when heading_deg is currently derived from GPS course-over-ground (the
    # COG fallback, #17) rather than from the compass -- set when the compass goes
    # stale/lost while the boat is making enough way for COG to be meaningful, so
    # a guided mode keeps steering instead of only coasting. Lets telemetry/alarms
    # say "steering on GPS course". Reset to False as soon as the compass returns.
    heading_from_cog: bool = False
    sog_knots: float = 0.0  # speed over ground from GPS
    depth_m: float = 0.0  # water depth under the boat (from a depth sounder)
    # Latest raw IMU/AHRS sample (accel + gyro), if a compass driver exposes one
    # (e.g. HWT901B). Auxiliary — not consumed by the controller yet; surfaced for
    # logging/analysis and future fusion. None when no IMU-capable device is active.
    imu: ImuSample | None = None

    # --- GNSS/INS fusion outputs (M9N UBX + HWT901B IMU) ------------------- #
    # All optional/additive: populated only when the fusion has the sensors it
    # needs; None/False otherwise, so every existing hardware combo is unchanged.
    yaw_rate_dps: float | None = None    # sensor/fused yaw rate (deg/s), else None
    ground_vel_n_mps: float | None = None  # fused NED ground velocity, north (m/s)
    ground_vel_e_mps: float | None = None  # fused NED ground velocity, east (m/s)
    vertical_vel_mps: float | None = None  # NED down velocity (only from a 3D-velocity source)
    crab_deg: float | None = None        # course-minus-heading (leeway/set), signed deg
    dead_reckoning: bool = False         # fusion is coasting on IMU through a GPS gap
    velocity_measured: bool = False      # ground velocity is from a real velocity vector
    interference_comp_deg: float = 0.0   # experimental motor-interference heading remedy applied

    # Sensor-anomaly protection: how many implausible readings were rejected.
    heading_rejected: int = 0
    position_rejected: int = 0

    # --- Live sonar/fishfinder ingest + chart divergence (#45) ----------- #
    # Written by nav/sonar.ingest when a live sounding is merged with the chart.
    # ``sonar_depth_m`` is the latest measured depth from the fishfinder;
    # ``charted_depth_m`` is what the imported DepthMap says at the boat's
    # position (0 = no chart data nearby); ``depth_divergence_m`` is
    # measured - charted (NEGATIVE = sounder shallower than the chart, the
    # grounding-risk case); ``depth_divergence_alert`` latches True while they
    # disagree beyond tolerance so telemetry/UI can flag an uncharted shoal.
    sonar_depth_m: float = 0.0
    charted_depth_m: float = 0.0
    depth_divergence_m: float = 0.0
    depth_divergence_alert: bool = False

    # --- Sensor staleness (freshness watchdog) --------------------------- #
    # ``time.monotonic()`` (via the navigator's injectable clock) stamp of when
    # each primary input was last ingested, so the governor can force a safe
    # coast when a sensor a mode relies on goes silent (a dead compass in
    # heading-hold, a frozen depth feeding the shallow-water stop). ``None`` =
    # never received: treated as "fresh until the first sample" by the governor
    # (so a harness that never stamps them can't be false-tripped), while the
    # health telemetry reports it as null.
    fix_received_mono: float | None = None
    heading_received_mono: float | None = None
    # Monotonic stamp of the last REAL compass heading. Distinct from
    # heading_received_mono (which the COG fallback ALSO refreshes so the governor
    # sees a fresh heading and keeps steering): this tracks only the compass, so
    # the navigator can tell when the compass itself has gone stale and the COG
    # fallback may take over. ``None`` = no compass heading has ever arrived.
    compass_received_mono: float | None = None
    depth_received_mono: float | None = None
    imu_received_mono: float | None = None

    # --- Control-loop supervision ---------------------------------------- #
    # Set to a short description when a control tick raises (the loop caught it,
    # zeroed the motor, and kept running); cleared after the next clean tick.
    controller_fault: str | None = None
    # ``time.monotonic()`` stamp of the last control-loop iteration, so a future
    # supervisor can compute the loop's heartbeat age. 0.0 until the loop runs.
    controller_last_tick_monotonic: float = 0.0

    # Active behaviour and its parameters.
    mode: ControlModeName = ControlModeName.MANUAL
    anchor: GeoPoint | None = None
    anchor_radius_m: float = 5.0
    anchor_heading: float = 0.0  # heading to hold while station-keeping
    target_heading: float = 0.0
    drift_target_knots: float = 0.5  # target speed-over-ground for Drift mode

    # --- Contour-follow (#57): hold a depth contour (isobath). ----------- #
    contour_target_depth_m: float = 0.0
    contour_side: str = "deep"  # "deep" | "shallow" -- which way to turn

    # --- Circle / orbit (#58): orbit a centre at a fixed radius. ---------- #
    orbit_center: GeoPoint | None = None
    orbit_radius_m: float = 20.0
    orbit_direction: str = "cw"  # "cw" | "ccw"

    # --- Trolling pattern (#59): sinusoidal S-curve weave. --------------- #
    trolling_base_heading: float = 0.0
    trolling_amplitude_deg: float = 20.0
    trolling_period_s: float = 20.0

    waypoints: list[Waypoint] = field(default_factory=list)
    active_waypoint: int = 0
    # Transient per-waypoint speed request: set by WaypointMode when the boat
    # ARRIVES at a waypoint carrying a speed attribute; consumed (and cleared)
    # by the controller the same tick, which routes it into the throttle-%
    # override or the Cruise Control (knots) channel. ("throttle_pct"|"speed_kn", value).
    route_speed_request: tuple[str, float] | None = None
    route_on_arrival: str = "none"  # "anchor" | "stop" | "none" when route done
    route_complete: bool = False
    # When True, reaching the last waypoint wraps back to the first (closed loop,
    # e.g. the "around island" route) instead of completing -- the boat circles
    # continuously. Set via the goto/load_route "loop" flag.
    route_loop: bool = False
    # When True, reaching either END of the route reverses direction and runs it
    # back the other way -- a continuous there-and-back "patrol" (distinct from
    # route_loop, which closes the ring). Set via the goto/load_route "patrol" flag.
    route_patrol: bool = False

    # --- Work Area mode: visit spots, hold at each, then advance. -------- #
    # The spots are state.waypoints; active_waypoint is the current spot. While
    # holding, the UI shows a big "Go to next spot" button (driven by work_holding).
    work_holding: bool = False             # currently holding position at a spot
    work_dwell_remaining_s: float = 0.0    # countdown to auto-advance (timed mode)
    work_next_requested: bool = False      # transient: the "next spot" button press

    # --- Return-to-Launch (#61): the recorded home/launch point. --------- #
    launch: GeoPoint | None = None  # first good fix, or set via set_launch
    rtl_recommended: bool = False  # battery can *just* make it home -> UI prompt

    # --- Man-overboard (#63): the MOB mark to return to. ----------------- #
    mob: GeoPoint | None = None
    mob_active: bool = False

    # Latest actuator command produced by the controller.
    motor_command: MotorCommand = field(default_factory=MotorCommand)
    # Physical steer range of the trolling motor (deg each side of the bow). The
    # real motor rotates a bit past 360° total (cable-limited); this maps the
    # normalized steering command to a real azimuth for display/telemetry.
    max_steer_angle_deg: float = 35.0

    # --- Vectored / azimuth station-keeping (#35) -------------------------- #
    # True while the anchor hold is running the opt-in vectored (wide-azimuth)
    # law; the commanded motor azimuth (deg off the bow, signed, + = starboard)
    # it is currently demanding. Written by AnchorHoldMode each tick it holds;
    # reset by the controller on a mode change.
    stationkeep_vectored: bool = False
    stationkeep_azimuth_deg: float = 0.0

    # --- Hold quality metric (#34) --------------------------------------- #
    # Rolling holding-quality numbers, updated by the controller every tick an
    # anchor mode (PID anchor_hold OR learned anchor_ml) is station-keeping:
    # RMS radial error and % of time within the anchor radius over a trailing
    # ~window_s. Reset when the mark is cleared/moved; frozen while paused.
    hold_rms_m: float = 0.0
    hold_pct_in_radius: float = 0.0
    hold_window_s: float = 60.0
    hold_holding_s: float = 0.0  # holding time accumulated (caps at window)

    # Diagnostics surfaced to the UI so a human can see *why* the controller
    # is doing what it is doing.
    distance_to_anchor_m: float = 0.0
    distance_to_waypoint_m: float = 0.0
    cross_track_m: float = 0.0
    bearing_to_dest: float = 0.0
    last_apb: str | None = None

    # Shared wind/current estimator's learned environmental drift velocity (world
    # frame), published every control tick by the persistent
    # ``WindCurrentEstimator`` and consumed by waypoint crab feed-forward, drift
    # mode and anchor hold. ``est_drift_mps`` / ``est_drift_dir`` are the magnitude
    # and the compass direction the drift pushes TOWARD; east/north are the
    # components; ``settled`` gates feed-forward consumers (they fall back to pure
    # feedback until it is True); ``confidence`` is a 0..1 quality signal.
    est_drift_mps: float = 0.0
    est_drift_dir: float = 0.0  # degrees the drift pushes toward
    est_drift_east: float = 0.0
    est_drift_north: float = 0.0
    est_drift_settled: bool = False
    est_drift_confidence: float = 0.0

    # Latest parsed APB (decomposed to keep core/ free of nav imports) used by
    # the external-autopilot FollowAPB mode.
    has_apb: bool = False
    apb_cross_track_m: float = 0.0
    apb_steer_to: str = "L"
    apb_bearing_to_dest: float = 0.0

    @property
    def position(self) -> GeoPoint | None:
        return self.fix.point if self.fix else None

    def to_dict(self) -> dict:
        pos = self.position
        return {
            "mode": self.mode.value,
            "position": {"lat": pos.lat, "lon": pos.lon} if pos else None,
            "heading_deg": round(self.heading_deg, 2),
            "heading_from_cog": self.heading_from_cog,
            "sog_knots": round(self.sog_knots, 2),
            "fusion": {
                "yaw_rate_dps": (round(self.yaw_rate_dps, 2)
                                 if self.yaw_rate_dps is not None else None),
                "ground_vel_n_mps": (round(self.ground_vel_n_mps, 3)
                                     if self.ground_vel_n_mps is not None else None),
                "ground_vel_e_mps": (round(self.ground_vel_e_mps, 3)
                                     if self.ground_vel_e_mps is not None else None),
                "vertical_vel_mps": (round(self.vertical_vel_mps, 3)
                                     if self.vertical_vel_mps is not None else None),
                "crab_deg": (round(self.crab_deg, 1) if self.crab_deg is not None else None),
                "dead_reckoning": self.dead_reckoning,
                "velocity_measured": self.velocity_measured,
                "interference_comp_deg": round(self.interference_comp_deg, 2),
            },
            "depth_m": round(self.depth_m, 1),
            "imu": asdict(self.imu) if self.imu else None,
            "sensors": {
                "heading_rejected": self.heading_rejected,
                "position_rejected": self.position_rejected,
            },
            # Live sonar vs charted depth (#45): latest measured depth, the
            # charted depth under the boat, their signed difference, and the
            # divergence alarm (sounder materially shallower than the chart).
            "sonar": {
                "depth_m": round(self.sonar_depth_m, 1),
                "charted_depth_m": round(self.charted_depth_m, 1),
                "divergence_m": round(self.depth_divergence_m, 1),
                "divergence_alert": self.depth_divergence_alert,
            },
            "anchor": (
                {"lat": self.anchor.lat, "lon": self.anchor.lon}
                if self.anchor
                else None
            ),
            "anchor_radius_m": self.anchor_radius_m,
            "anchor_heading": round(self.anchor_heading, 2),
            "target_heading": round(self.target_heading, 2),
            "drift_target_knots": round(self.drift_target_knots, 2),
            "waypoints": [
                {"name": w.name, "lat": w.point.lat, "lon": w.point.lon,
                 "heading": w.heading,
                 "throttle_pct": w.throttle_pct, "speed_kn": w.speed_kn}
                for w in self.waypoints
            ],
            "active_waypoint": self.active_waypoint,
            "route_complete": self.route_complete,
            "route_loop": self.route_loop,
            "route_patrol": self.route_patrol,
            "work_holding": self.work_holding,
            "work_dwell_remaining_s": round(self.work_dwell_remaining_s, 1),
            "work_spot_count": len(self.waypoints),
            "motor": {
                **asdict(self.motor_command),
                # Trolling-motor azimuth relative to the bow (deg), for the UI
                # motor-direction indicator. Reverse thrust points it astern.
                "steer_angle_deg": round(
                    self.motor_command.steering * self.max_steer_angle_deg
                    + (180.0 if self.motor_command.thrust < 0 else 0.0),
                    1,
                ),
            },
            "distance_to_anchor_m": round(self.distance_to_anchor_m, 2),
            "stationkeep": {
                "vectored": self.stationkeep_vectored,
                "azimuth_deg": round(self.stationkeep_azimuth_deg, 1),
            },
            "hold_quality": {
                "rms_m": round(self.hold_rms_m, 2),
                "pct_in_radius": round(self.hold_pct_in_radius, 1),
                "window_s": self.hold_window_s,
                "holding_s": round(self.hold_holding_s, 1),
            },
            "distance_to_waypoint_m": round(self.distance_to_waypoint_m, 2),
            "cross_track_m": round(self.cross_track_m, 2),
            "bearing_to_dest": round(self.bearing_to_dest, 2),
            "est_drift_mps": round(self.est_drift_mps, 3),
            "est_drift_dir": round(self.est_drift_dir, 1),
            "est_drift_settled": self.est_drift_settled,
            "last_apb": self.last_apb,
            "launch": {
                "lat": self.launch.lat if self.launch else None,
                "lon": self.launch.lon if self.launch else None,
                "set": self.launch is not None,
            },
            "rtl_recommended": self.rtl_recommended,
            "mob": {
                "active": self.mob_active,
                "lat": self.mob.lat if self.mob else None,
                "lon": self.mob.lon if self.mob else None,
            },
        }
