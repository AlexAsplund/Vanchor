"""The controller: the heart of the event-driven system.

Responsibilities:
  * Own the active control mode and the steering helm.
  * On every control tick, ask the mode for a setpoint, turn it into a concrete
    :class:`MotorCommand` via the helm, and hand it to the motor controller.
  * Translate inbound commands (from the UI/bus) into mode/state changes.

The control logic is exposed as a synchronous ``control_tick(dt)`` so it can be
driven deterministically by tests, and wrapped by an async ``run`` loop for the
live system.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time

from ..core import events
from ..core.events import EventBus
from ..core.geo import angle_difference, destination_point, normalize_deg
from ..core.models import (
    ControlModeName,
    GeoPoint,
    GuidedSetpoint,
    ManualSetpoint,
    MotorCommand,
    Setpoint,
    Waypoint,
)
from ..core.pid import PID
from ..core.state import NavigationState
from ..hardware.interfaces import MotorController
from ..nav.track import TrackRecorder
from .estimator import EstimatorConfig, WindCurrentEstimator
from .modes import (
    AnchorConfig,
    AnchorHoldMode,
    ContourConfig,
    ContourFollowMode,
    ControlMode,
    DriftConfig,
    DriftMode,
    FollowApbConfig,
    FollowApbMode,
    HeadingHoldMode,
    ManualMode,
    OrbitConfig,
    OrbitMode,
    TrollingConfig,
    TrollingMode,
    WaypointConfig,
    WaypointMode,
    WorkAreaConfig,
    WorkAreaMode,
)
from .safety import SafetyConfig, SafetyGovernor, SafetyStatus

logger = logging.getLogger("vanchor.controller")

# Modes whose throttle Cruise Control may take over (an underway speed hold makes
# no sense for manual or station-keeping).
_CRUISING_MODES = frozenset(
    {
        ControlModeName.HEADING_HOLD,
        ControlModeName.WAYPOINT,
        ControlModeName.FOLLOW_APB,
        ControlModeName.CONTOUR_FOLLOW,
        ControlModeName.ORBIT,
        ControlModeName.TROLLING,
    }
)

# Boat-relative jog directions -> heading offset (degrees).
_JOG_OFFSETS = {"forward": 0.0, "back": 180.0, "left": -90.0, "right": 90.0}


# Below this |thrust| the trolling motor has no meaningful steering authority
# (a prop that isn't turning can't vector the boat), so we stop moving the
# steering actuator to avoid jittering / straining the servo for no effect.
STEER_EPS = 0.03


class Helm:
    """Turns a heading intent into a steering command via one shared PID.

    This is the "autopilot" inner loop: every guided mode benefits from a single
    well-tuned heading controller instead of re-implementing steering.
    """

    def __init__(self, pid: PID | None = None, steer_tau: float = 0.6,
                 autopilot_steer_scale: float = 1.0, steer_sign: float = 1.0,
                 thrust_yaw_ff: float = 0.0) -> None:
        self.pid = pid or PID(kp=0.035, ki=0.0, kd=0.012, output_min=-1.0, output_max=1.0)
        # First-order low-pass time constant (s) on the steering command. The
        # raw PID output chases ~1deg compass noise; without smoothing the motor
        # would slew back and forth constantly. ~0.6 s removes that jitter with
        # little control lag. Set to 0 to disable.
        self.steer_tau = steer_tau
        # The head can mechanically swing far (manual uses the full range), but
        # the autopilot only needs a fraction of it for course control. This
        # scales the guided (autopilot) steering so its *physical* deflection
        # stays at the tuned authority even when max_steer_angle_deg is large
        # (= autopilot_steer_deg / max_steer_angle_deg). Manual is unscaled.
        self.autopilot_steer_scale = autopilot_steer_scale
        # +1 for a bow-mounted thruster, -1 for stern. A stern thruster yaws the
        # boat the OPPOSITE way for the same steering deflection (the lever arm
        # sign flips), so without this the autopilot would steer backwards and
        # never converge. Derived from the boat's thruster mount and updated when
        # the boat profile changes; calibration can also set it from a measured
        # turn. Applied to BOTH manual and guided so a given command always turns
        # the boat the same way regardless of mount.
        self.steer_sign = 1.0 if steer_sign >= 0 else -1.0
        # Thrust-yaw feed-forward: a constant steering bias (in command units,
        # i.e. a fraction of the full mechanical swing) that pre-cancels the yaw
        # a laterally-offset thruster induces under straight thrust. Geometry sets
        # the magnitude; it is applied INSIDE ``steer_sign`` (so a stern mount,
        # whose deflection yaws the boat the opposite way, gets the opposite
        # physical deflection automatically) and only while making way. It is the
        # same sign in forward and reverse because both the bias and the steering
        # term scale with thrust, so the cancelling geometry is thrust-independent.
        self.thrust_yaw_ff = thrust_yaw_ff
        self._raw_hold = 0.0  # last raw PID steering (held when thrust ~0)
        self._filtered = 0.0  # low-passed output actually commanded

    def reset(self) -> None:
        self.pid.reset()
        self._raw_hold = 0.0
        self._filtered = 0.0

    def compute(self, setpoint: Setpoint, state: NavigationState, dt: float) -> MotorCommand:
        if isinstance(setpoint, ManualSetpoint):
            # Add the thrust-yaw feed-forward so a hands-off (steering=0) helm
            # tracks straight even with an off-centre motor; only while making way.
            ff = self.thrust_yaw_ff if abs(setpoint.thrust) >= STEER_EPS else 0.0
            return MotorCommand(
                thrust=setpoint.thrust,
                steering=(setpoint.steering + ff) * self.steer_sign,
            ).clamped()

        assert isinstance(setpoint, GuidedSetpoint)
        state.target_heading = setpoint.target_heading

        if abs(setpoint.thrust) < STEER_EPS:
            # No thrust => no steering authority: hold the raw target (the
            # low-pass below then just keeps the actuator where it is).
            raw = self._raw_hold
        else:
            # Positive error => target is to starboard => steer right (positive).
            error = angle_difference(state.heading_deg, setpoint.target_heading)
            raw = self.pid.update_error(error, dt)
            # A single steerable thruster reverses its steering authority when
            # the prop runs in reverse, so flip the command to stay stable.
            if setpoint.thrust < 0:
                raw = -raw
        self._raw_hold = raw

        # Low-pass the command so the steering head isn't driven by noise.
        if self.steer_tau > 0.0:
            alpha = dt / (self.steer_tau + dt)
            self._filtered += (raw - self._filtered) * alpha
        else:
            self._filtered = raw
        # Thrust-yaw feed-forward: a constant deflection that pre-cancels the
        # lateral-offset yaw bias, so the loop doesn't have to fight it (which a
        # PD helm could only do at a steady-state heading error). Applied only
        # while making way and inside ``steer_sign`` like the rest of the command.
        ff = self.thrust_yaw_ff if abs(setpoint.thrust) >= STEER_EPS else 0.0
        steering = (self._filtered * self.autopilot_steer_scale + ff) * self.steer_sign
        return MotorCommand(thrust=setpoint.thrust, steering=steering).clamped()


class Controller:
    def __init__(
        self,
        state: NavigationState,
        motor: MotorController,
        bus: EventBus | None = None,
        *,
        tick_hz: float = 5.0,
        helm: Helm | None = None,
        anchor_config: AnchorConfig | None = None,
        waypoint_config: WaypointConfig | None = None,
        follow_apb_config: FollowApbConfig | None = None,
        drift_config: DriftConfig | None = None,
        contour_config: ContourConfig | None = None,
        orbit_config: OrbitConfig | None = None,
        trolling_config: TrollingConfig | None = None,
        safety_config: SafetyConfig | None = None,
        estimator_config: EstimatorConfig | None = None,
        cruise_pid: PID | None = None,
        jog_increment_m: float = 1.5,
        track_min_distance_m: float = 5.0,
        mono_fn=time.monotonic,
    ) -> None:
        self.state = state
        self.motor = motor
        self.bus = bus
        self.tick_hz = tick_hz
        # MONOTONIC clock seam for the sensor-staleness ages fed to the governor.
        # Injectable so it can be driven deterministically (matches Runtime's
        # mono_fn, which is what the navigator stamps receive-times with).
        self._mono_fn = mono_fn
        self.helm = helm or Helm()

        # Persistent wind/current (drift) estimator: ONE instance, fed every
        # control tick in EVERY mode, so the environmental drift estimate is
        # always warm. It NEVER resets on a mode change -- so Spot-Lock, waypoint
        # crab feed-forward and drift mode all engage already knowing the set,
        # instead of relearning it. Its estimate is published onto ``state`` for
        # any mode (and the HUD) to read.
        if estimator_config is None:
            # Keep the estimator's thrust-decoupling boat speed in step with the
            # boat spec app.py configured on the anchor config, so it doesn't need
            # its own tuning path.
            estimator_config = EstimatorConfig()
            if anchor_config is not None:
                estimator_config.boat_max_speed_mps = anchor_config.boat_max_speed_mps
        self.estimator = WindCurrentEstimator(estimator_config)

        # Cruise Control: an optional SOG (speed-over-ground) PID that takes over
        # the throttle of guided "underway" modes when a target speed is set.
        self.cruise_pid = cruise_pid or PID(
            kp=0.64, ki=0.25, kd=0.0, output_min=0.0, output_max=1.0
        )
        self.cruise_knots: float | None = None
        # Throttle % override (#49): when set (0..1), guided modes use this as
        # their thrust magnitude instead of their built-in default. ``None`` =
        # no override.
        self.throttle_override: float | None = None
        # Pause/Resume (#50): a snapshot of the active guided mode + its
        # parameters captured on pause, restored on resume. ``None`` = nothing
        # suspended.
        self.suspended: dict | None = None
        self.jog_increment_m = jog_increment_m
        self.track = TrackRecorder(track_min_distance_m)

        self.manual = ManualMode()
        # Share one WaypointConfig between waypoint + work-area travel so boat-spec
        # tuning (app._apply_boat_specs) applies to both.
        wp_cfg = waypoint_config or WaypointConfig()
        self.modes: dict[ControlModeName, ControlMode] = {
            ControlModeName.MANUAL: self.manual,
            ControlModeName.ANCHOR_HOLD: AnchorHoldMode(anchor_config),
            ControlModeName.HEADING_HOLD: HeadingHoldMode(),
            ControlModeName.WAYPOINT: WaypointMode(wp_cfg),
            ControlModeName.WORK_AREA: WorkAreaMode(
                WorkAreaConfig(), waypoint_config=wp_cfg, anchor_config=anchor_config
            ),
            ControlModeName.FOLLOW_APB: FollowApbMode(follow_apb_config),
            ControlModeName.DRIFT: DriftMode(drift_config),
            ControlModeName.CONTOUR_FOLLOW: ContourFollowMode(contour_config),
            ControlModeName.ORBIT: OrbitMode(orbit_config),
            ControlModeName.TROLLING: TrollingMode(trolling_config),
        }
        # Learned spot-lock (optional): registered only if the shipped tiny-NN
        # model loads, so a missing/invalid model never breaks startup -- the
        # mode simply isn't offered.
        try:
            from .anchor_ml import AnchorMLMode

            self.modes[ControlModeName.ANCHOR_ML] = AnchorMLMode()
        except Exception as exc:  # noqa: BLE001 - any load error -> mode absent
            logger.warning("anchor_ml mode unavailable: %s", exc)
        self.safety = SafetyGovernor(safety_config)
        self.safety_status = SafetyStatus()
        self._last_fix_seq = state.fix_seq
        self._running = False
        # Consecutive failed control ticks (reset on any clean tick). Drives a
        # small backoff so a persistently-throwing tick doesn't spin the CPU.
        self._consecutive_faults = 0

        if bus is not None:
            bus.subscribe("command", self._on_command)

    # ------------------------------------------------------------------ #
    # Core control logic (synchronous, deterministic)
    # ------------------------------------------------------------------ #
    def control_tick(self, dt: float) -> MotorCommand:
        # Update the persistent drift estimate FIRST, so the active mode sees a
        # fresh ``state.est_drift_*`` this tick. It decouples our own propulsion
        # using the PREVIOUS tick's applied command (state.motor_command), exactly
        # as the old mode-local estimator did.
        self.estimator.update(self.state, dt)
        mode = self.modes[self.state.mode]
        setpoint = mode.update(self.state, dt)
        setpoint = self._apply_cruise(setpoint, dt)
        setpoint = self._apply_throttle_override(setpoint)
        command = self.helm.compute(setpoint, self.state, dt)

        # A fresh fix is one whose sequence number advanced since last tick.
        fix_is_fresh = self.state.fix_seq != self._last_fix_seq
        self._last_fix_seq = self.state.fix_seq

        # Sensor-staleness ages (seconds since each input last arrived). ``None``
        # when never stamped -> the governor treats those as fresh (so a harness
        # that never advances the clock can't be false-tripped).
        heading_age_s, depth_age_s = self._sensor_ages()

        command, self.safety_status = self.safety.govern(
            command,
            self.state,
            dt,
            fix_is_fresh,
            heading_age_s=heading_age_s,
            depth_age_s=depth_age_s,
        )
        self.state.motor_command = command
        self.motor.apply(command)

        # Breadcrumb the boat's path if a track recording is in progress.
        self.track.maybe_record(self.state.position)

        # Fire the route's on-arrival action once, after it completes.
        if (
            self.state.mode == ControlModeName.WAYPOINT
            and self.state.route_complete
            and self.state.route_on_arrival in ("anchor", "stop")
        ):
            action = self.state.route_on_arrival
            self.state.route_on_arrival = "none"  # one-shot
            logger.info("route complete -> on-arrival action: %s", action)
            if action == "anchor":
                self.handle_command({"type": "anchor_hold"})
            else:
                self.handle_command({"type": "stop"})
        return command

    def _sensor_ages(self) -> tuple[float | None, float | None]:
        """(heading_age_s, depth_age_s) since each input was last ingested, or
        ``None`` when it has never been stamped. Uses the injected monotonic
        clock -- the same seam the navigator stamps receive-times with."""
        now = self._mono_fn()
        h = self.state.heading_received_mono
        d = self.state.depth_received_mono
        return (
            (now - h) if h is not None else None,
            (now - d) if d is not None else None,
        )

    def _apply_cruise(self, setpoint: Setpoint, dt: float) -> Setpoint:
        """When Cruise Control is on, replace a guided mode's fixed throttle with
        the output of the SOG PID so the boat holds a target speed over ground."""
        if (
            self.cruise_knots is None
            or not isinstance(setpoint, GuidedSetpoint)
            or self.state.mode not in _CRUISING_MODES
        ):
            return setpoint
        thrust = self.cruise_pid.update(self.state.sog_knots, dt)
        # The cruise PID's output is unsigned (output_min=0.0): it only knows a
        # *speed* target, not a direction. Preserve the mode's intended thrust
        # SIGN so a reverse manoeuvre (e.g. WaypointMode backing toward a mark
        # that's close behind) still drives astern instead of being flipped to a
        # forward push that drives the boat away from the mark. A zero setpoint
        # stays forward-neutral (copysign of 0.0 is +).
        if setpoint.thrust < 0.0:
            thrust = math.copysign(thrust, setpoint.thrust)
        return GuidedSetpoint(target_heading=setpoint.target_heading, thrust=thrust)

    def _apply_throttle_override(self, setpoint: Setpoint, dt: float = 0.0) -> Setpoint:
        """Replace a guided mode's thrust *magnitude* with the % override.

        The mode keeps full control of *direction* (sign of thrust -- e.g. an
        anchor recovery backing up, or drift braking in reverse) and of whether
        it wants thrust at all (a zero stays zero); only the magnitude of a
        non-zero demand is scaled to the user's engine power. Cruise (a KNOTS
        speed hold) is left untouched -- it has already produced its setpoint and
        owns the throttle for cruising modes when active.
        """
        if (
            self.throttle_override is None
            or not isinstance(setpoint, GuidedSetpoint)
            or setpoint.thrust == 0.0
        ):
            return setpoint
        # Don't fight an active cruise (knots) hold on a cruising mode.
        if self.cruise_knots is not None and self.state.mode in _CRUISING_MODES:
            return setpoint
        magnitude = self.throttle_override
        thrust = math.copysign(magnitude, setpoint.thrust)
        return GuidedSetpoint(target_heading=setpoint.target_heading, thrust=thrust)

    def set_mode(self, mode: ControlModeName) -> None:
        changed = mode != self.state.mode
        if changed:
            logger.info("mode: %s -> %s", self.state.mode.value, mode.value)
        self.state.mode = mode
        # Only tear down the inner loops on a REAL mode change. Re-issuing the
        # current mode (e.g. a remote-helm button re-sending {"type":"manual"}
        # every press) must NOT reset the helm/governor: a governor reset zeroes
        # the slew anchors, so a re-sent command would ramp the prop from 0 again
        # (surge) and bypass the reverse interlock. On a genuine change we DO
        # reset, but seed the governor's slew anchors from the last applied motor
        # command so thrust/steering ramp from where the boat actually is rather
        # than snapping through zero.
        if changed:
            self.helm.reset()
            last = self.state.motor_command
            self.safety.reset(thrust=last.thrust, steering=last.steering)
        self.modes[mode].activate(self.state)

    # ------------------------------------------------------------------ #
    # Command handling
    # ------------------------------------------------------------------ #
    def handle_command(self, command: dict) -> None:
        """Apply a command dict. Shape: ``{"type": ..., ...}``."""
        ctype = command.get("type")
        try:
            if ctype == "manual":
                self.manual.set(
                    float(command.get("thrust", 0.0)), float(command.get("steering", 0.0))
                )
                self.set_mode(ControlModeName.MANUAL)
            elif ctype == "anchor_hold":
                anchor = command.get("anchor")
                if anchor:
                    self.state.anchor = GeoPoint(float(anchor["lat"]), float(anchor["lon"]))
                elif self.state.position is not None:
                    self.state.anchor = self.state.position
                if "radius_m" in command:
                    self.state.anchor_radius_m = float(command["radius_m"])
                # Record the heading at the moment of dropping (for display); the boat
                # holds this passively once on station (no active heading slew).
                self.state.anchor_heading = self.state.heading_deg
                self.set_mode(ControlModeName.ANCHOR_HOLD)
            elif ctype == "anchor_ml":
                # Learned spot-lock: same mark-setting as anchor_hold, but driven by
                # the tiny NN. Falls back to the PID mode if the model isn't loaded.
                anchor = command.get("anchor")
                if anchor:
                    self.state.anchor = GeoPoint(float(anchor["lat"]), float(anchor["lon"]))
                elif self.state.position is not None:
                    self.state.anchor = self.state.position
                if "radius_m" in command:
                    self.state.anchor_radius_m = float(command["radius_m"])
                self.state.anchor_heading = self.state.heading_deg
                self.set_mode(
                    ControlModeName.ANCHOR_ML
                    if ControlModeName.ANCHOR_ML in self.modes
                    else ControlModeName.ANCHOR_HOLD
                )
            elif ctype == "heading_hold":
                heading = command.get("heading")
                self.state.target_heading = (
                    float(heading) if heading is not None else self.state.heading_deg
                )
                if "throttle" in command:
                    self.modes[ControlModeName.HEADING_HOLD].throttle = float(
                        command["throttle"]
                    )
                self.set_mode(ControlModeName.HEADING_HOLD)
            elif ctype == "goto":
                wps = command.get("waypoints", [])
                self.state.waypoints = [
                    Waypoint(
                        name=str(w.get("name", f"WP{i}")),
                        point=GeoPoint(float(w["lat"]), float(w["lon"])),
                    )
                    for i, w in enumerate(wps)
                ]
                if "throttle" in command:
                    self.modes[ControlModeName.WAYPOINT].config.throttle = float(
                        command["throttle"]
                    )
                # "active" present => a LIVE EDIT of the running route (the user
                # dragged/inserted/deleted/reordered a committed waypoint and the UI
                # re-sent it). Resume from the given index (clamped) instead of
                # restarting at 0, and leave the route's mode/flags/progress intact so
                # an edit doesn't make the boat start over. Absent => a fresh start.
                resume = command.get("active")
                if resume is None:
                    self.state.active_waypoint = 0
                    # What to do when the route finishes: "anchor", "stop", or "none".
                    self.state.route_on_arrival = str(command.get("on_arrival", "none"))
                    # Closed-loop route (e.g. "around island"): circle continuously.
                    self.state.route_loop = bool(command.get("loop", False))
                    # Patrol: at each end, reverse and run the route back.
                    self.state.route_patrol = bool(command.get("patrol", False))
                    self.set_mode(ControlModeName.WAYPOINT)
                else:
                    n = len(self.state.waypoints)
                    self.state.active_waypoint = max(0, min(int(resume), n - 1)) if n else 0
                    if self.state.mode != ControlModeName.WAYPOINT:
                        self.set_mode(ControlModeName.WAYPOINT)
            elif ctype == "load_route":
                # Waypoints already parsed (from GPX) and placed on the state by the
                # runtime; just (re)start waypoint navigation.
                self.state.active_waypoint = 0
                self.state.route_loop = bool(command.get("loop", False))
                self.state.route_patrol = bool(command.get("patrol", False))
                if "throttle" in command:
                    self.modes[ControlModeName.WAYPOINT].config.throttle = float(
                        command["throttle"]
                    )
                self.set_mode(ControlModeName.WAYPOINT)
            elif ctype == "work_area":
                # Work Area: spots = waypoints (each with optional hold heading); visit
                # each, spot-lock + hold, advance on the "next spot" button and/or a
                # dwell timer. loop/patrol cycle the spots like a route.
                wps = command.get("waypoints", [])
                self.state.waypoints = [
                    Waypoint(
                        name=str(w.get("name", f"Spot {i + 1}")),
                        point=GeoPoint(float(w["lat"]), float(w["lon"])),
                        heading=(float(w["heading"]) if w.get("heading") is not None else None),
                    )
                    for i, w in enumerate(wps)
                ]
                self.state.active_waypoint = 0
                self.state.route_loop = bool(command.get("loop", False))
                self.state.route_patrol = bool(command.get("patrol", False))
                wa = self.modes[ControlModeName.WORK_AREA].config
                if "dwell_s" in command:
                    wa.dwell_s = max(0.0, float(command["dwell_s"]))
                if "advance" in command:
                    wa.advance = "timed" if str(command["advance"]) == "timed" else "manual"
                if "throttle" in command:
                    wa.throttle = float(command["throttle"])
                self.set_mode(ControlModeName.WORK_AREA)
            elif ctype == "next_spot":
                # The big on-screen "Go to next spot" button (Work Area mode).
                self.state.work_next_requested = True
            elif ctype == "follow_apb":
                if "throttle" in command:
                    self.modes[ControlModeName.FOLLOW_APB].config.throttle = float(
                        command["throttle"]
                    )
                self.set_mode(ControlModeName.FOLLOW_APB)
            elif ctype == "drift":
                heading = command.get("heading")
                self.state.target_heading = (
                    float(heading) if heading is not None else self.state.heading_deg
                )
                if "knots" in command:
                    self.state.drift_target_knots = float(command["knots"])
                self.set_mode(ControlModeName.DRIFT)
            elif ctype == "contour_follow":
                self.state.contour_target_depth_m = float(command.get("target_depth_m", 0.0))
                self.state.contour_side = str(command.get("side", "deep"))
                self._apply_speed_knots(command.get("speed_knots"))
                self.set_mode(ControlModeName.CONTOUR_FOLLOW)
            elif ctype == "orbit":
                self.state.orbit_center = GeoPoint(
                    float(command["center_lat"]), float(command["center_lon"])
                )
                self.state.orbit_radius_m = float(command.get("radius_m", 20.0))
                self.state.orbit_direction = str(command.get("direction", "cw"))
                self._apply_speed_knots(command.get("speed_knots"))
                self.set_mode(ControlModeName.ORBIT)
            elif ctype == "trolling":
                base = command.get("base_heading")
                self.state.trolling_base_heading = (
                    float(base) if base is not None else self.state.heading_deg
                )
                self.state.trolling_amplitude_deg = float(command.get("amplitude_deg", 20.0))
                self.state.trolling_period_s = float(command.get("period_s", 20.0))
                self._apply_speed_knots(command.get("speed_knots"))
                self.set_mode(ControlModeName.TROLLING)
            elif ctype == "jog":
                self._jog(command)
            elif ctype == "cruise":
                self._set_cruise(command.get("knots"))
            elif ctype == "set_throttle":
                self._set_throttle(command.get("percent"))
            elif ctype == "pause_nav":
                self._pause_nav()
            elif ctype == "resume_nav":
                self._resume_nav()
            elif ctype in ("record", "replay", "backtrack"):
                self._track_command(ctype, command)
            elif ctype == "set_nogo_zones":
                self.safety.set_nogo_zones(
                    [
                        [(float(p[0]), float(p[1])) for p in ring]
                        for ring in command.get("zones", [])
                    ]
                )
                logger.info("no-go zones set: %d", self.safety.nogo_zone_count)
            elif ctype == "set_min_depth":
                self.safety.config.min_depth_m = float(command.get("min_depth_m", 0.0))
                logger.info("min depth set: %.1f m", self.safety.config.min_depth_m)
            elif ctype == "set_fix_failsafe":
                self.safety.config.fix_failsafe_enabled = bool(command.get("enabled", False))
                logger.info("loss-of-fix failsafe %s",
                            "ON" if self.safety.config.fix_failsafe_enabled else "OFF")
            elif ctype == "set_launch":
                self._set_launch()
            elif ctype == "mob":
                self._mob()
            elif ctype == "mob_clear":
                self._mob_clear()
            elif ctype == "stop":
                self.suspended = None  # a hard stop clears any paused nav
                self.manual.set(0.0, 0.0)
                self.set_mode(ControlModeName.MANUAL)
            else:
                logger.warning("unknown command: %r", command)
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("handle_command %r: malformed payload: %s", ctype, exc)

    # -- Tier-1 features ------------------------------------------------- #
    def _jog(self, command: dict) -> None:
        """Spot-Lock Jog: nudge the anchor boat-relative (fwd/back/left/right)."""
        if self.state.anchor is None:
            logger.warning("jog ignored: no anchor set")
            return
        direction = str(command.get("direction", "forward"))
        if direction not in _JOG_OFFSETS:
            logger.warning("jog: unknown direction %r", direction)
            return
        distance = float(command.get("distance_m", self.jog_increment_m))
        bearing = normalize_deg(self.state.heading_deg + _JOG_OFFSETS[direction])
        self.state.anchor = destination_point(self.state.anchor, distance, bearing)
        logger.info("jog %s %.1f m -> anchor moved", direction, distance)

    def _apply_speed_knots(self, knots: object) -> None:
        """For the guided pattern modes (contour/orbit/trolling): if a
        ``speed_knots`` is supplied, hold it via the existing Cruise Control
        (SOG) loop so the boat keeps that speed over ground; if it is ``None``
        the mode falls back to its own default thrust (cruise left as-is)."""
        if knots is not None:
            self._set_cruise(knots)

    def _set_cruise(self, knots: object) -> None:
        """Enable/disable Cruise Control. ``knots`` <= 0 or None turns it off."""
        if knots is None or float(knots) <= 0.0:
            self.cruise_knots = None
            logger.info("cruise off")
            return
        self.cruise_knots = float(knots)
        self.cruise_pid.setpoint = self.cruise_knots
        self.cruise_pid.reset()
        logger.info("cruise on: %.1f kn", self.cruise_knots)

    def _set_throttle(self, percent: object) -> None:
        """Set/clear the guided-mode throttle % override (#49). ``None`` or 0
        clears it; otherwise a 0..100 percent of engine power."""
        if percent is None or float(percent) <= 0.0:
            self.throttle_override = None
            logger.info("throttle override cleared")
            return
        pct = max(0.0, min(100.0, float(percent)))
        self.throttle_override = pct / 100.0
        logger.info("throttle override: %.0f%%", pct)

    # -- Pause / Resume navigation (#50) --------------------------------- #
    def _pause_nav(self) -> None:
        """Remember the active guided mode + its parameters, then hold position
        (anchor-hold at the current spot)."""
        if self.state.mode == ControlModeName.MANUAL:
            logger.info("pause_nav ignored: not navigating")
            return
        self.suspended = {
            "mode": self.state.mode,
            "waypoints": list(self.state.waypoints),
            "active_waypoint": self.state.active_waypoint,
            "route_on_arrival": self.state.route_on_arrival,
            "route_loop": self.state.route_loop,
            "route_patrol": self.state.route_patrol,
            "target_heading": self.state.target_heading,
            "anchor": self.state.anchor,
            "anchor_radius_m": self.state.anchor_radius_m,
            "anchor_heading": self.state.anchor_heading,
            "drift_target_knots": self.state.drift_target_knots,
            "cruise_knots": self.cruise_knots,
            "throttle_override": self.throttle_override,
        }
        logger.info("nav paused (was %s); holding position", self.state.mode.value)
        # Hold position: anchor-hold at the current spot.
        if self.state.position is not None:
            self.state.anchor = self.state.position
        self.state.anchor_heading = self.state.heading_deg
        self.set_mode(ControlModeName.ANCHOR_HOLD)

    def _resume_nav(self) -> None:
        """Restore the previously suspended mode + all its parameters."""
        snap = self.suspended
        if snap is None:
            logger.info("resume_nav ignored: nothing suspended")
            return
        self.suspended = None
        self.state.waypoints = list(snap["waypoints"])
        self.state.active_waypoint = snap["active_waypoint"]
        self.state.route_on_arrival = snap["route_on_arrival"]
        self.state.route_loop = snap.get("route_loop", False)
        self.state.route_patrol = snap.get("route_patrol", False)
        self.state.target_heading = snap["target_heading"]
        self.state.anchor = snap["anchor"]
        self.state.anchor_radius_m = snap["anchor_radius_m"]
        self.state.anchor_heading = snap["anchor_heading"]
        self.state.drift_target_knots = snap["drift_target_knots"]
        self.cruise_knots = snap["cruise_knots"]
        self.throttle_override = snap["throttle_override"]
        logger.info("nav resumed -> %s", snap["mode"].value)
        self.set_mode(snap["mode"])

    def _track_command(self, ctype: str, command: dict) -> None:
        if ctype == "record":
            action = str(command.get("action", "start"))
            if action == "start":
                self.track.start(self.state.position)
            elif action == "stop":
                self.track.stop()
            elif action == "clear":
                self.track.clear()
            return
        # replay (forward) / backtrack (reverse) -> feed WaypointMode.
        waypoints = self.track.as_waypoints(reverse=(ctype == "backtrack"))
        if not waypoints:
            logger.warning("%s ignored: no recorded track", ctype)
            return
        self.state.waypoints = waypoints
        self.state.active_waypoint = 0
        if "throttle" in command:
            self.modes[ControlModeName.WAYPOINT].config.throttle = float(
                command["throttle"]
            )
        self.set_mode(ControlModeName.WAYPOINT)
        logger.info("%s: navigating %d recorded points", ctype, len(waypoints))

    # -- Return-to-Launch (#61) ------------------------------------------ #
    def _set_launch(self) -> None:
        """Record the launch/home point at the current position."""
        if self.state.position is None or self.state.position.is_null():
            logger.warning("set_launch ignored: no position fix")
            return
        self.state.launch = self.state.position
        logger.info("launch point set to current position")

    def maybe_record_launch(self) -> None:
        """Auto-record the launch point on the first good fix (idempotent)."""
        if self.state.launch is None:
            pos = self.state.position
            if pos is not None and not pos.is_null():
                self.state.launch = pos
                logger.info("launch point auto-recorded (first fix)")

    # -- Man-overboard (#63) --------------------------------------------- #
    def _mob(self) -> None:
        """Mark the current position as MOB and navigate straight back to it."""
        pos = self.state.position
        if pos is None or pos.is_null():
            logger.warning("mob ignored: no position fix")
            return
        self.state.mob = pos
        self.state.mob_active = True
        # Return to the mark as a single-waypoint route, stopping on arrival so
        # the boat holds near the casualty.
        self.state.waypoints = [Waypoint(name="MOB", point=pos)]
        self.state.active_waypoint = 0
        self.state.route_on_arrival = "stop"
        self.set_mode(ControlModeName.WAYPOINT)
        logger.info("MOB: returning to %.5f, %.5f", pos.lat, pos.lon)

    def _mob_clear(self) -> None:
        """Cancel a man-overboard return."""
        self.state.mob_active = False
        logger.info("MOB cleared")

    async def _on_command(self, command: dict) -> None:
        self.handle_command(command)

    # ------------------------------------------------------------------ #
    # Async runtime
    # ------------------------------------------------------------------ #
    async def _tick_once(self, dt: float) -> None:
        """Run a single supervised control iteration.

        The body is wrapped so that ANY exception (in a mode, the helm, the
        governor, or the motor) cannot silently kill the loop task -- which would
        leave the motor stuck on its last command (in sim, the boat runs away).
        On a fault we log with a traceback, best-effort zero the motor, record a
        fault indicator on the state, and return so the caller can keep looping.
        A clean tick clears the fault flag.
        """
        try:
            command = self.control_tick(dt)
            await self.motor.flush()
            if self.bus is not None:
                await self.bus.publish(events.MOTOR_COMMAND, command)
            self.state.controller_fault = None
            self._consecutive_faults = 0
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            self._consecutive_faults += 1
            logger.exception(
                "control tick failed (%d consecutive); zeroing motor",
                self._consecutive_faults,
            )
            self.state.controller_fault = f"{type(exc).__name__}: {exc}"
            # Best-effort STOP: never let a fault leave the prop running. Guard
            # this in its own try so a motor that is itself faulting can't escape.
            try:
                neutral = MotorCommand(thrust=0.0, steering=0.0)
                self.motor.apply(neutral)
                await self.motor.flush()
                self.state.motor_command = neutral
            except Exception:  # noqa: BLE001
                logger.exception("failed to zero motor after control fault")

    async def run(self) -> None:
        self._running = True
        period = 1.0 / self.tick_hz
        logger.info("controller loop started at %.1f Hz", self.tick_hz)
        last = time.monotonic()
        while self._running:
            now = time.monotonic()
            # Measure the REAL elapsed time and clamp it to a sane band so a
            # scheduling hiccup (a long GC pause, a debugger breakpoint) can't
            # feed the PIDs a pathological dt. Below 0.5x period there's nothing
            # to gain; above 3x we cap the integral/derivative kick.
            dt = min(max(now - last, 0.5 * period), 3.0 * period)
            last = now
            self.state.controller_last_tick_monotonic = now

            await self._tick_once(dt)

            # Sleep out the remainder of the period after subtracting the work we
            # just did, so the loop holds ~tick_hz instead of drifting slower.
            elapsed = time.monotonic() - now
            sleep_s = max(0.0, period - elapsed)
            # After repeated consecutive failures, add a small capped backoff so
            # a hard-faulting tick doesn't hot-spin -- but NEVER exit the loop.
            if self._consecutive_faults >= 3:
                sleep_s = max(sleep_s, min(self._consecutive_faults * period, 2.0))
            await asyncio.sleep(sleep_s)

    def stop(self) -> None:
        self._running = False
