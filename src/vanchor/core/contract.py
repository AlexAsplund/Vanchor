"""The vanchor API contract: a versioned, self-describing description of the
telemetry payload and the accepted commands.

The lesson borrowed from SignalK: **the data model is the contract**. Telemetry
grew organically into a big dict; this pins it into a declared, versioned,
self-describing shape (types + units + one-line descriptions), exposed at
``GET /api/contract`` so any client (or a third-party frontend) can introspect
it. A drift test (``tests/test_contract.py``) fails if a telemetry key or a
command type is added without declaring it here -- keeping the contract honest.

Bump ``SCHEMA_VERSION`` on any breaking change to a field's meaning/type/unit;
additive fields are a minor bump.
"""
from __future__ import annotations

SCHEMA_VERSION = "1.0"

# key -> {type, [unit], desc}. type is a coarse JSON-ish type ("number",
# "integer", "boolean", "string", "object", "array", or "<type>|null").
TELEMETRY_FIELDS: dict[str, dict] = {
    # --- core navigation state ---
    "mode": {"type": "string", "desc": "active control mode (see /api/contract commands)"},
    "position": {"type": "object|null", "desc": "current GPS fix {lat, lon}"},
    "heading_deg": {"type": "number", "unit": "deg", "desc": "true heading, 0-360"},
    "heading_from_cog": {"type": "boolean", "desc": "heading is derived from GPS course (compass lost)"},
    "target_heading": {"type": "number|null", "unit": "deg", "desc": "heading-hold target"},
    "sog_knots": {"type": "number", "unit": "kn", "desc": "speed over ground"},
    "depth_m": {"type": "number", "unit": "m", "desc": "measured water depth"},
    "sim_enabled": {"type": "boolean", "desc": "running the simulator (vs real hardware)"},
    "truth": {"type": "object|null", "desc": "simulator ground-truth boat state (sim only)"},
    "imu": {"type": "object|null", "desc": "latest AHRS accel+gyro sample"},
    "sensors": {"type": "object", "desc": "raw sensor snapshot (fix/heading/depth)"},
    "fusion": {"type": "object", "desc": "GNSS/INS fusion outputs (yaw rate, ground "
               "velocity, crab, dead-reckoning) when a UBX GPS + IMU are fused"},
    # --- anchor / position hold ---
    "anchor": {"type": "object|null", "desc": "the held anchor point {lat, lon}"},
    "anchor_radius_m": {"type": "number", "unit": "m", "desc": "watch-circle radius"},
    "anchor_heading": {"type": "number|null", "unit": "deg", "desc": "heading captured at anchor drop"},
    "distance_to_anchor_m": {"type": "number", "unit": "m", "desc": "current distance from the anchor"},
    "hold_quality": {"type": "object", "desc": "hold quality metric (RMS error, % in radius)"},
    "anchor_ml": {"type": "object", "desc": "learned station-keeper telemetry (residual scale, guard)"},
    # --- routing / waypoints ---
    "active_waypoint": {"type": "object|null", "desc": "the waypoint currently being steered to"},
    "waypoints": {"type": "array", "desc": "the active route waypoints"},
    "distance_to_waypoint_m": {"type": "number|null", "unit": "m", "desc": "range to the active waypoint"},
    "bearing_to_dest": {"type": "number|null", "unit": "deg", "desc": "bearing to the destination"},
    "cross_track_m": {"type": "number", "unit": "m", "desc": "cross-track error off the leg"},
    "nav": {"type": "object", "desc": "guided-nav status (paused, suspended mode, leg index)"},
    "last_apb": {"type": "object|null", "desc": "last received APB autopilot sentence (Follow-APB)"},
    "route_complete": {"type": "boolean", "desc": "the active route has finished (all marks reached)"},
    "manual_course": {"type": "object|null", "desc": "manual course-hold line: {bearing, lat, lon} of the anchored track"},
    "auto_apb": {"type": "object", "desc": "auto Follow-APB: {enabled, engaged}"},
    "route_loop": {"type": "object|null", "desc": "loop-route state"},
    "route_patrol": {"type": "object|null", "desc": "patrol-route state"},
    # --- fishing / survey modes ---
    "contour": {"type": "object", "desc": "contour-follow state"},
    "orbit": {"type": "object|null", "desc": "orbit-mode state"},
    "trolling": {"type": "object|null", "desc": "ground-track trolling state"},
    "stationkeep": {"type": "object", "desc": "vectored station-keeping state"},
    "work_holding": {"type": "boolean", "desc": "work-area: holding at a spot"},
    "work_spot_count": {"type": "integer", "desc": "work-area: number of spots"},
    "work_dwell_remaining_s": {"type": "number|null", "unit": "s", "desc": "work-area: dwell time left"},
    "drift_target_knots": {"type": "number", "unit": "kn", "desc": "drift-mode along-axis target speed"},
    # --- environment / drift estimate ---
    "environment": {"type": "object", "desc": "wind/current estimate + configured environment"},
    "est_drift_mps": {"type": "number", "unit": "m/s", "desc": "estimated drift speed"},
    "est_drift_dir": {"type": "number", "unit": "deg", "desc": "estimated drift direction (toward)"},
    "est_drift_settled": {"type": "boolean", "desc": "drift estimate has converged"},
    # --- actuator / control ---
    "motor": {"type": "object", "desc": "applied motor command {thrust, steering}"},
    "steering": {"type": "object", "desc": "steering feedback (angle, wrap, ok)"},
    "throttle_override": {"type": "object", "desc": "manual throttle override state"},
    "cruise": {"type": "object", "desc": "cruise-control (speed hold) state"},
    # --- safety / health / devices ---
    "safety": {"type": "object", "desc": "safety-governor status (clamps, alarms, failsafes)"},
    "safety_geometry": {"type": "object", "desc": "no-go zones / min-depth / fix-failsafe geometry"},
    "health": {"type": "object", "desc": "per-sensor freshness + controller-loop health"},
    "devices": {"type": "object", "desc": "per-device {source, connected, healthy}"},
    "mode_availability": {"type": "object", "desc": "per-mode {available, reason} from device connectivity"},
    "link": {"type": "object", "desc": "client link / failsafe status"},
    "rtl_recommended": {"type": "boolean", "desc": "return-to-launch recommended (e.g. low battery)"},
    "mob": {"type": "object|null", "desc": "man-overboard mark state"},
    "launch": {"type": "object|null", "desc": "captured launch point (for RTL)"},
    # --- battery / power ---
    "battery": {"type": "object", "desc": "battery snapshot (SoC, voltage, current, range)"},
    # --- charts / depth ---
    "depth_count": {"type": "integer", "desc": "number of accumulated depth soundings"},
    "depth_points": {"type": "array", "desc": "accumulated depth soundings for the overlay"},
    "sonar": {"type": "object", "desc": "live sonar vs chart divergence (grounding alert)"},
    # --- track / trip / boat / misc ---
    "track": {"type": "object", "desc": "recorded track summary"},
    "trip": {"type": "object", "desc": "trip odometer + timing"},
    "boat": {"type": "object", "desc": "active boat profile summary"},
    "gps_offset": {"type": "object", "desc": "GPS offset calibration (surveyed correction)"},
    "calibration": {"type": "object", "desc": "calibration/auto-tune status"},
    "debug": {"type": "object", "desc": "debug-recorder status"},
    "replay": {"type": "object", "desc": "replay playback status"},
}

# command type -> {desc}. Every ctype the controller/server accepts is declared.
COMMANDS: dict[str, dict] = {
    "manual": {"desc": "direct thrust+steering (manual mode)"},
    "stop": {"desc": "zero the motor immediately (always available)"},
    "anchor_hold": {"desc": "PID position hold at a point"},
    "anchor_ml": {"desc": "learned (Smart) position hold"},
    "anchor_leif": {"desc": "pure learned full-azimuth position hold (experimental)"},
    "heading_hold": {"desc": "hold a compass heading"},
    "goto": {"desc": "go to a waypoint / chart-tapped point"},
    "load_route": {"desc": "load + follow a route"},
    "follow_apb": {"desc": "follow an external chartplotter's APB route"},
    "work_area": {"desc": "survey/visit a set of spots"},
    "drift": {"desc": "controlled drift along an axis"},
    "contour_follow": {"desc": "follow a depth contour"},
    "orbit": {"desc": "orbit a centre point"},
    "trolling": {"desc": "ground-track S-curve trolling"},
    "cruise": {"desc": "set a cruise (speed-hold) target"},
    "set_throttle": {"desc": "set a throttle percentage"},
    "jog": {"desc": "nudge the anchor hold point"},
    "backtrack": {"desc": "retrace the recorded track"},
    "record": {"desc": "start/stop track recording"},
    "pause_nav": {"desc": "pause the active guided navigation"},
    "resume_nav": {"desc": "resume paused navigation"},
    "next_spot": {"desc": "advance to the next work-area spot"},
    "mob": {"desc": "drop a man-overboard mark + return"},
    "mob_clear": {"desc": "clear the man-overboard mark"},
    "set_launch": {"desc": "capture the launch point for RTL"},
    "set_min_depth": {"desc": "set the minimum-depth safety limit"},
    "set_auto_apb": {"desc": "enable/disable auto Follow-APB engage on a live APB feed"},
    "set_fix_failsafe": {"desc": "enable/disable the GPS-fix-loss failsafe"},
    "set_nogo_zones": {"desc": "set the no-go zone geometry"},
}


def build_contract(envelope_version: int | None = None) -> dict:
    """The full self-describing contract for ``GET /api/contract``."""
    return {
        "schema_version": SCHEMA_VERSION,
        "envelope_version": envelope_version,
        "units": "angles in degrees, distances in metres, speed in knots (SOG) "
                 "or m/s (drift), depth in metres",
        "telemetry": TELEMETRY_FIELDS,
        "commands": COMMANDS,
    }
