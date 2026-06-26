"""Typed data models shared across the whole system.

These are deliberately small, immutable-ish dataclasses. They are the common
vocabulary spoken by sensors, the navigator, control modes, the helm and the
motor controller, so that real and simulated devices are interchangeable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum


class ControlModeName(str, Enum):
    """The high level steering behaviours the controller can be in."""

    MANUAL = "manual"
    ANCHOR_HOLD = "anchor_hold"
    HEADING_HOLD = "heading_hold"
    WAYPOINT = "waypoint"
    FOLLOW_APB = "follow_apb"
    DRIFT = "drift"
    CONTOUR_FOLLOW = "contour_follow"
    ORBIT = "orbit"
    TROLLING = "trolling"


@dataclass(frozen=True)
class GeoPoint:
    """A WGS84 latitude/longitude in decimal degrees."""

    lat: float
    lon: float

    def as_tuple(self) -> tuple[float, float]:
        return (self.lat, self.lon)

    def is_null(self) -> bool:
        """True for the conventional ``(0, 0)`` "no fix" sentinel."""
        return self.lat == 0.0 and self.lon == 0.0


@dataclass(frozen=True)
class GpsFix:
    """A parsed position fix (from an RMC/GGA sentence or a simulated GPS)."""

    point: GeoPoint
    sog_knots: float = 0.0  # speed over ground
    cog_deg: float = 0.0  # course over ground
    timestamp: float = 0.0
    valid: bool = True


@dataclass(frozen=True)
class HeadingReading:
    """A compass heading sample in degrees (0..360, magnetic or true)."""

    heading_deg: float
    timestamp: float = 0.0


@dataclass(frozen=True)
class Waypoint:
    name: str
    point: GeoPoint


@dataclass(frozen=True)
class MotorCommand:
    """The actuator-level command sent to the motor controller.

    ``thrust`` is the normalized forward drive (-1 reverse .. 1 full ahead).
    ``steering`` is the normalized turn command (-1 hard port .. 1 hard
    starboard). A trolling motor realizes ``steering`` by physically rotating;
    a rudder boat would realize it with a rudder. The abstraction is the same.
    """

    thrust: float = 0.0
    steering: float = 0.0

    def clamped(self) -> "MotorCommand":
        return MotorCommand(
            thrust=_clamp(self.thrust, -1.0, 1.0),
            steering=_clamp(self.steering, -1.0, 1.0),
        )


@dataclass(frozen=True)
class ManualSetpoint:
    """Mode output: drive the motor directly."""

    thrust: float = 0.0
    steering: float = 0.0


@dataclass(frozen=True)
class GuidedSetpoint:
    """Mode output: hold a target heading; the helm derives the steering."""

    target_heading: float = 0.0
    thrust: float = 0.0


# A control mode produces one of these each tick.
Setpoint = ManualSetpoint | GuidedSetpoint


@dataclass(frozen=True)
class CrossTrackError:
    """Cross-track error relative to a leg. ``distance_m`` is signed: positive
    means the boat is to starboard (right) of the intended track."""

    distance_m: float
    steer_to: str  # "L" or "R" -- the direction to steer to get back on track


@dataclass
class Environment:
    """Wind and current acting on the boat. Directions are *toward* which the
    flow pushes, in degrees. Speeds are in m/s."""

    current_speed: float = 0.0
    current_dir: float = 0.0
    wind_speed: float = 0.0
    wind_dir: float = 0.0
    # Fraction of wind speed that translates into hull drift (leeway).
    wind_leeway: float = 0.03
    # Gustiness: std (m/s) and correlation time (s) of the time-varying gust the
    # simulator layers on top of the base wind. 0 amplitude = steady wind.
    gust_amplitude_mps: float = 0.0
    gust_tau_s: float = 5.0
    # Slow weather wander (much slower than gusts), in [0, 1]. 0 = steady.
    # The simulator evolves wind speed/direction (and current) by this much and
    # writes the evolving values back into wind_speed/wind_dir/current_speed.
    wind_variability: float = 0.0
    current_variability: float = 0.0

    def drift_vector(self) -> tuple[float, float]:
        """Net environmental drift as an (east, north) velocity in m/s."""
        ce = self.current_speed * math.sin(math.radians(self.current_dir))
        cn = self.current_speed * math.cos(math.radians(self.current_dir))
        we = self.wind_speed * self.wind_leeway * math.sin(math.radians(self.wind_dir))
        wn = self.wind_speed * self.wind_leeway * math.cos(math.radians(self.wind_dir))
        return (ce + we, cn + wn)


@dataclass
class BoatState:
    """Ground-truth physical state of the (simulated) boat."""

    point: GeoPoint = field(default_factory=lambda: GeoPoint(0.0, 0.0))
    heading_deg: float = 0.0  # the way the bow points
    speed_mps: float = 0.0  # forward speed through the water
    timestamp: float = 0.0
    # Velocity over ground (world frame, m/s) -- hull motion plus environmental
    # drift. This is what a real GPS reports as course/speed over ground.
    ground_ve: float = 0.0  # east
    ground_vn: float = 0.0  # north


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
