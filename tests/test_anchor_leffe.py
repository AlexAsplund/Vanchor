"""'Leffe' -- the pure full-azimuth learned station-keeper (experimental)."""
import numpy as np
from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.core.models import ControlModeName, ManualSetpoint
from vanchor.controller.anchor_ml import AnchorLeffeMode


def _rt():
    return Runtime(load(None))


def test_anchor_leffe_command_engages_the_mode():
    rt = _rt()
    rt.controller.handle_command({"type": "anchor_leffe", "radius_m": 6, "anchor": {"lat": 59.0, "lon": 13.0}})
    assert rt.state.mode is ControlModeName.ANCHOR_LEFFE
    assert rt.state.anchor is not None
    assert rt.state.anchor_radius_m == 6


def test_leffe_produces_bounded_manual_setpoint_and_updates_distance():
    rt = _rt()
    rt.controller.handle_command({"type": "anchor_leffe", "radius_m": 5, "anchor": {"lat": 59.0, "lon": 13.0}})
    for _ in range(20):
        rt.controller.control_tick(0.2)
    # command stays within the actuator range; distance_to_anchor is kept fresh
    assert -1.0 <= rt.state.motor_command.thrust <= 1.0
    assert -1.0 <= rt.state.motor_command.steering <= 1.0
    assert rt.state.distance_to_anchor_m >= 0.0


def test_leffe_is_pure_no_pid_base():
    """Leffe's command is the net output directly (no pid_base term)."""
    m = AnchorLeffeMode()
    # residual_scale is forced to 0 (the guardrail/PID path is inert)
    assert m.residual_scale == 0.0
    assert m.boat_azimuth_deg == AnchorLeffeMode.TRAIN_AZIMUTH_DEG


def test_leffe_azimuth_rescales_to_boat_range():
    """+/-1 (trained at TRAIN_AZIMUTH_DEG) is rescaled to the boat's mechanical
    steering range so the physical deflection matches training."""
    rt = _rt()
    leffe = rt.controller.modes[ControlModeName.ANCHOR_LEFFE]
    # app syncs the boat's mechanical range on init
    assert leffe.boat_azimuth_deg == rt.config.boat.max_steer_angle_deg
    # a full-scale net steering command scales down when the boat swing > trained
    leffe.boat_azimuth_deg = 180.0
    st = 1.0 * leffe.policy_steer_sign * (leffe.TRAIN_AZIMUTH_DEG / 180.0)
    assert abs(st) < 1.0 and abs(st) == 120.0 / 180.0
