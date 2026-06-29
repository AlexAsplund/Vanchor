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
    sog_knots: float = 0.0  # speed over ground from GPS
    depth_m: float = 0.0  # water depth under the boat (from a depth sounder)

    # Sensor-anomaly protection: how many implausible readings were rejected.
    heading_rejected: int = 0
    position_rejected: int = 0

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

    # Diagnostics surfaced to the UI so a human can see *why* the controller
    # is doing what it is doing.
    distance_to_anchor_m: float = 0.0
    distance_to_waypoint_m: float = 0.0
    cross_track_m: float = 0.0
    bearing_to_dest: float = 0.0
    last_apb: str | None = None

    # Anchor controller's learned environmental drift (for display / feed-forward).
    est_drift_mps: float = 0.0
    est_drift_dir: float = 0.0  # degrees the drift pushes toward

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
            "sog_knots": round(self.sog_knots, 2),
            "depth_m": round(self.depth_m, 1),
            "sensors": {
                "heading_rejected": self.heading_rejected,
                "position_rejected": self.position_rejected,
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
                {"name": w.name, "lat": w.point.lat, "lon": w.point.lon}
                for w in self.waypoints
            ],
            "active_waypoint": self.active_waypoint,
            "route_loop": self.route_loop,
            "route_patrol": self.route_patrol,
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
            "distance_to_waypoint_m": round(self.distance_to_waypoint_m, 2),
            "cross_track_m": round(self.cross_track_m, 2),
            "bearing_to_dest": round(self.bearing_to_dest, 2),
            "est_drift_mps": round(self.est_drift_mps, 3),
            "est_drift_dir": round(self.est_drift_dir, 1),
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
