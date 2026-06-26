"""A higher-fidelity 3-DOF (surge-sway-yaw) maneuvering boat model.

This is a drop-in alternative to :class:`vanchor.sim.boat.Boat`. It exposes the
same interface (``state``, ``step``, ``truth``) but instead of a first-order
speed lag plus a kinematic yaw rate it integrates a proper rigid-body +
hydrodynamic model in body-fixed coordinates ``[u, v, r]`` (surge, sway, yaw
rate).

The structure is inspired by the MIT-licensed Fossen "otter" USV from
``cybergalactic/PythonVehicleSimulator``:

    (M_rb + M_a) * nu_dot + (D_lin + D_quad(nu)) * nu = tau

where ``nu = [u, v, r]`` are body velocities, ``M_rb`` is the rigid-body mass /
inertia, ``M_a`` is added mass, ``D_lin`` / ``D_quad`` are linear and quadratic
damping, and ``tau`` is the generalized force/moment produced by the thruster.

Thrust mapping: this models a **single steerable trolling motor mounted at the
bow**, at a signed longitudinal offset ``thruster_x_m`` from the centre of
gravity (positive = forward / bow). The motor produces a thrust of magnitude
``T = command.thrust * max_thrust_n`` (negative = reverse) directed along its
steered axis ``delta = command.steering * radians(max_steer_angle_deg)``:

    Fx = T * cos(delta)        # surge
    Fy = T * sin(delta)        # sway (+ = starboard)
    N  = thruster_x_m * Fy - thruster_y_m * Fx   # yaw from both lever arms

    tau = [Fx, Fy, N]

The key consequence of this (vectored-thrust / outboard) model is that **steering
authority scales with thrust**: with no thrust the motor produces no force and so
no yaw moment, hence essentially no turning -- a trolling motor cannot steer
without running. With a bow mount (``thruster_x_m > 0``) positive steering turns
the boat to starboard (heading increases). Cross-coupling between sway and yaw in
the damping / added-mass matrices makes the boat visibly "crab" (sway) during a
turn.

Integration is semi-implicit Euler: solve for ``nu_dot``, advance ``nu``, then
update heading by ``r*dt`` and the NED position from the body velocity rotated
into the local tangent plane plus the environmental drift.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np

from ..core.geo import normalize_deg, offset_meters
from ..core.models import BoatState, Environment, GeoPoint, MotorCommand

logger = logging.getLogger("vanchor.sim")


@dataclass
class FossenParams:
    """Physical constants for the 3-DOF model, tuned to a 4.1 m boat driven by a
    single steerable trolling motor mounted at the bow (~1.6 m/s top speed,
    ~12-25 deg/s full-thrust/full-steer turn rate).

    Masses are in kg, lengths in m, the yaw inertia in kg*m^2. Damping
    coefficients are in SI units consistent with forces in N and moments in
    N*m acting on velocities in m/s and rad/s.

    The surge linear-damping coefficient is *derived* from ``max_thrust_n`` and
    ``max_speed_mps`` so that full thrust converges to ~``max_speed_mps``; the
    yaw inertia is derived from the hull geometry. See :meth:`__post_init__`.
    """

    # Boat geometry / mass ----------------------------------------------- #
    length: float = 4.1  # overall length (m)
    beam: float = 1.7  # beam (m)
    mass: float = 300.0  # kg, hull + motor + battery + 1 person

    # Thruster -----------------------------------------------------------#
    max_thrust_n: float = 250.0  # ~55 lbf trolling motor at full thrust (N)
    reverse_efficiency: float = 0.6  # reverse thrust as a fraction of forward
    max_speed_mps: float = 1.6  # target top speed at full thrust (m/s)
    thruster_x_m: float = 1.7  # CG -> thruster longitudinal distance, + = bow
    thruster_y_m: float = 0.0  # CG -> thruster lateral distance, + = starboard
    max_steer_angle_deg: float = 35.0  # max motor steer deflection (deg)

    # Added mass (entrained water). Surge is small; sway/yaw are large
    # because a hull must shove a lot of water sideways. Negative by the
    # Fossen sign convention. -------------------------------------------- #
    x_udot: float = -30.0  # added mass in surge
    y_vdot: float = -250.0  # added mass in sway
    n_rdot: float = -180.0  # added inertia in yaw
    y_rdot: float = -40.0  # sway/yaw added-mass coupling
    n_vdot: float = -40.0  # yaw/sway added-mass coupling

    # Sway / yaw linear damping (surge is derived in __post_init__). ----- #
    y_v: float = -260.0  # sway drag (large: hull resists sideways motion)
    n_r: float = -700.0  # yaw drag: sets the sustained turn rate
    y_r: float = -40.0  # sway/yaw damping coupling
    n_v: float = -40.0  # yaw/sway damping coupling

    # Quadratic damping (grows with speed^2). ---------------------------- #
    x_uu: float = -20.0
    y_vv: float = -180.0
    n_rr: float = -200.0

    # Hull character / tracking (directional stability). 1.0 = the tuned skiff
    # (default), ~0.35 = a flat-bottom jon boat (skittish, easily yawed, lots of
    # leeway), ~2.5 = a deep-V / keelboat (tracks straight, resists turning).
    # See __post_init__: at this default and the default L/B the multiplier is
    # exactly 1.0, so the boat is byte-identical to before this knob existed.
    hull_tracking: float = 1.0

    def __post_init__(self) -> None:
        # Yaw inertia from a uniform rectangle (Iz = m/12 * (L^2 + B^2)).
        self.iz: float = self.mass / 12.0 * (self.length**2 + self.beam**2)

        # --- Hull character / tracking (directional stability) ------------- #
        # A longer, narrower hull tracks better; a short, beamy one is loose.
        # The slenderness factor is normalised to the default L/B (4.1/1.7) so
        # it is exactly 1.0 there, and clamped to a sane band. ``hull_tracking``
        # (clamped) scales it: ~0.35 jon boat .. 1.0 skiff .. ~2.5 keel/deep-V.
        ht = min(3.0, max(0.25, self.hull_tracking))
        ref_slender = 4.1 / 1.7
        slender = min(1.6, max(0.7, (self.length / self.beam) / ref_slender))
        k = ht * slender
        # Scale the *directional* coefficients: yaw damping (sustained turn rate
        # + directional stability) and sway damping (lateral resistance vs
        # leeway). Higher k => slower turns, less leeway, straighter tracking.
        self.n_r *= k
        self.n_rr *= k
        self.y_v *= k
        self.y_vv *= k
        # Expose the realised multiplier for telemetry/tests/introspection.
        self.hull_k: float = k

        # Derive the surge *linear* drag so that, at top speed, the total
        # surge drag balances full thrust:
        #     max_thrust_n = (-x_u) * v_max + (-x_uu) * v_max^2
        # =>  -x_u = max_thrust_n / v_max - (-x_uu) * v_max
        v_max = self.max_speed_mps
        x_u_mag = self.max_thrust_n / v_max - (-self.x_uu) * v_max
        # Guard against a (mis)configured quadratic term overwhelming the
        # balance and producing a non-physical negative linear drag.
        self.x_u: float = -max(x_u_mag, 1.0)


class FossenBoat:
    """A 3-DOF surge-sway-yaw boat. Drop-in for :class:`~vanchor.sim.boat.Boat`."""

    def __init__(
        self, state: BoatState | None = None, params: FossenParams | None = None
    ) -> None:
        self.state = state or BoatState()
        self.params = params or FossenParams()
        # Body-frame velocity nu = [u (surge), v (sway), r (yaw rate)].
        self._nu = np.zeros(3, dtype=float)
        self._build_matrices()

    # ------------------------------------------------------------------ #
    # Matrix assembly
    # ------------------------------------------------------------------ #
    def _build_matrices(self) -> None:
        p = self.params

        # Rigid-body mass/inertia matrix (origin at the centre of gravity, so
        # the surge/sway and yaw blocks decouple).
        m_rb = np.diag([p.mass, p.mass, p.iz])

        # Added mass matrix.
        m_a = np.array(
            [
                [-p.x_udot, 0.0, 0.0],
                [0.0, -p.y_vdot, -p.y_rdot],
                [0.0, -p.n_vdot, -p.n_rdot],
            ],
            dtype=float,
        )

        self._mass_matrix = m_rb + m_a
        self._mass_inv = np.linalg.inv(self._mass_matrix)

        # Linear damping (positive-definite resistance, hence the negation of
        # the conventionally-negative coefficients).
        self._d_lin = -np.array(
            [
                [p.x_u, 0.0, 0.0],
                [0.0, p.y_v, p.y_r],
                [0.0, p.n_v, p.n_r],
            ],
            dtype=float,
        )

    def _damping(self, nu: np.ndarray) -> np.ndarray:
        """Total damping matrix at velocity ``nu`` (linear + quadratic)."""
        p = self.params
        u, v, r = nu
        d_quad = -np.diag(
            [
                p.x_uu * abs(u),
                p.y_vv * abs(v),
                p.n_rr * abs(r),
            ]
        )
        return self._d_lin + d_quad

    def _tau(self, command: MotorCommand) -> np.ndarray:
        """Generalized body force/moment ``[X, Y, N]`` from the command.

        A single steerable thruster mounted at longitudinal offset
        ``thruster_x_m`` from the CG. Thrust magnitude ``T`` is directed along
        the steered axis ``delta``; the yaw moment is the sway force times the
        mount lever arm. With a bow mount (``thruster_x_m > 0``) positive
        steering yields a positive (starboard) yaw moment. With zero thrust the
        force -- and therefore the yaw moment -- vanishes.
        """
        p = self.params
        cmd = command.clamped()
        # Trolling-motor props bite less in reverse than forward.
        eff = 1.0 if cmd.thrust >= 0 else p.reverse_efficiency
        thrust = cmd.thrust * eff * p.max_thrust_n
        delta = cmd.steering * math.radians(p.max_steer_angle_deg)
        fx = thrust * math.cos(delta)
        fy = thrust * math.sin(delta)
        # N = x*F_lat - y*F_fwd: the longitudinal arm turns sway into yaw, and a
        # lateral offset turns the forward thrust into a yaw bias -- so an
        # off-centre motor yaws the boat even at zero steering.
        yaw_moment = p.thruster_x_m * fy - p.thruster_y_m * fx
        return np.array([fx, fy, yaw_moment], dtype=float)

    # ------------------------------------------------------------------ #
    # Integration
    # ------------------------------------------------------------------ #
    def step(self, dt: float, command: MotorCommand, env: Environment) -> None:
        if dt <= 0:
            return
        s = self.state
        nu = self._nu

        # Solve M * nu_dot + D(nu) * nu = tau  ->  nu_dot.
        tau = self._tau(command)
        damping = self._damping(nu)
        nu_dot = self._mass_inv @ (tau - damping @ nu)

        # Semi-implicit Euler: advance the velocities first.
        nu = nu + nu_dot * dt
        self._nu = nu
        u, v, r = nu

        # Heading from the yaw rate (r is rad/s).
        s.heading_deg = normalize_deg(s.heading_deg + math.degrees(r) * dt)

        # Rotate the body velocity into NED. Heading 0 = north, +heading =
        # clockwise (toward east); +sway (v) is to starboard.
        h = math.radians(s.heading_deg)
        sin_h, cos_h = math.sin(h), math.cos(h)
        north = u * cos_h - v * sin_h
        east = u * sin_h + v * cos_h

        de, dn = env.drift_vector()
        s.ground_ve = east + de
        s.ground_vn = north + dn
        s.point = offset_meters(s.point, s.ground_ve * dt, s.ground_vn * dt)

        # Scalar forward speed exposed to the rest of the system (speed through
        # the water along the hull's velocity vector).
        s.speed_mps = math.hypot(u, v)
        s.timestamp += dt

    def teleport(self, point: GeoPoint, heading: float | None = None) -> None:
        """Instantly move ground truth to ``point`` (optionally set heading) and
        zero the body-frame velocities (surge/sway/yaw) so the boat stops dead
        instead of coasting from its pre-teleport momentum."""
        s = self.state
        s.point = point
        if heading is not None:
            s.heading_deg = normalize_deg(float(heading))
        self._nu = np.zeros(3, dtype=float)
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

    # ------------------------------------------------------------------ #
    # Introspection (handy for tests / telemetry)
    # ------------------------------------------------------------------ #
    @property
    def surge_mps(self) -> float:
        return float(self._nu[0])

    @property
    def sway_mps(self) -> float:
        return float(self._nu[1])

    @property
    def yaw_rate_dps(self) -> float:
        return math.degrees(float(self._nu[2]))
