"""A higher-fidelity 3-DOF (surge-sway-yaw) maneuvering boat model.

This is a drop-in alternative to :class:`vanchor.sim.boat.Boat`. It exposes the
same interface (``state``, ``step``, ``truth``) but instead of a first-order
speed lag plus a kinematic yaw rate it integrates a proper rigid-body +
hydrodynamic model in body-fixed coordinates ``[u, v, r]`` (surge, sway, yaw
rate).

The structure is inspired by the MIT-licensed Fossen "otter" USV from
``cybergalactic/PythonVehicleSimulator``:

    M * nu_dot + C(nu_r) * nu_r + (D_lin + D_quad(nu_r)) * nu_r = tau

where ``nu = [u, v, r]`` are body velocities, ``M = M_rb + M_a`` is the rigid-body
plus added mass, ``C(nu)`` is the Coriolis-centripetal matrix (the body-frame /
rotating-hull coupling + added-mass Munk moment -- NOT the negligible planetary
Earth-rotation Coriolis effect), ``D_lin`` / ``D_quad`` are linear and quadratic
damping, and ``tau`` is the generalized
force/moment from the thruster plus the aerodynamic wind force. The hydrodynamic
terms act on the velocity **through the water** ``nu_r = nu - nu_c`` (so a current
advects the hull and is felt as drag), while the kinematics integrate the
absolute velocity ``nu``. Wind enters ``tau`` as a quadratic aerodynamic force /
yaw moment (not a fixed leeway), so leeway and weathervaning emerge from the
force balance.

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
update heading by ``r*dt`` and the NED position from the absolute body velocity
rotated into the local tangent plane (current/wind already live inside ``nu``).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np

from ..core.geo import normalize_deg, offset_meters
from ..core.models import BoatState, Environment, GeoPoint, MotorCommand

logger = logging.getLogger("vanchor.sim")

# Aerodynamic wind-force constants (Fossen Handbook ch. 10 / OCIMF / Isherwood).
# A small-craft approximation: C_X≈cx·cos γ, C_Y≈cy·sin γ, C_N≈cn·sin 2γ on the
# apparent (relative) wind. See FossenBoat._tau_wind.
RHO_AIR = 1.225  # kg/m^3
WIND_CX = 0.6
WIND_CY = 0.9
WIND_CN = 0.1


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

        # Above-water windage areas for the aerodynamic wind force, derived from
        # geometry assuming a low skiff freeboard (~0.35 m frontal, ~0.45 m
        # lateral profile). Defaults: A_F≈0.6 m^2, A_L≈1.85 m^2 for the 4.1 m
        # boat -- matching typical small-craft wind-tunnel reference areas.
        self.area_front: float = self.beam * 0.35
        self.area_lateral: float = self.length * 0.45


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

        # Added mass must be symmetric (Fossen: M_A = M_Aᵀ for a port/starboard
        # symmetric hull). Enforce it so a user setting y_rdot ≠ n_vdot can't make
        # the model physically inconsistent (and so C(ν) via m2c stays exact).
        m_a = 0.5 * (m_a + m_a.T)

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

    def _coriolis(self, nu: np.ndarray) -> np.ndarray:
        """Coriolis-centripetal matrix C(ν) = C_RB + C_A.

        NOT the planetary (Earth-rotation) Coriolis effect -- that is ~0.06 N for
        this boat and is correctly ignored. This is the *body-frame* Coriolis-
        centripetal coupling: the fictitious-force terms that arise from writing
        the dynamics in the hull-fixed frame (which rotates as the boat yaws),
        plus the added-mass "Munk moment" (C_A). It scales with the boat's OWN
        motion (u·r, v·r), so it matters precisely when the boat is turning --
        regardless of vessel size.

        Computed by Fossen's ``m2c`` 3-DOF formula on the full (symmetric) mass
        matrix -- the same term the otter USV uses. Skew-symmetric (νᵀC ν ≡ 0).
        This is the centripetal sway/yaw coupling that makes the boat crab
        *into* a turn; omitting it (as before) inverted the crab and overstated
        sway by ~4× in hard turns.
        """
        m = self._mass_matrix
        u, v, r = nu
        c02 = -m[1, 1] * v - m[1, 2] * r
        c12 = m[0, 0] * u
        return np.array(
            [[0.0, 0.0, c02], [0.0, 0.0, c12], [-c02, -c12, 0.0]], dtype=float
        )

    def _current_body(self, env: Environment, heading_deg: float) -> np.ndarray:
        """The water current expressed in the body frame ``[u_c, v_c, 0]``.

        The hull is advected by the water, so the hydrodynamic forces act on the
        velocity *relative to the water* ν_r = ν − ν_c (not the ground velocity).
        """
        if env.current_speed == 0.0:
            return np.zeros(3, dtype=float)
        ce = env.current_speed * math.sin(math.radians(env.current_dir))
        cn = env.current_speed * math.cos(math.radians(env.current_dir))
        h = math.radians(heading_deg)
        u_c = cn * math.cos(h) + ce * math.sin(h)
        v_c = -cn * math.sin(h) + ce * math.cos(h)
        return np.array([u_c, v_c, 0.0], dtype=float)

    def _tau_wind(self, env: Environment, nu: np.ndarray, heading_deg: float) -> np.ndarray:
        """Aerodynamic wind force/moment ``[X, Y, N]`` on the above-water hull.

        Quadratic in the *apparent* (relative) wind: τ_wind = ½ρ_air·C(γ)·A·V_rw²
        (Fossen ch. 10 / OCIMF). Unlike a fixed leeway fraction this is
        heading-dependent, produces a yaw moment (weathervaning -- the dominant
        disturbance a heading/anchor-hold autopilot must reject), grows with V²,
        and exerts a real force the thruster must fight. Leeway then *emerges*
        from this sway force balanced against the hull's sway damping.
        """
        p = self.params
        vw = env.wind_speed
        if vw <= 0.0:
            return np.zeros(3, dtype=float)
        psi = math.radians(heading_deg)
        bw = math.radians(env.wind_dir)  # "toward which the wind pushes"
        wu = vw * math.cos(bw - psi)  # wind velocity in the body frame
        wv = vw * math.sin(bw - psi)
        # Apparent wind = wind velocity minus the boat's own motion. The drag
        # force acts ALONG it (downwind), so gw is its body-frame angle (no sign
        # flip -- the cross-checked beam-on magnitude is ½ρ·cy·A_L·V² ≈ 54 N at
        # 7 m/s, and a beam wind gives zero yaw).
        aw_u = wu - nu[0]
        aw_v = wv - nu[1]
        vrw = math.hypot(aw_u, aw_v)
        gw = math.atan2(aw_v, aw_u)
        q = 0.5 * RHO_AIR * vrw * vrw
        x = q * WIND_CX * math.cos(gw) * p.area_front
        y = q * WIND_CY * math.sin(gw) * p.area_lateral
        n = q * WIND_CN * math.sin(2.0 * gw) * p.area_lateral * p.length
        return np.array([x, y, n], dtype=float)

    # ------------------------------------------------------------------ #
    # Integration
    # ------------------------------------------------------------------ #
    def step(self, dt: float, command: MotorCommand, env: Environment) -> None:
        if dt <= 0:
            return
        s = self.state
        nu = self._nu

        # Current advects the hull: the hydrodynamic forces (Coriolis + damping)
        # act on the velocity through the WATER, nu_r = nu - nu_c, not the ground
        # velocity. Wind enters as an aerodynamic force on tau. Position then
        # integrates the ABSOLUTE velocity nu, so the boat genuinely drifts with
        # the water (nu relaxes toward nu_c under drag) -- no kinematic offset.
        nu_c = self._current_body(env, s.heading_deg)
        nu_r = nu - nu_c

        # Solve M*nu_dot + C(nu_r)*nu_r + D(nu_r)*nu_r = tau_thrust + tau_wind.
        tau = self._tau(command) + self._tau_wind(env, nu, s.heading_deg)
        cor = self._coriolis(nu_r)
        damping = self._damping(nu_r)
        nu_dot = self._mass_inv @ (tau - cor @ nu_r - damping @ nu_r)

        # Semi-implicit Euler: advance the velocities first.
        nu = nu + nu_dot * dt
        self._nu = nu
        u, v, r = nu

        # Heading from the yaw rate (r is rad/s).
        s.heading_deg = normalize_deg(s.heading_deg + math.degrees(r) * dt)

        # Rotate the absolute body velocity into NED -> velocity over ground.
        # Heading 0 = north, +heading = clockwise (toward east); +sway (v) is to
        # starboard. Current/wind already live inside nu, so nothing is added.
        h = math.radians(s.heading_deg)
        sin_h, cos_h = math.sin(h), math.cos(h)
        north = u * cos_h - v * sin_h
        east = u * sin_h + v * cos_h
        s.ground_ve = east
        s.ground_vn = north
        s.point = offset_meters(s.point, east * dt, north * dt)

        # Forward speed THROUGH THE WATER (|nu_r|) for the rest of the system;
        # speed-over-ground = hypot(ground_ve, ground_vn) and differs when a
        # current flows.
        s.speed_mps = math.hypot(u - nu_c[0], v - nu_c[1])
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
