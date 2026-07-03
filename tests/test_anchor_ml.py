"""The hybrid learned virtual-anchor mode (``anchor_ml``) + its controller wiring.

``anchor_ml`` is now a hybrid: command = clip(pid_base + 0.3*residual). The base
guarantees robustness (deadband idle, reverse-when-astern); the bounded residual
only tightens the hold."""

from vanchor.controller.anchor_ml import AnchorMLMode, pid_base
from vanchor.core.models import (
    ControlModeName,
    Environment,
    GeoPoint,
    GpsFix,
    ManualSetpoint,
)
from vanchor.core.state import NavigationState

from .harness import Harness


def test_model_loads_and_is_tiny():
    m = AnchorMLMode()
    assert m._mlp.sizes[-1] == 2          # outputs thrust + steering
    assert m._mlp.sizes[0] % 8 == 0       # input is whole obs frames
    assert m.history == m._mlp.sizes[0] // 8 >= 1


def test_update_maps_state_to_clamped_manual_setpoint():
    m = AnchorMLMode()
    st = NavigationState()
    st.fix = GpsFix(point=GeoPoint(59.0, 18.0), sog_knots=0.6, cog_deg=120.0)
    st.anchor = GeoPoint(59.0001, 18.0001)
    st.heading_deg = 30.0
    sp = m.update(st, 0.2)
    assert isinstance(sp, ManualSetpoint)
    assert -1.0 <= sp.thrust <= 1.0
    assert -1.0 <= sp.steering <= 1.0


def test_anchor_ml_registered_and_engages():
    h = Harness(model="fossen")
    assert ControlModeName.ANCHOR_ML in h.controller.modes
    h.command({"type": "anchor_ml", "radius_m": 6.0})
    assert h.controller.state.mode == ControlModeName.ANCHOR_ML
    assert h.controller.state.anchor is not None


def test_anchor_ml_holds_station_under_wind_and_current():
    env = Environment(current_speed=0.3, current_dir=90.0, wind_speed=4.0, wind_dir=120.0)
    h = Harness(model="fossen", environment=env)
    h.command({"type": "anchor_ml", "radius_m": 6.0})
    distances = h.run(seconds=200)
    settled = distances[-150:]
    assert max(settled) < 6.0                      # stays within the watch circle
    assert sum(settled) / len(settled) < 3.0       # holds tight, not just inside


def test_anchor_ml_falls_back_to_pid_when_model_unavailable():
    h = Harness(model="fossen")
    del h.controller.modes[ControlModeName.ANCHOR_ML]   # simulate a missing model
    h.command({"type": "anchor_ml"})
    assert h.controller.state.mode == ControlModeName.ANCHOR_HOLD


def test_pid_base_idles_in_deadband_and_reverses_when_astern():
    assert pid_base(0.5, 0.0, 0.0, 0.0) == (0.0, 0.0)   # inside deadband -> idle
    th_ahead, _ = pid_base(5.0, 0.0, 0.0, 0.0)          # mark ahead -> forward
    th_astern, _ = pid_base(-5.0, 0.0, 0.0, 0.0)        # mark astern -> reverse
    assert th_ahead > 0.0
    assert th_astern < 0.0


def test_hybrid_command_stays_near_the_pid_base():
    """The residual is bounded (+-0.3), so the command can never run away from
    the robust base -- the worst case is just the PID."""
    m = AnchorMLMode()
    st = NavigationState()
    # Evaluate the residual bound in the policy's own (trained) steering frame:
    # set the boat range to the trained azimuth so the deploy-time rescale is
    # identity. On the real 180° boat the rescale only shrinks steering further
    # (0.67x), so the command stays at least this close to the PID base.
    st.max_steer_angle_deg = m.train_azimuth_deg or st.max_steer_angle_deg
    st.fix = GpsFix(point=GeoPoint(59.001, 18.001), sog_knots=0.4, cog_deg=90.0)
    st.anchor = GeoPoint(59.0, 18.0)
    st.heading_deg = 20.0
    sp = m.update(st, 0.2)
    frame = m._frame(st, 0.2)
    base_th, base_st = pid_base(frame[0] * 10, frame[1] * 10, frame[2] * 1.5, frame[3] * 1.5)
    assert abs(sp.thrust - base_th) <= m.residual_scale + 1e-6
    assert abs(sp.steering - base_st) <= m.residual_scale + 1e-6


def test_hybrid_holds_from_rest_no_driveoff():
    """The live v2 failure: engaged from rest it drove off. The hybrid holds."""
    h = Harness(model="fossen")          # boat starts at rest
    h.command({"type": "anchor_ml", "radius_m": 6.0})
    distances = h.run(seconds=120)
    settled = distances[-90:]
    assert max(settled) < 6.0            # stays in the watch circle (not 21 m)
    assert sum(settled) / len(settled) < 3.0
