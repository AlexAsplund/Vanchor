"""Lateral thruster offset: off-centre yaw physics + feed-forward compensation.

A trolling motor mounted off the boat's centreline yaws the boat under straight
forward thrust (yaw moment ``N = x*F_lat - y*F_fwd``). These tests cover:

  * the new ``thruster_y_m`` / feed-forward geometry on ``BoatConfig``,
  * the Fossen yaw-moment term (off-centre motor visibly yaws),
  * the helm feed-forward holding heading where it otherwise wouldn't,
  * the calibration two-point trim estimate,
  * the config / telemetry / ``POST /api/boat`` round-trip.
"""

import math

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.controller.calibration import _ff_trim_delta, _yaw_drift_rate
from vanchor.controller.controller import Helm
from vanchor.core.config import AppConfig, BoatConfig
from vanchor.core.models import BoatState, Environment, GeoPoint, MotorCommand
from vanchor.core.pid import PID
from vanchor.sim.fossen import FossenBoat, FossenParams
from vanchor.ui.server import create_app

HERE = GeoPoint(59.3293, 18.0686)


# --------------------------------------------------------------------------- #
# Config geometry
# --------------------------------------------------------------------------- #
def test_thruster_y_default_is_zero_and_centred_ff_is_zero():
    b = BoatConfig()
    assert b.thruster_y_m == 0.0
    assert b.thrust_yaw_ff_angle() == 0.0  # centred -> no feed-forward


def test_ff_angle_cancels_geometry():
    # delta_ff = atan2(y, |x|): the deflection where x*sin = y*cos.
    b = BoatConfig(thruster_offset_m=1.7, thruster_y_m=0.3)
    a = b.thrust_yaw_ff_angle()
    assert a == pytest.approx(math.atan2(0.3, 1.7))
    x, y = 1.7, 0.3
    assert x * math.sin(a) == pytest.approx(y * math.cos(a))


def test_ff_angle_uses_magnitude_of_x_for_stern():
    # A stern mount (x<0) needs the same magnitude FF angle; steer_sign flips it.
    bow = BoatConfig(thruster_offset_m=1.7, thruster_y_m=0.3).thrust_yaw_ff_angle()
    stern = BoatConfig(thruster_offset_m=-1.7, thruster_y_m=0.3).thrust_yaw_ff_angle()
    assert bow == pytest.approx(stern)


def test_ff_override_and_trim():
    b = BoatConfig(thruster_y_m=0.3, thrust_yaw_ff=0.1, thrust_yaw_ff_trim=0.02)
    assert b.thrust_yaw_ff_angle() == pytest.approx(0.12)  # override + trim
    # Without an override, the trim still adds onto the derived angle.
    b2 = BoatConfig(thruster_offset_m=1.7, thruster_y_m=0.3, thrust_yaw_ff_trim=0.05)
    assert b2.thrust_yaw_ff_angle() == pytest.approx(math.atan2(0.3, 1.7) + 0.05)


# --------------------------------------------------------------------------- #
# Fossen physics
# --------------------------------------------------------------------------- #
def _run(boat, command, env, seconds, dt=0.05):
    for _ in range(int(seconds / dt)):
        boat.step(dt, command, env)


def test_offcentre_thruster_yaws_under_straight_thrust():
    centred = FossenBoat(BoatState(point=HERE, heading_deg=0.0), FossenParams(thruster_y_m=0.0))
    offset = FossenBoat(BoatState(point=HERE, heading_deg=0.0), FossenParams(thruster_y_m=0.4))
    _run(centred, MotorCommand(thrust=1.0, steering=0.0), Environment(), 12.0)
    _run(offset, MotorCommand(thrust=1.0, steering=0.0), Environment(), 12.0)
    # Centred motor holds heading; a starboard-offset motor yaws to port
    # (N = -y*F_fwd < 0 -> heading decreases).
    assert abs(centred.state.heading_deg - 0.0) < 1.0
    assert offset.yaw_rate_dps < -1.0


def test_yaw_sign_flips_with_offset_side():
    stbd = FossenBoat(BoatState(point=HERE), FossenParams(thruster_y_m=0.4))
    port = FossenBoat(BoatState(point=HERE), FossenParams(thruster_y_m=-0.4))
    _run(stbd, MotorCommand(thrust=1.0), Environment(), 8.0)
    _run(port, MotorCommand(thrust=1.0), Environment(), 8.0)
    assert stbd.yaw_rate_dps < 0.0 < port.yaw_rate_dps


# --------------------------------------------------------------------------- #
# Helm feed-forward (closed-loop, via a configured harness)
# --------------------------------------------------------------------------- #
def _harness(ff_norm: float, y: float, *, x: float = 1.7):
    from tests.harness import Harness

    h = Harness(model="fossen")
    h.sim.boat.params = FossenParams(thruster_x_m=x, thruster_y_m=y, max_steer_angle_deg=180.0)
    h.sim.boat._build_matrices()
    h.controller.helm = Helm(
        PID(kp=0.035, ki=0.0, kd=0.012, output_min=-1.0, output_max=1.0),
        steer_tau=0.6,
        autopilot_steer_scale=35.0 / 180.0,
        steer_sign=1.0,
        thrust_yaw_ff=ff_norm,
    )
    return h


def _ff_norm(y: float, x: float = 1.7) -> float:
    return math.atan2(y, abs(x)) / math.radians(180.0)


def test_feedforward_holds_heading_in_guided_mode():
    y = 0.35
    no_ff = _harness(0.0, y)
    no_ff.command({"type": "heading_hold", "heading": 0.0, "throttle": 1.0})
    no_ff.run(40.0)

    with_ff = _harness(_ff_norm(y), y)
    with_ff.command({"type": "heading_hold", "heading": 0.0, "throttle": 1.0})
    with_ff.run(40.0)

    err_no = abs(((no_ff.sim.truth().heading_deg + 180) % 360) - 180)
    err_yes = abs(((with_ff.sim.truth().heading_deg + 180) % 360) - 180)
    # The feed-forward removes the steady-state heading error the PD helm leaves.
    assert err_yes < 2.0
    assert err_yes < err_no - 3.0


def test_feedforward_holds_heading_open_loop_manual():
    """With the geometric FF + no closed loop (manual, centred steering), an
    off-centre motor still tracks far straighter than with no FF."""
    y = 0.3
    no_ff = _harness(0.0, y)
    no_ff.command({"type": "manual", "thrust": 1.0, "steering": 0.0})
    no_ff.run(20.0)

    with_ff = _harness(_ff_norm(y), y)
    with_ff.command({"type": "manual", "thrust": 1.0, "steering": 0.0})
    with_ff.run(20.0)

    err_no = abs(((no_ff.sim.truth().heading_deg + 180) % 360) - 180)
    err_yes = abs(((with_ff.sim.truth().heading_deg + 180) % 360) - 180)
    assert err_yes < err_no * 0.5


def test_feedforward_is_zero_when_not_making_way():
    """No thrust -> no FF deflection (a stopped prop can't vector the boat)."""
    helm = Helm(thrust_yaw_ff=0.1, steer_sign=1.0)
    from vanchor.core.models import ManualSetpoint
    from vanchor.core.state import NavigationState

    cmd = helm.compute(ManualSetpoint(thrust=0.0, steering=0.0), NavigationState(), 0.2)
    assert cmd.steering == 0.0


# --------------------------------------------------------------------------- #
# Calibration trim estimate
# --------------------------------------------------------------------------- #
def test_yaw_drift_rate_signed():
    headings = [(i * 0.1, -i * 0.5) for i in range(20)]  # steadily decreasing
    assert _yaw_drift_rate(headings) < 0.0


def test_ff_trim_delta_nulls_residual_drift():
    # FF angle of 0.1 rad reduced drift from -6 dps to -0.5 dps -> gain 55 dps/rad.
    # Extra angle to null -0.5 dps is +0.009 rad (same sign convention as FF).
    delta = _ff_trim_delta(drift_off_dps=-6.0, drift_on_dps=-0.5, ff_angle_rad=0.1)
    assert delta == pytest.approx(0.5 / 55.0, abs=1e-3)


def test_ff_trim_delta_handles_no_signal():
    assert _ff_trim_delta(0.0, 0.0, 0.0) == 0.0
    assert _ff_trim_delta(-1.0, -1.0, 0.1) == 0.0  # no drift change -> no gain


def test_ff_trim_delta_is_clamped():
    # A near-zero gain would otherwise demand an enormous correction.
    delta = _ff_trim_delta(drift_off_dps=-5.0, drift_on_dps=-4.999, ff_angle_rad=0.1)
    assert abs(delta) <= math.radians(30.0) + 1e-9


# --------------------------------------------------------------------------- #
# Config / telemetry / API round-trip
# --------------------------------------------------------------------------- #
def test_config_round_trip():
    cfg = AppConfig.from_dict({"boat": {"thruster_y_m": 0.42, "thrust_yaw_ff_trim": 0.03}})
    assert cfg.boat.thruster_y_m == 0.42
    assert cfg.boat.thrust_yaw_ff_trim == 0.03


def test_boat_profile_telemetry_exposes_offset(tmp_path):
    # The lateral thruster offset is exposed in the boat telemetry and survives a
    # live update (write-through into the active named profile, #75/#89).
    rt = Runtime(AppConfig.from_dict({"data_dir": str(tmp_path)}))
    rt.update_boat({"thruster_y_m": 0.25})
    prof = rt.boat_profile()
    assert prof["thruster_y_m"] == 0.25
    assert "thrust_yaw_ff" in prof and "thrust_yaw_ff_trim" in prof
    rt.update_boat({"thruster_y_m": 0.42})
    assert rt.boat_profile()["thruster_y_m"] == 0.42


def test_post_api_boat_updates_offset_and_ff_live(tmp_path):
    rt = Runtime(AppConfig.from_dict({"data_dir": str(tmp_path)}))
    app = create_app(rt)
    with TestClient(app) as c:
        body = c.post("/api/boat", json={"thruster_y_m": 0.3}).json()
        assert body["thruster_y_m"] == 0.3
        # The helm's live feed-forward picked up the new geometry.
        expect = rt.config.boat.thrust_yaw_ff_angle() / math.radians(
            rt.config.boat.max_steer_angle_deg
        )
        assert rt.controller.helm.thrust_yaw_ff == pytest.approx(expect)
        assert rt.controller.helm.thrust_yaw_ff != 0.0
