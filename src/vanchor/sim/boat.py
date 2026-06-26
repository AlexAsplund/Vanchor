"""A simple but believable boat motion model.

The model is intentionally minimal -- enough to exercise the control loops
realistically without pretending to be a hydrodynamics simulator:

  * Forward speed follows the commanded thrust through a first-order lag
    (the boat takes a moment to spin up / coast down).
  * Steering directly produces a yaw rate (a trolling motor turns the hull by
    rotating its thrust vector).
  * Wind and current add a drift velocity that displaces the boat without
    changing its heading -- this is what anchor-hold and track-keeping fight.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.geo import normalize_deg, offset_meters
from ..core.models import BoatState, Environment, GeoPoint, MotorCommand
import math


@dataclass
class BoatParams:
    max_speed_mps: float = 1.6  # ~3 knots, typical small trolling motor
    accel_tau_s: float = 2.5  # time constant of the speed response
    max_turn_rate_deg: float = 25.0  # yaw rate at full steering (with full thrust)
    reverse_efficiency: float = 0.6  # reverse thrust as a fraction of forward
    # Thrust at/above which steering has full authority. Below it, turning
    # authority scales down -- a trolling motor can't steer without running, so
    # at zero thrust the boat doesn't turn (matching the 3-DOF fossen model and
    # real hardware, and so a frozen steering command doesn't keep spinning it).
    steer_thrust_ref: float = 0.2


class Boat:
    def __init__(self, state: BoatState | None = None, params: BoatParams | None = None) -> None:
        self.state = state or BoatState()
        self.params = params or BoatParams()

    def step(self, dt: float, command: MotorCommand, env: Environment) -> None:
        if dt <= 0:
            return
        p = self.params
        s = self.state

        # Speed: first-order approach to the commanded steady-state speed.
        # Reverse bites less than forward (trolling-motor prop).
        eff = 1.0 if command.thrust >= 0 else p.reverse_efficiency
        target_speed = command.thrust * eff * p.max_speed_mps
        alpha = min(1.0, dt / p.accel_tau_s)
        s.speed_mps += (target_speed - s.speed_mps) * alpha

        # Heading: steering commands a yaw rate, but only with thrust behind it
        # (no prop wash, no steering authority).
        authority = min(1.0, abs(command.thrust) / p.steer_thrust_ref)
        s.heading_deg = normalize_deg(
            s.heading_deg + command.steering * p.max_turn_rate_deg * authority * dt
        )

        # Velocity = hull motion along heading + environmental drift.
        he = s.speed_mps * math.sin(math.radians(s.heading_deg))
        hn = s.speed_mps * math.cos(math.radians(s.heading_deg))
        de, dn = env.drift_vector()

        s.ground_ve = he + de
        s.ground_vn = hn + dn
        s.point = offset_meters(s.point, s.ground_ve * dt, s.ground_vn * dt)
        s.timestamp += dt

    def teleport(self, point: GeoPoint, heading: float | None = None) -> None:
        """Instantly move ground truth to ``point`` (optionally set heading) and
        zero all motion so the boat doesn't keep coasting from its old velocity."""
        s = self.state
        s.point = point
        if heading is not None:
            s.heading_deg = normalize_deg(float(heading))
        s.speed_mps = 0.0
        s.ground_ve = 0.0
        s.ground_vn = 0.0

    def truth(self) -> BoatState:
        """An immutable-ish snapshot of the current ground truth."""
        s = self.state
        return BoatState(
            point=s.point,
            heading_deg=s.heading_deg,
            speed_mps=s.speed_mps,
            timestamp=s.timestamp,
            ground_ve=s.ground_ve,
            ground_vn=s.ground_vn,
        )
