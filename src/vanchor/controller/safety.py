"""Safety governor: the last line of defence before a command reaches the motor.

This is a pure, synchronous filter sitting between the helm (which produces an
*intended* :class:`MotorCommand`) and the motor controller (which actuates it).
It never decides *where* to go -- it only restrains *how* commands are applied,
so that a misbehaving mode, a flaky GPS, or a dragging anchor cannot drive the
boat dangerously.

The governor keeps a small amount of internal state between ticks (the last
applied thrust, a reverse cooldown timer, and the time since the last fresh
GPS fix). It is deliberately free of I/O and of the event bus so it can be
exhaustively unit-tested.

Behaviours, all applied within a single :meth:`SafetyGovernor.govern` call:

* **Thrust slew limiting** -- the magnitude of thrust change per tick is capped
  at ``max_thrust_slew_per_s * dt`` so the prop cannot slam between settings.
* **Reverse protection** -- a sign flip of thrust (ahead<->astern) is blocked
  until thrust has rested near zero for ``reverse_delay_s`` seconds, avoiding
  abrupt gear-style reversals.
* **Loss-of-fix failsafe** -- once the time since the last fresh fix exceeds
  ``fix_timeout_s`` thrust is forced to zero so the boat coasts rather than
  steaming blind.
* **Anchor drag alarm** -- in anchor-hold mode, drifting beyond
  ``drag_alarm_factor * anchor_radius_m`` from the anchor raises an alarm.
* **Steering slew limiting** -- the steering change per tick is capped at
  ``max_steer_slew_per_s * dt`` so the command stays within the steering head's
  real rotation speed (and isn't a gear-shredding high-frequency jitter).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from math import copysign

from shapely.affinity import scale
from shapely.geometry import Point, Polygon
from shapely.prepared import prep

from ..core.models import ControlModeName, MotorCommand
from ..core.state import NavigationState

logger = logging.getLogger("vanchor.safety")

# Thrust magnitudes at or below this are treated as "stopped" for the purpose
# of reverse protection (so tiny residual thrust does not block a reversal).
_THRUST_EPSILON = 0.02


@dataclass
class SafetyConfig:
    """Tunable limits for the :class:`SafetyGovernor`."""

    # Maximum change in normalized thrust per second (slew-rate limit).
    max_thrust_slew_per_s: float = 1.0
    # Maximum change in normalized steering per second -- the motor's steering
    # head can only physically rotate so fast, and a high-frequency jittery
    # command would tear up the gearbox. In normalized units/s (multiply by the
    # boat's max_steer_angle_deg to get deg/s of rotation). Set <= 0 to disable.
    max_steer_slew_per_s: float = 1.4
    # Seconds thrust must rest near zero before the sign may flip (reverse).
    reverse_delay_s: float = 1.0
    # Seconds without a fresh fix before thrust is forced to zero.
    fix_timeout_s: float = 3.0
    # Loss-of-fix failsafe master switch. OFF by default: losing the GPS fix does
    # NOT cut thrust (the boat holds its last command). Enable it to force a stop
    # after fix_timeout_s without a fresh fix.
    fix_failsafe_enabled: bool = False
    # Anchor drag alarm trips beyond this multiple of the anchor radius.
    drag_alarm_factor: float = 2.0
    # --- Shallow-water / geofence auto-stop (#62) ----------------------- #
    # Cut thrust when the sounded depth drops below this (metres). 0 disables
    # the check (and an unknown/no-return depth never trips it either).
    min_depth_m: float = 0.0
    # Also cut thrust when the boat is inside -- or within this lookahead (m) of
    # -- a no-go polygon. The lookahead gives the boat room to stop before it
    # actually enters the zone.
    nogo_lookahead_m: float = 5.0


@dataclass
class SafetyStatus:
    """What the governor did on a single tick, for telemetry and alarms."""

    thrust_limited: bool = False
    steer_limited: bool = False
    reverse_blocked: bool = False
    fix_lost: bool = False
    drag_alarm: bool = False
    # Shallow-water / geofence auto-stop (#62).
    shallow_stop: bool = False
    nogo_stop: bool = False
    min_depth_m: float = 0.0
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "thrust_limited": self.thrust_limited,
            "steer_limited": self.steer_limited,
            "reverse_blocked": self.reverse_blocked,
            "fix_lost": self.fix_lost,
            "drag_alarm": self.drag_alarm,
            "shallow_stop": self.shallow_stop,
            "nogo_stop": self.nogo_stop,
            "min_depth_m": self.min_depth_m,
            "messages": list(self.messages),
        }


class SafetyGovernor:
    """Filters motor commands and raises alarms, holding state across ticks."""

    def __init__(self, config: SafetyConfig | None = None) -> None:
        self.config = config or SafetyConfig()
        # Last thrust/steering we actually allowed through (slew-limit anchors).
        self._last_thrust: float = 0.0
        self._last_steering: float = 0.0
        # The last NON-ZERO applied thrust DIRECTION (+1 ahead / -1 astern / 0
        # never driven). It is "sticky": it persists through a tick (or many) at
        # ~zero thrust, so a PID that crosses zero for a single tick
        # (+0.8 -> 0 -> -0.5) is still recognised as a reversal and gated. Mirrors
        # the firmware's applied-direction interlock (engine.ino).
        self._last_applied_dir: float = 0.0
        # Last *desired* (pre-slew) steering -- the closed-loop steering target,
        # exposed for the steering gauge (target vs feedback).
        self.desired_steering: float = 0.0
        # Seconds thrust has continuously been within the stop epsilon.
        self._rest_timer_s: float = 0.0
        # Seconds since the last fresh fix was observed.
        self._time_since_fix_s: float = 0.0
        # No-go polygons in (lon, lat) order, prepared for fast contains/distance.
        self._nogo: list[Polygon] = []
        self._nogo_prepared: list = []

    def set_nogo_zones(self, zones: list[list[tuple[float, float]]]) -> None:
        """Replace the no-go polygons. ``zones`` is a list of rings, each a list
        of ``(lat, lon)`` vertices. Degenerate rings (<3 points) are skipped."""
        polys: list[Polygon] = []
        for ring in zones:
            if len(ring) < 3:
                continue
            # shapely works in (x, y) = (lon, lat).
            poly = Polygon([(lon, lat) for (lat, lon) in ring])
            if not poly.is_valid:
                poly = poly.buffer(0)
            if not poly.is_empty:
                polys.append(poly)
        self._nogo = polys
        self._nogo_prepared = [prep(p) for p in polys]

    @property
    def nogo_zone_count(self) -> int:
        return len(self._nogo)

    def reset(self, thrust: float = 0.0, steering: float = 0.0) -> None:
        """Forget the transient timers (e.g. on mode change or restart), but seed
        the slew anchors from the given last-applied command so thrust/steering
        ramp from where the boat actually IS rather than snapping back through
        zero (which would surge the prop and bypass the reverse interlock)."""
        self._last_thrust = thrust
        self._last_steering = steering
        # Preserve the applied direction across the reset so a reset mid-drive
        # can't be used to sneak through an un-gated reversal.
        if abs(thrust) > _THRUST_EPSILON:
            self._last_applied_dir = copysign(1.0, thrust)
        self._rest_timer_s = 0.0
        self._time_since_fix_s = 0.0

    def _in_or_near_nogo(self, state: NavigationState) -> bool:
        """True if the boat is inside, or within ``nogo_lookahead_m`` of, a no-go
        polygon. Returns False when the position is unknown."""
        pos = state.position
        if pos is None or pos.is_null():
            return False
        pt = Point(pos.lon, pos.lat)
        # Convert the metric lookahead to a distance we can compare in shapely's
        # planar (lon, lat) space. Latitude is ~111.32 km/deg everywhere, but a
        # degree of LONGITUDE shrinks by cos(lat) toward the poles, so using the
        # latitude scale for BOTH axes would UNDER-cover E-W (~50% at 60°N) -- the
        # opposite of a safe margin. To keep both axes on the same metric scale we
        # squash longitude by cos(lat) (so 1 scaled-degree = 111.32 km on either
        # axis), then compare distances against the latitude-degree radius.
        look_m = max(0.0, self.config.nogo_lookahead_m)
        look_deg = look_m / 111320.0
        coslat = max(0.05, math.cos(math.radians(pos.lat)))  # floored near poles
        pt_s = Point(pos.lon * coslat, pos.lat)
        for poly, prepared in zip(self._nogo, self._nogo_prepared):
            if prepared.covers(pt):
                return True
            if look_deg <= 0.0:
                continue
            poly_s = scale(poly, xfact=coslat, yfact=1.0, origin=(0.0, 0.0))
            if poly_s.distance(pt_s) <= look_deg:
                return True
        return False

    def govern(
        self,
        command: MotorCommand,
        state: NavigationState,
        dt: float,
        fix_is_fresh: bool,
    ) -> tuple[MotorCommand, SafetyStatus]:
        """Filter ``command`` and report what was done.

        ``dt`` is the elapsed time since the previous call in seconds.
        ``fix_is_fresh`` is True when a new GPS fix arrived since the last tick;
        the governor accumulates the gap itself for the loss-of-fix failsafe.
        """
        status = SafetyStatus()
        cfg = self.config
        status.min_depth_m = cfg.min_depth_m

        # Work on the clamped command so all reasoning is in [-1, 1].
        desired = command.clamped().thrust
        steering = command.clamped().steering

        # --- Shallow-water / geofence auto-stop (#62) ------------------ #
        # A valid, too-shallow sounding cuts thrust. Depth <= 0 means "unknown /
        # no return", which must NOT trip the alarm (don't false-stop in deep
        # water where the sounder simply isn't reporting).
        if cfg.min_depth_m > 0.0 and 0.0 < state.depth_m < cfg.min_depth_m:
            status.shallow_stop = True
            status.messages.append(
                f"shallow water: depth {state.depth_m:.1f}m < {cfg.min_depth_m:.1f}m; stop"
            )
            desired = 0.0
        # Inside (or within lookahead of) a no-go polygon cuts thrust too.
        if self._nogo_prepared and self._in_or_near_nogo(state):
            status.nogo_stop = True
            status.messages.append("inside/near a no-go zone; stop")
            desired = 0.0

        # --- Loss-of-fix failsafe ------------------------------------- #
        if fix_is_fresh:
            self._time_since_fix_s = 0.0
        else:
            self._time_since_fix_s += dt
        if cfg.fix_failsafe_enabled and self._time_since_fix_s > cfg.fix_timeout_s:
            status.fix_lost = True
            status.messages.append(
                f"fix lost for {self._time_since_fix_s:.1f}s > "
                f"{cfg.fix_timeout_s:.1f}s; forcing stop"
            )
            desired = 0.0

        # --- Reverse protection --------------------------------------- #
        # Update the "resting near zero" timer from where we currently are.
        if abs(self._last_thrust) <= _THRUST_EPSILON:
            self._rest_timer_s += dt
        else:
            self._rest_timer_s = 0.0

        # A sign flip relative to the last APPLIED direction counts as a
        # reversal. We compare against the sticky ``_last_applied_dir`` (not the
        # instantaneous ``_last_thrust``) so a command that passes through zero
        # for one or more ticks -- exactly what a PID crossing zero produces,
        # e.g. +0.8 -> 0 -> -0.5 -- is still gated. Near-zero requests are
        # treated as unsigned so we never block coming *to* a stop.
        flipping = (
            abs(desired) > _THRUST_EPSILON
            and self._last_applied_dir != 0.0
            and copysign(1.0, desired) != self._last_applied_dir
        )
        if flipping and self._rest_timer_s < cfg.reverse_delay_s:
            status.reverse_blocked = True
            status.messages.append(
                f"reverse blocked: thrust must rest near zero for "
                f"{cfg.reverse_delay_s:.1f}s (rested {self._rest_timer_s:.1f}s)"
            )
            # Hold at zero rather than flip; this also lets the rest timer build.
            desired = 0.0

        # --- Thrust slew limiting ------------------------------------- #
        max_step = cfg.max_thrust_slew_per_s * dt
        delta = desired - self._last_thrust
        if max_step >= 0.0 and abs(delta) > max_step:
            status.thrust_limited = True
            status.messages.append(
                f"thrust slew-limited: |Δ|={abs(delta):.3f} > {max_step:.3f}"
            )
            applied_thrust = self._last_thrust + copysign(max_step, delta)
        else:
            applied_thrust = desired

        self._last_thrust = applied_thrust
        # Remember the last direction we actually drove (sticky through zero) so
        # the reverse interlock survives a through-zero PID crossing.
        if abs(applied_thrust) > _THRUST_EPSILON:
            self._last_applied_dir = copysign(1.0, applied_thrust)

        # --- Steering slew limiting ----------------------------------- #
        # The steering head can only rotate so fast; cap the change per tick so
        # the command is physically realisable and not a jittery sawtooth.
        self.desired_steering = steering
        max_steer_step = cfg.max_steer_slew_per_s * dt
        steer_delta = steering - self._last_steering
        if cfg.max_steer_slew_per_s > 0.0 and abs(steer_delta) > max_steer_step:
            status.steer_limited = True
            applied_steering = self._last_steering + copysign(max_steer_step, steer_delta)
        else:
            applied_steering = steering
        self._last_steering = applied_steering

        # --- Anchor drag alarm ---------------------------------------- #
        # Any station-keeping mode that holds via an anchor must be watched,
        # including the learned spot-lock (ANCHOR_ML). WORK_AREA is deliberately
        # excluded: it also sets state.anchor, but leaves it stale while TRAVELLING
        # between spots, which would false-trip the alarm on the way to a spot.
        if state.anchor is not None and state.mode in (
            ControlModeName.ANCHOR_HOLD,
            ControlModeName.ANCHOR_ML,
        ):
            threshold = cfg.drag_alarm_factor * state.anchor_radius_m
            if state.distance_to_anchor_m > threshold:
                status.drag_alarm = True
                status.messages.append(
                    f"anchor drag: {state.distance_to_anchor_m:.1f}m > "
                    f"{threshold:.1f}m"
                )

        if status.messages:
            logger.debug("safety: %s", "; ".join(status.messages))

        return (
            MotorCommand(thrust=applied_thrust, steering=applied_steering),
            status,
        )
