"""Control modes -- the high-level steering behaviours.

Each mode is a pure strategy: given the current :class:`NavigationState` and a
timestep, produce a :class:`Setpoint`. A mode never touches hardware; it only
expresses *intent* (either drive the motor directly, or hold a target heading).
The controller's helm turns a heading intent into actual steering, so all the
guided modes share one well-tuned heading loop.

This separation makes every behaviour independently unit-testable with no
hardware and no event loop.
"""

from __future__ import annotations

import abc
import math
from dataclasses import dataclass

from ..core.geo import (
    angle_difference,
    cross_track,
    haversine_m,
    initial_bearing,
    knots_to_mps,
    normalize_deg,
)
from ..core.models import (
    ControlModeName,
    GeoPoint,
    GuidedSetpoint,
    ManualSetpoint,
    Setpoint,
)
from ..core.pid import PID
from ..core.state import NavigationState


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class ControlMode(abc.ABC):
    name: ControlModeName

    def activate(self, state: NavigationState) -> None:
        """Called when this mode becomes active. Reset internal loops here."""

    @abc.abstractmethod
    def update(self, state: NavigationState, dt: float) -> Setpoint: ...


class ManualMode(ControlMode):
    name = ControlModeName.MANUAL

    def __init__(self) -> None:
        self.thrust = 0.0
        self.steering = 0.0

    def set(self, thrust: float, steering: float) -> None:
        self.thrust = thrust
        self.steering = steering

    def update(self, state: NavigationState, dt: float) -> Setpoint:
        return ManualSetpoint(thrust=self.thrust, steering=self.steering)


class HeadingHoldMode(ControlMode):
    """Hold the heading stored in ``state.target_heading`` while applying a
    user-set cruise throttle."""

    name = ControlModeName.HEADING_HOLD

    def __init__(self, throttle: float = 0.0) -> None:
        self.throttle = throttle

    def update(self, state: NavigationState, dt: float) -> Setpoint:
        return GuidedSetpoint(target_heading=state.target_heading, thrust=self.throttle)


@dataclass
class DriftConfig:
    kp: float = 0.5
    ki: float = 0.25


class DriftMode(ControlMode):
    """Controlled drift: hold a heading while a bidirectional SOG PID holds a
    *low* target speed-over-ground (``state.drift_target_knots``).

    Wind/current carry the boat along the chosen bearing; the motor only trims
    speed -- adding thrust if the drift is too slow, or reversing to brake if it
    is too fast. Mirrors the Drift mode of high-end GPS trolling motors.
    """

    name = ControlModeName.DRIFT

    def __init__(self, config: DriftConfig | None = None) -> None:
        self.config = config or DriftConfig()
        self.pid = PID(
            kp=self.config.kp, ki=self.config.ki, kd=0.0, output_min=-1.0, output_max=1.0
        )

    def activate(self, state: NavigationState) -> None:
        self.pid.reset()

    def update(self, state: NavigationState, dt: float) -> Setpoint:
        self.pid.setpoint = state.drift_target_knots
        # Regulate the SIGNED speed ALONG the held drift heading, not the
        # unsigned speed-over-ground. With the held heading transverse to the
        # actual drift, |SOG| never reaches the target from below, so the PID
        # would ratchet its integral against a magnitude it can't null. Projecting
        # the GPS velocity onto the held heading (negative when the boat is
        # actually moving the OTHER way) gives the controller a signed error it
        # can drive to zero -- adding thrust when too slow, braking in reverse
        # when too fast.
        fix = state.fix
        if fix is not None:
            along_knots = fix.sog_knots * math.cos(
                math.radians(angle_difference(fix.cog_deg, state.target_heading))
            )
        else:
            along_knots = state.sog_knots
        thrust = self.pid.update(along_knots, dt)
        return GuidedSetpoint(target_heading=state.target_heading, thrust=thrust)


@dataclass
class AnchorConfig:
    kp: float = 0.12  # thrust per metre of position error
    kd: float = 0.6  # *braking* thrust per (m/s) of closing speed toward the anchor
    max_thrust: float = 1.0
    # Idle dead-band: once within this distance of the mark, idle the motor.
    # With ~1.5 m GPS noise this stops the boat hunting/oscillating around the
    # mark (it idles and drifts within the band instead -- calmer, easier on the
    # motor) -- and because the motor isn't running, the boat doesn't yaw, so its
    # heading is held passively (it does not spin to chase the mark). Drift is
    # still rejected: once pushed past the band the PD drives it back. 0.8 m
    # roughly halves the settling time and oscillation vs. no dead-band (see
    # docs/analysis.md) while staying 100% within a tight radius.
    idle_deadband_m: float = 0.8
    # Aggressive "recover" (re-point and drive back to the mark) only kicks in
    # past this distance, regardless of how small the anchor radius is. Without
    # it, a tight radius (1-2 m) sits below the GPS-noise + boat-length floor, so
    # the boat is *always* "outside" and stuck darting in recover mode -- the
    # classic small-radius overcorrection. With it, a small radius holds just as
    # calmly as a large one (gentle station-keeping), only entering recover when
    # genuinely shoved off station.
    recover_floor_m: float = 3.5
    # Optional low-pass (EMA) on the perceived position. 1.0 = off (filtering
    # adds lag a control loop fights, so it is off by default).
    pos_filter_alpha: float = 1.0

    # --- Predictive drift handling --------------------------------------- #
    # The drift is *estimated* (from true ground velocity minus our propulsion)
    # and exposed for the HUD/telemetry. Because the GPS now reports real
    # course/speed-over-ground, the kd (closing-speed) term already *anticipates*
    # drift -- it acts on velocity, so it counters the drift before the position
    # error builds, holding ~100% within radius with little maneuvering. The
    # explicit feed-forward below then double-counts and over-corrects, so it is
    # OFF by default; it remains available (e.g. for very low control rates).
    feedforward: bool = False
    feedforward_gain: float = 0.7  # fraction of the estimated drift to counter
    boat_max_speed_mps: float = 1.6  # to estimate our thrust's contribution to v
    # Time constant (s) of the drift-estimate low-pass. It is converted to a
    # per-tick EMA weight as ``alpha = dt / (drift_tau_s + dt)`` so the smoothing
    # is FRAME-RATE INDEPENDENT (a fixed per-tick weight would smooth twice as
    # hard at 10 Hz as at 5 Hz). ~10 s matches the original intent.
    drift_tau_s: float = 10.0
    drift_min_mps: float = 0.05  # below this, no significant drift to point into


class AnchorHoldMode(ControlMode):
    """Virtual anchor ("Spot-Lock"): hold position with reverse thrust + braking.

    A PD controller on the (ground) distance to the mark: ``kp`` pulls toward the
    anchor, ``kd`` brakes using the GPS closing speed so the boat doesn't
    overshoot and orbit. It uses **reverse thrust** -- braking an overshoot, and
    when the anchor ends up *behind* the boat it backs straight up toward it
    rather than looping all the way around. Within ``idle_deadband_m`` it idles;
    with no thrust the (thrust-to-steer) motor produces no yaw, so the heading is
    held passively and the servo isn't worked.

    Note: a single bow-mounted, thrust-to-steer motor is underactuated -- it
    cannot actively hold an arbitrary heading while sitting still (steering needs
    thrust, which moves it off station). So this holds *position* and lets the
    heading settle, exactly like a real GPS trolling motor.
    """

    name = ControlModeName.ANCHOR_HOLD

    def __init__(self, config: AnchorConfig | None = None) -> None:
        self.config = config or AnchorConfig()
        self._reverse = False  # hysteresis on the forward/reverse decision
        self._recovering = False  # hysteresis on the recover/station decision
        self._filt: GeoPoint | None = None  # EMA-filtered perceived position
        self._drift_e = 0.0  # estimated drift velocity (world east, m/s)
        self._drift_n = 0.0  # estimated drift velocity (world north, m/s)

    def activate(self, state: NavigationState) -> None:
        self._reverse = False
        self._recovering = False
        self._filt = None
        self._drift_e = 0.0
        self._drift_n = 0.0

    def _update_drift(self, state: NavigationState, state_dt: float) -> None:
        """Estimate the environmental drift velocity (world frame) by subtracting
        our own propulsion from the observed GPS velocity, low-passed."""
        fix = state.fix
        if fix is None:
            return
        v = knots_to_mps(fix.sog_knots)
        cog = math.radians(fix.cog_deg)
        v_e, v_n = v * math.sin(cog), v * math.cos(cog)
        # Our thrust roughly produces thrust*max_speed along the boat heading.
        h = math.radians(state.heading_deg)
        thr = state.motor_command.thrust
        int_e = thr * self.config.boat_max_speed_mps * math.sin(h)
        int_n = thr * self.config.boat_max_speed_mps * math.cos(h)
        # dt-scaled EMA weight so the smoothing time constant is fixed at
        # drift_tau_s regardless of the control rate.
        dt = max(1e-3, state_dt)
        a = dt / (self.config.drift_tau_s + dt)
        self._drift_e += a * ((v_e - int_e) - self._drift_e)
        self._drift_n += a * ((v_n - int_n) - self._drift_n)
        state.est_drift_mps = math.hypot(self._drift_e, self._drift_n)
        state.est_drift_dir = math.degrees(math.atan2(self._drift_e, self._drift_n)) % 360.0

    def _filtered_position(self, raw: GeoPoint) -> GeoPoint:
        a = self.config.pos_filter_alpha
        if self._filt is None or a >= 1.0:
            self._filt = raw
        else:
            self._filt = GeoPoint(
                self._filt.lat + a * (raw.lat - self._filt.lat),
                self._filt.lon + a * (raw.lon - self._filt.lon),
            )
        return self._filt

    def _closing_speed(self, state: NavigationState, toward_deg: float) -> float:
        """GPS-derived speed component toward ``toward_deg`` (+ = approaching)."""
        fix = state.fix
        if fix is None:
            return 0.0
        v = knots_to_mps(fix.sog_knots)
        return v * math.cos(math.radians(angle_difference(fix.cog_deg, toward_deg)))

    def update(self, state: NavigationState, dt: float) -> Setpoint:
        raw = state.position
        if raw is None or state.anchor is None:
            return ManualSetpoint(0.0, 0.0)

        pos = self._filtered_position(raw)
        distance = haversine_m(pos, state.anchor)
        bearing = initial_bearing(pos, state.anchor)
        state.distance_to_anchor_m = distance
        state.bearing_to_dest = bearing
        cfg = self.config
        radius = state.anchor_radius_m

        self._update_drift(state, dt)

        # Recover (actively re-point at the mark) only when pushed clearly out of
        # the radius; resume station-keeping once back well inside. The
        # hysteresis stops GPS noise from toggling the two. The effective trigger
        # never drops below recover_floor_m, so a tight radius doesn't put the
        # boat in permanent recovery (overcorrection) against GPS noise.
        recover_at = max(radius, cfg.recover_floor_m)
        if distance > recover_at:
            self._recovering = True
        elif distance < recover_at * 0.6:
            self._recovering = False

        if self._recovering:
            # Drive toward the anchor, forward if it's ahead else reverse (back
            # straight up) -- whichever needs less turning -- braked by closing
            # speed so we don't overshoot and orbit.
            closing = self._closing_speed(state, bearing)
            demand = _clamp(
                cfg.kp * distance - cfg.kd * closing, -cfg.max_thrust, cfg.max_thrust
            )
            angle = angle_difference(state.heading_deg, bearing)
            if abs(angle) > 110.0:
                self._reverse = True
            elif abs(angle) < 70.0:
                self._reverse = False
            if not self._reverse:
                return GuidedSetpoint(target_heading=bearing, thrust=demand)
            return GuidedSetpoint(
                target_heading=normalize_deg(bearing - 180.0), thrust=-demand
            )

        # --- Station-keeping inside the radius --------------------------- #
        drift_mag = math.hypot(self._drift_e, self._drift_n)
        if cfg.feedforward and drift_mag >= cfg.drift_min_mps:
            # Predictive: point the bow *into* the drift and hold a steady
            # counter-thrust (feed-forward), so the boat sits still against
            # wind/current instead of drifting out and darting back. The bow
            # axis is the direction opposing the drift.
            held = math.degrees(math.atan2(-self._drift_e, -self._drift_n)) % 360.0
            feed_forward = cfg.feedforward_gain * drift_mag / cfg.boat_max_speed_mps
        else:
            # No significant drift learned yet: hold the current heading.
            held = state.heading_deg
            feed_forward = 0.0

        # Position trim along the bow axis, with a dead-zone so GPS noise near
        # the mark doesn't cause fidgeting (the feed-forward does the holding).
        fwd_err = distance * math.cos(math.radians(angle_difference(held, bearing)))
        fwd_err = math.copysign(max(0.0, abs(fwd_err) - cfg.idle_deadband_m), fwd_err)
        fwd_speed = self._closing_speed(state, held)
        trim = cfg.kp * fwd_err - cfg.kd * fwd_speed
        thrust = _clamp(feed_forward + trim, -cfg.max_thrust, cfg.max_thrust)

        if abs(thrust) < 0.01:
            # Nothing to do: idle (no thrust => no yaw => heading held).
            return GuidedSetpoint(target_heading=state.heading_deg, thrust=0.0)
        return GuidedSetpoint(target_heading=held, thrust=thrust)


def maneuver_to_bearing(
    heading_deg: float,
    bearing_deg: float,
    distance_m: float,
    *,
    turn_rate_dps: float,
    fwd_speed_mps: float,
    reverse_efficiency: float,
    currently_reverse: bool = False,
    hysteresis: float = 0.85,
) -> tuple[float, float, bool]:
    """Pick **forward** (bow toward ``bearing``) or **reverse** (stern toward
    ``bearing``) to reach a point ``distance_m`` away on ``bearing_deg``, by
    whichever has the lower estimated *time to arrive*: ``turn_time + travel_time``.

    Reversing trades a smaller heading change for slower travel (a trolling-motor
    prop is weaker astern), so it wins when the target is **behind AND near** —
    sometimes that means "turn a little and reverse" rather than swinging the
    whole boat around; for a *far* target it is quicker to turn around and run
    forward. ``hysteresis`` (<1) makes switching require the alternative be
    clearly better, to stop chatter near the crossover.

    Returns ``(target_heading_deg, thrust_sign, reverse)`` where ``thrust_sign``
    is ``+1`` forward / ``-1`` reverse. (The helm flips steering authority under
    negative thrust, so a reverse setpoint steers correctly.)
    """
    turn = max(turn_rate_dps, 1.0)
    vf = max(fwd_speed_mps, 0.05)
    vr = max(fwd_speed_mps * reverse_efficiency, 0.05)
    a_fwd = abs(angle_difference(heading_deg, bearing_deg))
    a_rev = abs(angle_difference(heading_deg, normalize_deg(bearing_deg - 180.0)))
    cost_fwd = a_fwd / turn + distance_m / vf
    cost_rev = a_rev / turn + distance_m / vr
    if currently_reverse:
        reverse = cost_fwd >= cost_rev * hysteresis  # stay reverse unless fwd clearly better
    else:
        reverse = cost_rev < cost_fwd * hysteresis   # switch to reverse only if clearly better
    if reverse:
        return normalize_deg(bearing_deg - 180.0), -1.0, True
    return normalize_deg(bearing_deg), 1.0, False


@dataclass
class WaypointConfig:
    arrival_radius_m: float = 5.0
    throttle: float = 0.6
    # Degrees of heading correction per metre of cross-track error.
    xte_gain: float = 2.0
    max_xte_correction_deg: float = 60.0
    # Forward/reverse manoeuvring: reverse toward a mark instead of swinging ~180°
    # when that arrives sooner (close + behind). Populated from the active boat
    # config in app._apply_boat_specs. allow_reverse=False restores pure-forward.
    allow_reverse: bool = True
    reverse_efficiency: float = 0.6
    turn_rate_dps: float = 18.0
    boat_speed_mps: float = 1.6


class WaypointMode(ControlMode):
    """Steer through ``state.waypoints`` in order, correcting for cross-track
    error so the boat tracks each leg rather than just aiming at the mark."""

    name = ControlModeName.WAYPOINT

    def __init__(self, config: WaypointConfig | None = None) -> None:
        self.config = config or WaypointConfig()
        self._leg_start: GeoPoint | None = None
        self._reverse = False  # hysteresis on the forward/reverse decision
        self._step = 1         # traversal direction: +1 forward, -1 back (patrol)

    def activate(self, state: NavigationState) -> None:
        self._leg_start = state.position
        self._reverse = False
        self._step = 1
        state.route_complete = False

    def _wrap_or_bounce(self, state: NavigationState, pos: GeoPoint) -> bool:
        """``active_waypoint`` has run off an END of the route. ``route_loop``
        wraps to the start and keeps circling; ``route_patrol`` reverses direction
        and runs back the other way; a plain route completes. Returns True when
        the route is now COMPLETE."""
        n = len(state.waypoints)
        if state.route_loop:
            state.active_waypoint = 0
            self._step = 1
            self._leg_start = pos
            return False
        if state.route_patrol and n >= 2:
            self._step = -self._step
            state.active_waypoint += 2 * self._step  # off-the-end -> adjacent in-range mark
            self._leg_start = pos
            return False
        state.route_complete = True
        return True

    def update(self, state: NavigationState, dt: float) -> Setpoint:
        pos = state.position
        if pos is None or not state.waypoints:
            return ManualSetpoint(0.0, 0.0)

        # Off either END of the route (>= len going forward, or < 0 going back in
        # a patrol)? loop wraps, patrol reverses, a plain route completes.
        if not 0 <= state.active_waypoint < len(state.waypoints):
            if self._wrap_or_bounce(state, pos):
                return GuidedSetpoint(target_heading=state.heading_deg, thrust=0.0)

        if self._leg_start is None:
            self._leg_start = pos

        target = state.waypoints[state.active_waypoint].point
        distance = haversine_m(pos, target)
        state.distance_to_waypoint_m = distance

        # Arrival: within the radius, OR passed the leg's perpendicular (the
        # boat sailed past the waypoint abeam without entering the circle).
        arrived = distance <= self.config.arrival_radius_m
        if not arrived:
            leg_len = haversine_m(self._leg_start, target)
            if leg_len > 0.0:
                brg_leg = initial_bearing(self._leg_start, target)
                brg_pos = initial_bearing(self._leg_start, pos)
                d_from_start = haversine_m(self._leg_start, pos)
                along = d_from_start * math.cos(
                    math.radians(angle_difference(brg_leg, brg_pos))
                )
                xte_m = abs(cross_track(self._leg_start, target, pos).distance_m)
                if along >= leg_len and xte_m <= 3.0 * self.config.arrival_radius_m:
                    arrived = True

        if arrived:
            # Advance to the next leg (in the current traversal direction).
            state.active_waypoint += self._step
            self._leg_start = target
            # Multi-advance: consume stacked waypoints already within the arrival
            # radius in one tick, so dense replay tracks don't stall one point per tick.
            for _ in range(len(state.waypoints)):
                if not 0 <= state.active_waypoint < len(state.waypoints):
                    break
                nxt = state.waypoints[state.active_waypoint].point
                if haversine_m(pos, nxt) <= self.config.arrival_radius_m:
                    state.active_waypoint += self._step
                    self._leg_start = nxt
                else:
                    break
            if not 0 <= state.active_waypoint < len(state.waypoints):
                if self._wrap_or_bounce(state, pos):
                    return GuidedSetpoint(target_heading=state.heading_deg, thrust=0.0)
            target = state.waypoints[state.active_waypoint].point
            distance = haversine_m(pos, target)
            state.distance_to_waypoint_m = distance

        bearing = initial_bearing(pos, target)
        xte = cross_track(self._leg_start, target, pos)
        state.cross_track_m = xte.distance_m
        state.bearing_to_dest = bearing

        # Forward or reverse? For a mark that's behind and near, backing toward it
        # beats swinging the whole boat around (see maneuver_to_bearing).
        if self.config.allow_reverse:
            _, _, self._reverse = maneuver_to_bearing(
                state.heading_deg, bearing, distance,
                turn_rate_dps=self.config.turn_rate_dps,
                fwd_speed_mps=self.config.boat_speed_mps,
                reverse_efficiency=self.config.reverse_efficiency,
                currently_reverse=self._reverse,
            )
        else:
            self._reverse = False

        if self._reverse:
            # Back straight toward the mark (stern-first pure pursuit) — a short
            # manoeuvre, so we don't layer cross-track trim on top. The helm
            # flips steering authority under negative thrust.
            return GuidedSetpoint(
                target_heading=normalize_deg(bearing - 180.0),
                thrust=-self.config.throttle,
            )

        correction = max(
            -self.config.max_xte_correction_deg,
            min(self.config.max_xte_correction_deg, self.config.xte_gain * xte.distance_m),
        )
        # Positive xte => boat is right of track => steer left => reduce heading.
        heading = normalize_deg(bearing - correction)
        return GuidedSetpoint(target_heading=heading, thrust=self.config.throttle)


@dataclass
class WorkAreaConfig:
    """Work Area mode: visit each spot, hold position there, then advance."""

    arrival_radius_m: float = 8.0   # within this of a spot -> begin the hold
    dwell_s: float = 120.0          # auto-advance after this (when advance="timed")
    advance: str = "manual"         # "manual" (on-screen button) | "timed" (dwell)
    throttle: float = 0.6
    orient_thrust: float = 0.12     # gentle thrust used to orient to a spot's
                                    # desired hold heading once on station


class WorkAreaMode(ControlMode):
    """Work an area spot by spot: travel to ``state.waypoints[active]``, HOLD
    position there (active spot-lock) while the user works, then advance to the
    next spot -- after ``dwell_s`` ("timed" advance) and/or when the user taps
    "Go to next spot" (``state.work_next_requested``). ``route_loop`` cycles the
    spots; ``route_patrol`` runs them there-and-back; otherwise the boat holds the
    final spot once the route is done.

    Travel reuses the waypoint leg logic (cross-track + forward/reverse helm); the
    hold delegates to AnchorHoldMode (spot-lock). Dwell time is accumulated from
    ``dt`` so the deterministic harness drives it without a wall clock.
    """

    name = ControlModeName.WORK_AREA

    def __init__(
        self,
        config: WorkAreaConfig | None = None,
        *,
        waypoint_config: WaypointConfig | None = None,
        anchor_config: AnchorConfig | None = None,
    ) -> None:
        self.config = config or WorkAreaConfig()
        self._travel = waypoint_config or WaypointConfig()  # shared leg-nav params
        self._anchor = AnchorHoldMode(anchor_config)
        self._phase = "travel"          # "travel" | "hold"
        self._leg_start: GeoPoint | None = None
        self._reverse = False
        self._step = 1                  # +1 forward / -1 back (patrol)
        self._dwell_elapsed = 0.0

    def activate(self, state: NavigationState) -> None:
        self._phase = "travel"
        self._leg_start = state.position
        self._reverse = False
        self._step = 1
        self._dwell_elapsed = 0.0
        state.route_complete = False
        state.work_holding = False
        state.work_next_requested = False
        state.work_dwell_remaining_s = 0.0

    def _wrap_or_bounce(self, state: NavigationState, pos: GeoPoint) -> bool:
        """Ran off an END of the spot list: loop wraps to the start, patrol
        reverses direction, otherwise the work route completes (return True)."""
        n = len(state.waypoints)
        if state.route_loop:
            state.active_waypoint = 0
            self._step = 1
            self._leg_start = pos
            return False
        if state.route_patrol and n >= 2:
            self._step = -self._step
            state.active_waypoint += 2 * self._step
            self._leg_start = pos
            return False
        state.route_complete = True
        return True

    def _begin_hold(self, state: NavigationState, spot: GeoPoint, dt: float) -> Setpoint:
        self._phase = "hold"
        self._dwell_elapsed = 0.0
        state.anchor = spot
        state.anchor_heading = state.heading_deg
        self._anchor.activate(state)
        state.work_holding = True
        return self._anchor.update(state, dt)

    def update(self, state: NavigationState, dt: float) -> Setpoint:
        pos = state.position
        if pos is None or not state.waypoints:
            state.work_holding = self._phase == "hold"
            return ManualSetpoint(0.0, 0.0)

        if not 0 <= state.active_waypoint < len(state.waypoints):
            if self._wrap_or_bounce(state, pos):
                state.work_holding = False
                state.work_dwell_remaining_s = 0.0
                return GuidedSetpoint(target_heading=state.heading_deg, thrust=0.0)

        if self._phase == "hold":
            sp = self._anchor.update(state, dt)  # spot-lock; sets distance_to_anchor_m
            self._dwell_elapsed += dt
            timed = self.config.advance == "timed"
            want_advance = state.work_next_requested or (
                timed and self._dwell_elapsed >= self.config.dwell_s
            )
            state.work_next_requested = False  # consume the button press
            if want_advance and not state.route_complete:
                self._leg_start = state.waypoints[state.active_waypoint].point
                self._reverse = False
                state.active_waypoint += self._step
                if not 0 <= state.active_waypoint < len(state.waypoints):
                    self._wrap_or_bounce(state, pos)  # loop/patrol, or sets route_complete
                if state.route_complete:
                    state.active_waypoint = max(
                        0, min(state.active_waypoint, len(state.waypoints) - 1)
                    )
                else:
                    self._phase = "travel"
                    self._dwell_elapsed = 0.0
                    state.work_holding = False
                    state.work_dwell_remaining_s = 0.0
                    # fall through to the travel leg below
            if self._phase == "hold":
                state.work_holding = True
                state.work_dwell_remaining_s = (
                    max(0.0, self.config.dwell_s - self._dwell_elapsed)
                    if (timed and not state.route_complete) else 0.0
                )
                # On station, orient to the spot's desired heading if one is set.
                # Best-effort: a single bow thruster can't perfectly hold heading
                # AND position, so position recovery (the anchor) wins when the boat
                # drifts out of the hold radius.
                spot_hdg = state.waypoints[state.active_waypoint].heading
                if spot_hdg is not None and state.distance_to_anchor_m <= max(state.anchor_radius_m, 1.0):
                    return GuidedSetpoint(
                        target_heading=spot_hdg % 360.0, thrust=self.config.orient_thrust
                    )
                return sp

        # Travel toward the active spot.
        if self._leg_start is None:
            self._leg_start = pos
        target = state.waypoints[state.active_waypoint].point
        distance = haversine_m(pos, target)
        state.distance_to_waypoint_m = distance
        if distance <= self.config.arrival_radius_m:
            return self._begin_hold(state, target, dt)

        bearing = initial_bearing(pos, target)
        xte = cross_track(self._leg_start, target, pos)
        state.cross_track_m = xte.distance_m
        state.bearing_to_dest = bearing
        if self._travel.allow_reverse:
            _, _, self._reverse = maneuver_to_bearing(
                state.heading_deg, bearing, distance,
                turn_rate_dps=self._travel.turn_rate_dps,
                fwd_speed_mps=self._travel.boat_speed_mps,
                reverse_efficiency=self._travel.reverse_efficiency,
                currently_reverse=self._reverse,
            )
        else:
            self._reverse = False
        if self._reverse:
            return GuidedSetpoint(
                target_heading=normalize_deg(bearing - 180.0),
                thrust=-self.config.throttle,
            )
        correction = max(
            -self._travel.max_xte_correction_deg,
            min(self._travel.max_xte_correction_deg, self._travel.xte_gain * xte.distance_m),
        )
        heading = normalize_deg(bearing - correction)
        return GuidedSetpoint(target_heading=heading, thrust=self.config.throttle)


@dataclass
class FollowApbConfig:
    throttle: float = 0.6
    xte_gain: float = 2.0  # degrees of correction per metre of cross-track error
    max_xte_correction_deg: float = 60.0


class FollowApbMode(ControlMode):
    """Steer from an externally supplied APB sentence (e.g. a phone nav app or
    chartplotter acting as the route source). Uses the APB's bearing-to-
    destination biased by its cross-track error and steer-to direction."""

    name = ControlModeName.FOLLOW_APB

    def __init__(self, config: FollowApbConfig | None = None) -> None:
        self.config = config or FollowApbConfig()

    def update(self, state: NavigationState, dt: float) -> Setpoint:
        if not state.has_apb:
            # No autopilot sentence received yet: hold current heading, idle.
            return GuidedSetpoint(target_heading=state.heading_deg, thrust=0.0)

        bearing = state.apb_bearing_to_dest
        xte = abs(state.apb_cross_track_m)
        state.cross_track_m = state.apb_cross_track_m
        state.bearing_to_dest = bearing

        correction = min(self.config.max_xte_correction_deg, self.config.xte_gain * xte)
        # APB tells us which way to steer to regain the track.
        if state.apb_steer_to == "R":
            heading = normalize_deg(bearing + correction)
        else:
            heading = normalize_deg(bearing - correction)
        return GuidedSetpoint(target_heading=heading, thrust=self.config.throttle)


@dataclass
class ContourConfig:
    throttle: float = 0.5  # default forward drive when no cruise (knots) hold
    # Distance (m) the boat must travel before we re-evaluate the depth trend.
    # Comparing depth now vs. a few metres ago tells us which way the bottom
    # slopes (the gradient along our track), so we know which way to curve.
    trend_distance_m: float = 4.0
    # Heading correction per metre of depth error, capped to keep turns gentle.
    heading_gain_deg_per_m: float = 6.0
    max_offset_deg: float = 30.0


class ContourFollowMode(ControlMode):
    """Follow a depth contour (isobath).

    Drives forward at the set speed while steering to keep ``state.depth_m`` at
    ``target_depth_m``. It estimates the bottom's slope *along the track* from
    the depth TREND -- how much the depth changed over the last few metres of
    travel -- and curves toward deeper or shallower water to null the depth
    error, so the boat tracks the chosen isobath rather than just driving
    straight. ``side`` ("deep"/"shallow") picks which way it turns to correct,
    matching the bank the operator wants to favour. If no sounding is available
    it simply holds heading.
    """

    name = ControlModeName.CONTOUR_FOLLOW

    def __init__(self, config: ContourConfig | None = None) -> None:
        self.config = config or ContourConfig()
        self._ref_pos: GeoPoint | None = None
        self._ref_depth: float = 0.0
        self._base_heading: float = 0.0  # along-contour heading we weave around
        self.error_m = 0.0  # exposed for telemetry

    def activate(self, state: NavigationState) -> None:
        self._ref_pos = state.position
        self._ref_depth = state.depth_m
        self._base_heading = state.heading_deg
        self.error_m = 0.0

    def update(self, state: NavigationState, dt: float) -> Setpoint:
        depth = state.depth_m
        heading = state.heading_deg
        thrust = self.config.throttle

        # No usable sounding (<=0 = unknown/no return): hold heading, keep moving.
        if depth <= 0.0:
            return GuidedSetpoint(target_heading=heading, thrust=thrust)

        target = state.contour_target_depth_m
        # Positive error => we are too DEEP (need to head toward shallower water).
        self.error_m = depth - target

        pos = state.position
        if pos is not None:
            if self._ref_pos is None:
                self._ref_pos = pos
                self._ref_depth = depth
            elif haversine_m(self._ref_pos, pos) >= self.config.trend_distance_m:
                # Use the depth TREND along the track to keep the along-contour
                # base heading aligned with the isobath as the bottom curves. If
                # the depth changed while running near the target depth, the
                # contour is bending, so rotate the base heading to chase it
                # (toward deeper if we've drifted shallow, toward shallower if
                # deep) -- a slow correction that follows a curving isobath.
                ddepth = depth - self._ref_depth
                if abs(self.error_m) < 1.0 and abs(ddepth) > 0.05:
                    deep_side = 1.0 if state.contour_side == "deep" else -1.0
                    nudge = deep_side * _clamp(-ddepth * 5.0, -15.0, 15.0)
                    self._base_heading = normalize_deg(self._base_heading + nudge)
                self._ref_pos = pos
                self._ref_depth = depth

        # ``side`` says which side of the boat the operator wants the DEEP water
        # on. To null the depth error we weave off the along-contour BASE heading
        # (captured when engaged), not the live heading -- otherwise a constant
        # turn offset would just spin the boat in a circle:
        #   too deep  (error>0) -> aim toward the SHALLOW side
        #   too shallow(error<0)-> aim toward the DEEP side
        # With "deep" on starboard, the deep side is +90deg; aiming toward deep
        # is a positive (starboard) offset from base. "shallow" mirrors it. The
        # magnitude is proportional to the error and capped so the approach to
        # the isobath stays gentle.
        deep_side_sign = 1.0 if state.contour_side == "deep" else -1.0
        want_deeper = self.error_m < 0.0
        turn = deep_side_sign if want_deeper else -deep_side_sign

        magnitude = min(
            self.config.max_offset_deg,
            self.config.heading_gain_deg_per_m * abs(self.error_m),
        )
        offset = turn * magnitude
        return GuidedSetpoint(
            target_heading=normalize_deg(self._base_heading + offset), thrust=thrust
        )


@dataclass
class OrbitConfig:
    throttle: float = 0.5
    # Heading correction per metre of radial error, capped. Pulls the boat onto
    # the ring (converges) while the tangent term carries it around.
    radial_gain_deg_per_m: float = 3.0
    max_radial_correction_deg: float = 60.0


class OrbitMode(ControlMode):
    """Orbit a centre point at a fixed radius (circle / racetrack hold).

    Each tick it computes the bearing from the centre to the boat, advances that
    bearing a little in the travel direction (cw/ccw) to get a point slightly
    *ahead* on the ring, aims there, and biases the heading by a radial-error
    correction so the boat both converges to the ring and holds it. Drives
    forward at the set speed. ``range_m`` (distance to centre) is exposed for
    telemetry.
    """

    name = ControlModeName.ORBIT

    def __init__(self, config: OrbitConfig | None = None) -> None:
        self.config = config or OrbitConfig()
        self.range_m = 0.0  # exposed for telemetry

    def update(self, state: NavigationState, dt: float) -> Setpoint:
        pos = state.position
        center = state.orbit_center
        if pos is None or center is None:
            return GuidedSetpoint(target_heading=state.heading_deg, thrust=0.0)

        radius = max(1.0, state.orbit_radius_m)
        ccw = state.orbit_direction == "ccw"
        sign = -1.0 if ccw else 1.0  # cw advances bearing-from-centre +, ccw -

        range_m = haversine_m(center, pos)
        self.range_m = range_m
        state.distance_to_anchor_m = range_m  # reuse the HUD range field

        bearing_out = initial_bearing(center, pos)  # centre -> boat

        # Pure tangent: travel direction along the ring at the boat's bearing
        # (perpendicular to the radial, on the chosen turn side).
        tangent = normalize_deg(bearing_out + sign * 90.0)

        # Radial correction so the boat converges to the ring and holds it:
        # outside (radial_err>0) -> steer inward; inside -> steer outward. The
        # correction rotates the tangent heading toward/away from the centre.
        radial_err = range_m - radius  # + = outside
        correction = _clamp(
            self.config.radial_gain_deg_per_m * radial_err,
            -self.config.max_radial_correction_deg,
            self.config.max_radial_correction_deg,
        )
        # When outside (radial_err>0) steer toward the centre; when inside, steer
        # outward. Rotating the tangent by ``+sign*correction`` turns it toward
        # the inward radial for both cw and ccw (verified for each sign), so the
        # boat spirals onto the ring and then holds the tangent (correction->0).
        heading = normalize_deg(tangent + sign * correction)
        return GuidedSetpoint(target_heading=heading, thrust=self.config.throttle)


@dataclass
class TrollingConfig:
    throttle: float = 0.4


class TrollingMode(ControlMode):
    """S-curve trolling weave.

    Adds a sinusoidal heading offset ``amplitude_deg * sin(2*pi*t/period_s)``
    around a base heading (the heading when engaged, unless one is given) while
    driving forward at the set speed -- the lazy S-pattern used to cover water
    and vary lure action when trolling. ``phase`` (radians) is exposed for
    telemetry.
    """

    name = ControlModeName.TROLLING

    def __init__(self, config: TrollingConfig | None = None) -> None:
        self.config = config or TrollingConfig()
        self._t = 0.0
        self.phase = 0.0  # exposed for telemetry

    def activate(self, state: NavigationState) -> None:
        self._t = 0.0
        self.phase = 0.0

    def update(self, state: NavigationState, dt: float) -> Setpoint:
        self._t += dt
        period = max(0.1, state.trolling_period_s)
        self.phase = (2.0 * math.pi * self._t / period) % (2.0 * math.pi)
        offset = state.trolling_amplitude_deg * math.sin(self.phase)
        heading = normalize_deg(state.trolling_base_heading + offset)
        return GuidedSetpoint(target_heading=heading, thrust=self.config.throttle)
