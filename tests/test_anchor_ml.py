"""The learned virtual-anchor mode (``anchor_ml``) + its controller wiring."""

from vanchor.controller.anchor_ml import AnchorMLMode
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
