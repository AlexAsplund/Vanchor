"""Tests for the hull-character / tracking knob (directional stability).

Low ``hull_tracking`` (a jon boat) should turn faster for the same steering and
hold heading worse under a beam disturbance; high ``hull_tracking`` (a keel /
deep-V) should turn slower and track straighter. At ``hull_tracking == 1.0`` and
the default L/B the multiplier ``k`` is exactly 1.0, so the boat is identical to
before the knob existed.
"""

from __future__ import annotations

import math

from vanchor.core.config import BoatConfig
from vanchor.core.geo import normalize_deg
from vanchor.core.models import BoatState, Environment, GeoPoint, MotorCommand
from vanchor.sim.fossen import FossenBoat, FossenParams

HERE = GeoPoint(59.3293, 18.0686)


def _run(boat: FossenBoat, command: MotorCommand, env: Environment, seconds: float, dt: float = 0.05):
    for _ in range(int(seconds / dt)):
        boat.step(dt, command, env)


def _sustained_yaw_rate(hull_tracking: float) -> float:
    """Settled yaw rate (deg/s) at full thrust + full steering."""
    boat = FossenBoat(
        BoatState(point=HERE, heading_deg=0.0),
        FossenParams(hull_tracking=hull_tracking),
    )
    _run(boat, MotorCommand(thrust=1.0, steering=0.0), Environment(), seconds=5.0)
    _run(boat, MotorCommand(thrust=1.0, steering=1.0), Environment(), seconds=8.0)
    return abs(boat.yaw_rate_dps)


# --------------------------------------------------------------------------- #
# k multiplier: the no-op invariant
# --------------------------------------------------------------------------- #
def test_default_is_a_noop_byte_identical():
    """hull_tracking=1.0 at the default L/B => k == 1.0 and every scaled
    coefficient is exactly the un-knobbed value."""
    base = FossenParams()  # default hull_tracking=1.0
    explicit = FossenParams(hull_tracking=1.0)
    assert base.hull_k == 1.0
    assert explicit.hull_k == 1.0
    # The directional coefficients match the documented defaults exactly.
    assert base.n_r == -700.0
    assert base.n_rr == -200.0
    assert base.y_v == -260.0
    assert base.y_vv == -180.0


def test_k_scales_directional_coeffs_only():
    """k multiplies n_r, n_rr, y_v, y_vv but leaves surge/coupling alone."""
    p = FossenParams(hull_tracking=2.0)  # default L/B -> k == 2.0
    assert p.hull_k == 2.0
    assert p.n_r == -1400.0
    assert p.n_rr == -400.0
    assert p.y_v == -520.0
    assert p.y_vv == -360.0
    # Coupling + surge quadratic are untouched.
    assert p.y_r == -40.0
    assert p.n_v == -40.0
    assert p.x_uu == -20.0


def test_k_includes_slenderness_and_clamps():
    """k = hull_tracking * clamp((L/B)/(4.1/1.7), 0.7, 1.6)."""
    ref = 4.1 / 1.7
    # Long & narrow: slenderness pushes k above hull_tracking (until clamp).
    long_narrow = FossenParams(length=8.0, beam=1.5, hull_tracking=1.0)
    expected = min(1.6, max(0.7, (8.0 / 1.5) / ref))
    assert math.isclose(long_narrow.hull_k, expected)
    assert long_narrow.hull_k > 1.0
    # hull_tracking itself is clamped to [0.25, 3.0] (default L/B -> slender=1.0).
    assert FossenParams(hull_tracking=10.0).hull_k == 3.0
    assert FossenParams(hull_tracking=0.0).hull_k == 0.25


# --------------------------------------------------------------------------- #
# Behaviour: turn rate
# --------------------------------------------------------------------------- #
def test_low_tracking_turns_faster_than_high():
    jon = _sustained_yaw_rate(0.35)
    skiff = _sustained_yaw_rate(1.0)
    keel = _sustained_yaw_rate(2.5)
    # Monotonic: looser hull -> snappier turn.
    assert jon > skiff > keel
    # And the difference is meaningful, not noise.
    assert jon > skiff * 1.3
    assert keel < skiff * 0.85


# --------------------------------------------------------------------------- #
# Behaviour: leeway / sideslip in a turn
# --------------------------------------------------------------------------- #
def test_low_tracking_has_more_leeway_in_a_turn():
    def sway_in_turn(ht: float) -> float:
        boat = FossenBoat(
            BoatState(point=HERE, heading_deg=0.0), FossenParams(hull_tracking=ht)
        )
        _run(boat, MotorCommand(thrust=1.0, steering=0.0), Environment(), seconds=5.0)
        _run(boat, MotorCommand(thrust=1.0, steering=1.0), Environment(), seconds=4.0)
        return abs(boat.sway_mps)

    assert sway_in_turn(0.35) > sway_in_turn(2.5)


# --------------------------------------------------------------------------- #
# Behaviour: heading hold under a beam disturbance (open loop)
# --------------------------------------------------------------------------- #
def test_low_tracking_knocked_off_heading_more_by_a_beam_kick():
    """Motor straight ahead, then take a sideways "kick" (a gust/wake hitting
    the beam: an injected sway + yaw perturbation) and let the hull react with
    no steering correction. The yaw damping resists the kick, so a directionally
    stable keel is knocked off heading less than a loose jon boat."""

    def heading_after_kick(ht: float) -> float:
        boat = FossenBoat(
            BoatState(point=HERE, heading_deg=0.0), FossenParams(hull_tracking=ht)
        )
        cmd = MotorCommand(thrust=1.0, steering=0.0)
        _run(boat, cmd, Environment(), seconds=6.0)
        # Beam kick: a burst of sway velocity + a little yaw rate, as a side
        # gust/wave would impart. Then run straight open-loop and see how far
        # the heading swings.
        boat._nu[1] += 0.8   # sway (m/s)
        boat._nu[2] += 0.25  # yaw rate (rad/s)
        _run(boat, cmd, Environment(), seconds=8.0)
        # Angular deviation from the original heading (0), in [0, 180]. (Use the
        # symmetric wrap, not abs(normalize_deg): a hull that holds heading and
        # settles a hair negative would otherwise read as ~360, not ~0.)
        return abs((boat.state.heading_deg + 180.0) % 360.0 - 180.0)

    loose = heading_after_kick(0.35)
    tight = heading_after_kick(2.5)
    # The keel holds its heading better than the jon boat.
    assert loose > tight
    assert loose > tight * 1.2


# --------------------------------------------------------------------------- #
# Wiring: BoatConfig + live rebuild through the runtime
# --------------------------------------------------------------------------- #
def test_boatconfig_has_hull_tracking_default():
    assert BoatConfig().hull_tracking == 1.0


def test_runtime_applies_hull_tracking_live():
    """POST /api/boat -> update_boat -> _apply_boat_specs rebuilds the physics
    with the new hull_tracking, changing the live turn rate."""
    from vanchor.app import Runtime
    from vanchor.core.config import AppConfig

    rt = Runtime(AppConfig())
    try:
        sim = rt.simulator
        assert sim is not None

        def measure() -> float:
            sim.boat.teleport(HERE, heading=0.0)
            env = Environment()
            for _ in range(100):
                sim.boat.step(0.05, MotorCommand(thrust=1.0, steering=0.0), env)
            for _ in range(200):
                sim.boat.step(0.05, MotorCommand(thrust=1.0, steering=1.0), env)
            return abs(sim.boat.yaw_rate_dps)

        rt.update_boat({"hull_tracking": 0.35})
        assert rt.config.boat.hull_tracking == 0.35
        assert sim.boat.params.hull_tracking == 0.35
        loose_rate = measure()

        rt.update_boat({"hull_tracking": 2.5})
        assert sim.boat.params.hull_tracking == 2.5
        tight_rate = measure()

        assert loose_rate > tight_rate

        # The telemetry/profile surfaces it.
        assert rt.boat_profile()["hull_tracking"] == 2.5
    finally:
        if rt.simulator is not None:
            rt.simulator.stop()


def test_jon_boat_preset_tracks_looser_than_outboard():
    """The seeded presets (#89) span a real tracking range."""
    from vanchor.core.boat_profiles import _PRESETS

    presets = dict(_PRESETS)
    assert presets["Jon boat (flat-bottom)"]["hull_tracking"] < 1.0
    assert presets["15 HP stern outboard"]["hull_tracking"] > 1.0


def test_hull_tracking_biases_autopilot_tuning():
    """Hull character also tunes the CONTROLLER (works on real hardware, not just
    sim physics): a stiff/tracking hull gets MORE steering authority and LESS
    command smoothing; a loose/skittish hull the reverse. At 1.0 it's a no-op."""
    from vanchor.app import Runtime

    rt = Runtime()
    base_tau = rt.config.control.steer_tau

    rt._apply_boat_specs({"hull_tracking": 1.0})
    auth_default = rt.controller.helm.autopilot_steer_scale
    assert rt.controller.helm.steer_tau == base_tau  # default: no-op

    rt._apply_boat_specs({"hull_tracking": 0.35})  # jon boat
    auth_lo = rt.controller.helm.autopilot_steer_scale
    tau_lo = rt.controller.helm.steer_tau

    rt._apply_boat_specs({"hull_tracking": 2.5})  # keelboat
    auth_hi = rt.controller.helm.autopilot_steer_scale
    tau_hi = rt.controller.helm.steer_tau

    # Authority rises with tracking; smoothing (tau) falls with tracking.
    assert auth_lo < auth_default < auth_hi
    assert tau_lo > base_tau > tau_hi
