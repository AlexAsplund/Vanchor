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
* **Low-battery thrust derate** -- an externally-set cap (the battery ladder,
  #49) limits the applied thrust magnitude in progressive steps as the pack
  drains; it only ever lowers thrust and never overrides STOP or a failsafe.
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
from typing import Any

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
    # Loss-of-fix failsafe master switch. ON by default: the conservative coast
    # is the right default for a trolling motor -- once no fresh fix has arrived
    # for fix_timeout_s thrust is forced to zero rather than steaming blind. Set
    # False to keep holding the last command through a fix dropout.
    fix_failsafe_enabled: bool = True
    # Seconds without a fresh COMPASS heading before it is judged stale. While a
    # GUIDED (autopilot) mode is steering, a stale heading forces a safe coast
    # (zero thrust, steering held) so a dead compass in heading-hold can't circle
    # the boat at throttle forever. Manual driving is unaffected (a human steers).
    heading_stale_s: float = 3.0
    # Seconds without a fresh DEPTH sounding before it is judged stale. A stale
    # depth is treated as UNKNOWN by the shallow-water stop (rather than trusting
    # a frozen sounding), so a hung sounder neither false-stops nor silently
    # passes the min-depth check.
    depth_stale_s: float = 10.0
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
    # Stale compass heading forced a coast while a guided mode was steering.
    heading_stale: bool = False
    # Shallow-water / geofence auto-stop (#62).
    shallow_stop: bool = False
    nogo_stop: bool = False
    min_depth_m: float = 0.0
    # Low-battery thrust-derating ladder (#49): the current max-thrust CAP
    # (1.0 = full) and whether a derate is actually in force this tick. Exposed on
    # the status object (and via ``SafetyGovernor.thrust_cap``) for callers/tests;
    # kept OUT of ``to_dict`` so the serialized telemetry contract is unchanged.
    thrust_cap: float = 1.0
    thrust_derated: bool = False
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "thrust_limited": self.thrust_limited,
            "steer_limited": self.steer_limited,
            "reverse_blocked": self.reverse_blocked,
            "fix_lost": self.fix_lost,
            "drag_alarm": self.drag_alarm,
            "heading_stale": self.heading_stale,
            "shallow_stop": self.shallow_stop,
            "nogo_stop": self.nogo_stop,
            "min_depth_m": self.min_depth_m,
            "messages": list(self.messages),
        }


@dataclass(frozen=True)
class BatteryLadder:
    """Pure low-battery thrust-derating ladder (#49).

    Maps a battery state-of-charge (percent) to a maximum-thrust CAP in ``[0, 1]``.
    As SoC falls through each ``(soc_pct, cap)`` rung the cap steps DOWN (a soft
    derate) so propulsion is progressively limited BEFORE the boat is handed off
    to the existing RTL/failsafe at ``rtl_soc_pct``.

    The ladder is deliberately one-directional: the cap it returns is
    monotonically NON-INCREASING as SoC drops (it is the minimum cap over every
    rung whose threshold the SoC has fallen to or below), and it is 1.0 (full)
    above the top rung. It never RAISES thrust. STOP and every failsafe still take
    precedence in the governor -- a cap only limits magnitude, it can never force
    motion.
    """

    rungs: tuple[tuple[float, float], ...] = ()
    rtl_soc_pct: float = 0.0
    enabled: bool = True

    @classmethod
    def from_config(cls, safety: Any) -> "BatteryLadder":
        """Build from a config object exposing ``battery_ladder`` (a list of
        ``[soc_pct, cap]`` pairs), ``battery_rtl_soc_pct`` and
        ``battery_ladder_enabled``. Rungs are coerced to floats and caps clamped
        to ``[0, 1]``; malformed rungs are skipped rather than raising."""
        raw = getattr(safety, "battery_ladder", None) or ()
        rungs: list[tuple[float, float]] = []
        for entry in raw:
            try:
                soc, cap = float(entry[0]), float(entry[1])
            except (TypeError, ValueError, IndexError):
                continue
            rungs.append((soc, max(0.0, min(1.0, cap))))
        return cls(
            rungs=tuple(rungs),
            rtl_soc_pct=float(getattr(safety, "battery_rtl_soc_pct", 0.0)),
            enabled=bool(getattr(safety, "battery_ladder_enabled", True)),
        )

    def cap_for(self, soc_pct: float) -> float:
        """Max-thrust cap for the given SoC (1.0 = full thrust allowed).

        The cap is the minimum over every rung the SoC has fallen to/below, so a
        lower SoC can only ever yield an equal-or-lower cap (monotone)."""
        if not self.enabled:
            return 1.0
        cap = 1.0
        for thresh, rung_cap in self.rungs:
            if soc_pct <= thresh:
                cap = min(cap, rung_cap)
        return cap

    def at_rtl(self, soc_pct: float) -> bool:
        """True once SoC has reached the lowest (RTL hand-off) stage."""
        return self.enabled and soc_pct <= self.rtl_soc_pct


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
        # Low-battery thrust-derating cap (#49): an externally-set ceiling on the
        # THRUST MAGNITUDE in [0, 1] (1.0 = no derate). Set by the battery ladder
        # from the ~1 Hz supervisor. It only ever LIMITS magnitude, so STOP and
        # every failsafe (which force thrust to zero) still take precedence.
        self._thrust_cap: float = 1.0
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

    @property
    def thrust_cap(self) -> float:
        """The current low-battery thrust-magnitude cap (1.0 = no derate)."""
        return self._thrust_cap

    def set_thrust_cap(self, cap: float) -> None:
        """Set the low-battery thrust-derating cap (#49), clamped to ``[0, 1]``.

        A soft ceiling on the applied thrust MAGNITUDE only: it never raises
        thrust and never forces motion, so STOP and every failsafe still take
        precedence. Set to 1.0 to remove the derate. Called by the battery ladder
        from the supervisor -- never from a command path -- so it cannot be used
        to WEAKEN a failsafe."""
        self._thrust_cap = max(0.0, min(1.0, float(cap)))

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
        *,
        heading_age_s: float | None = None,
        depth_age_s: float | None = None,
    ) -> tuple[MotorCommand, SafetyStatus]:
        """Filter ``command`` and report what was done.

        ``dt`` is the elapsed time since the previous call in seconds.
        ``fix_is_fresh`` is True when a new GPS fix arrived since the last tick;
        the governor accumulates the gap itself for the loss-of-fix failsafe.

        ``heading_age_s`` / ``depth_age_s`` are the seconds since the compass /
        depth sounder last reported (``None`` = never sampled / caller not
        tracking staleness -> treated as fresh, so unit tests and the harness are
        never false-tripped). A stale heading in a guided mode forces a coast; a
        stale depth is treated as unknown by the shallow-water stop.
        """
        status = SafetyStatus()
        cfg = self.config
        status.min_depth_m = cfg.min_depth_m

        # Work on the clamped command so all reasoning is in [-1, 1].
        desired = command.clamped().thrust
        steering = command.clamped().steering

        # --- Low-battery thrust derate (soft cap, #49) ----------------- #
        # An externally-set cap limits the THRUST MAGNITUDE only. It is applied
        # FIRST, but every zeroing failsafe below (fix-loss, shallow, no-go,
        # stale-heading) still overrides it, and a STOP command already arrives as
        # desired=0 -- which is under any cap -- so the derate can never keep the
        # boat moving. It only ever LOWERS thrust.
        cap = max(0.0, min(1.0, self._thrust_cap))
        status.thrust_cap = cap
        if cap < 1.0:
            status.thrust_derated = True
            if abs(desired) > cap:
                desired = copysign(cap, desired)

        # --- Stale compass heading ------------------------------------- #
        # A guided (autopilot) mode steers on the compass; if it goes silent the
        # boat would keep circling at throttle on a frozen heading. Force a coast
        # (zero thrust) and hold the steering head (zero delta) until it recovers.
        # Manual mode is untouched -- a human is doing the steering there.
        heading_stale = (
            heading_age_s is not None
            and heading_age_s > cfg.heading_stale_s
            and state.mode != ControlModeName.MANUAL
        )
        if heading_stale:
            status.heading_stale = True
            status.messages.append(
                f"compass heading stale {heading_age_s:.1f}s > "
                f"{cfg.heading_stale_s:.1f}s in {state.mode.value}; coasting"
            )
            desired = 0.0
            steering = self._last_steering  # hold the head: no slew on stale data

        # --- Shallow-water / geofence auto-stop (#62) ------------------ #
        # A valid, too-shallow sounding cuts thrust. Depth <= 0 means "unknown /
        # no return", which must NOT trip the alarm (don't false-stop in deep
        # water where the sounder simply isn't reporting). A STALE sounding is
        # likewise treated as unknown -- don't keep judging against a frozen
        # value once the sounder has gone quiet.
        depth_stale = depth_age_s is not None and depth_age_s > cfg.depth_stale_s
        if (
            cfg.min_depth_m > 0.0
            and not depth_stale
            and 0.0 < state.depth_m < cfg.min_depth_m
        ):
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
        # <= 0 means DISABLED (no slew limiting), matching the steering slew
        # behaviour where max_steer_slew_per_s <= 0 likewise means unlimited.
        max_step = cfg.max_thrust_slew_per_s * dt
        delta = desired - self._last_thrust
        if cfg.max_thrust_slew_per_s > 0.0 and abs(delta) > max_step:
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
        # including the learned spot-lock (ANCHOR_ML). WORK_AREA is gated on
        # state.work_holding so the alarm only fires while actually spot-locked
        # at a spot (not while travelling between spots, when state.anchor is
        # stale and would otherwise false-trip the alarm).
        if state.anchor is not None and (
            state.mode in (ControlModeName.ANCHOR_HOLD, ControlModeName.ANCHOR_ML,
                           ControlModeName.ANCHOR_LEFFE)
            or (state.mode == ControlModeName.WORK_AREA and state.work_holding)
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
