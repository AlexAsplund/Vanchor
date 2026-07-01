"""Sensor-staleness detection + health telemetry (review H3/M14).

Covers:
  * the navigator stamping monotonic receive-times for each primary input,
  * the controller feeding those ages to the governor so a stale compass in a
    guided mode coasts (and recovers), and
  * the ``health`` telemetry block's shape (per-sensor ages, controller_fault,
    tick age, staleness flags), driven off ``Runtime.telemetry()`` directly (a
    full ``TestClient(Runtime())`` can hang on depth data -- so we don't use it).
"""

from __future__ import annotations

import pytest

from vanchor.app import Runtime
from vanchor.controller.controller import Controller
from vanchor.core.config import AppConfig
from vanchor.core.models import ControlModeName, GeoPoint, MotorCommand
from vanchor.core.state import NavigationState
from vanchor.nav import nmea
from vanchor.nav.navigator import Navigator


class _FakeMotor:
    """Minimal MotorController stand-in: records the last applied command."""

    def __init__(self) -> None:
        self.last = MotorCommand()

    def apply(self, command: MotorCommand) -> None:
        self.last = command


# --------------------------------------------------------------------------- #
# 1. Navigator stamps receive-times off an injectable monotonic clock.
# --------------------------------------------------------------------------- #
def test_navigator_stamps_receive_times():
    clock = [1000.0]
    state = NavigationState()
    nav = Navigator(state, bus=None, mono_fn=lambda: clock[0])

    # Nothing received yet -> all stamps are None.
    assert state.fix_received_mono is None
    assert state.heading_received_mono is None
    assert state.depth_received_mono is None

    nav.handle_sentence(nmea.encode_rmc(GeoPoint(59.0, 18.0), sog_knots=1.0, cog_deg=90))
    assert state.fix_received_mono == 1000.0

    clock[0] = 1001.0
    nav.handle_sentence(nmea.encode_hdm(90.0))
    assert state.heading_received_mono == 1001.0

    clock[0] = 1002.0
    nav.handle_sentence(nmea.encode_dpt(4.2))
    assert state.depth_received_mono == 1002.0
    # An unrelated later sample doesn't backdate the others.
    assert state.fix_received_mono == 1000.0


def test_rejected_heading_does_not_stamp():
    # A spike-rejected heading must not refresh the freshness clock (it wasn't a
    # real sample), so a jammed compass emitting only garbage still goes stale.
    clock = [500.0]
    state = NavigationState()
    nav = Navigator(state, bus=None, mono_fn=lambda: clock[0])
    nav.handle_sentence(nmea.encode_hdm(10.0))
    assert state.heading_received_mono == 500.0
    clock[0] = 600.0
    # A huge jump (> default heading_jump_max_deg) is rejected on first sight.
    nav.handle_sentence(nmea.encode_hdm(200.0))
    assert state.heading_received_mono == 500.0  # unchanged


# --------------------------------------------------------------------------- #
# 2. Controller feeds ages to the governor: stale compass coasts + recovers.
# --------------------------------------------------------------------------- #
def _guided_controller():
    clock = [1000.0]
    state = NavigationState()
    # Seed a fresh heading + fix so the first tick is fully fresh.
    state.heading_received_mono = 1000.0
    state.fix_received_mono = 1000.0
    ctrl = Controller(state, _FakeMotor(), bus=None, mono_fn=lambda: clock[0])
    ctrl.handle_command({"type": "heading_hold", "heading": 90.0, "throttle": 0.8})
    return clock, state, ctrl


def test_heading_stale_in_heading_hold_forces_zero_then_recovers():
    clock, state, ctrl = _guided_controller()

    # Fresh heading -> the boat drives.
    state.fix_seq += 1  # keep the fix failsafe happy (fresh fix each tick)
    cmd = ctrl.control_tick(0.2)
    assert cmd.thrust > 0
    assert not ctrl.safety_status.heading_stale

    # Compass goes silent for 5 s (> heading_stale_s default 3 s) -> coast.
    clock[0] = 1005.0
    state.fix_seq += 1
    cmd = ctrl.control_tick(0.2)
    assert ctrl.safety_status.heading_stale
    assert cmd.thrust == 0.0

    # A fresh heading arrives -> the flag clears and thrust ramps back up.
    state.heading_received_mono = 1005.0
    state.fix_seq += 1
    cmd = ctrl.control_tick(0.2)
    assert not ctrl.safety_status.heading_stale
    assert cmd.thrust > 0


def test_never_stamped_heading_does_not_trip_in_harness_style_loop():
    # A controller whose heading is NEVER stamped (age is None) must NOT coast --
    # "never sampled" is treated as fresh so the deterministic harness (which
    # doesn't advance the staleness clock) can't be false-tripped.
    clock = [0.0]
    state = NavigationState()  # no receive-times stamped
    ctrl = Controller(state, _FakeMotor(), bus=None, mono_fn=lambda: clock[0])
    ctrl.handle_command({"type": "heading_hold", "heading": 45.0, "throttle": 0.7})
    for _ in range(5):
        state.fix_seq += 1
        cmd = ctrl.control_tick(0.2)
    assert not ctrl.safety_status.heading_stale
    assert cmd.thrust > 0


# --------------------------------------------------------------------------- #
# 3. Config: loss-of-fix failsafe now defaults ON.
# --------------------------------------------------------------------------- #
def test_fix_failsafe_on_by_default_in_config():
    assert AppConfig().safety.fix_failsafe_enabled is True


# --------------------------------------------------------------------------- #
# 4. Health telemetry block shape (call Runtime.telemetry() directly).
# --------------------------------------------------------------------------- #
def test_health_block_shape(tmp_path):
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    health = rt.telemetry()["health"]

    assert set(health) == {
        "fix_age_s",
        "heading_age_s",
        "depth_age_s",
        "imu_age_s",
        "controller_fault",
        "controller_tick_age_s",
        "heading_stale",
        "fix_lost",
        "depth_stale",
    }
    # Never received yet -> ages are null (not zero); loop hasn't run -> null.
    assert health["fix_age_s"] is None
    assert health["heading_age_s"] is None
    assert health["depth_age_s"] is None
    assert health["imu_age_s"] is None
    assert health["controller_tick_age_s"] is None
    assert health["controller_fault"] is None
    assert health["heading_stale"] is False
    assert health["depth_stale"] is False


def test_health_ages_populate_after_samples(tmp_path):
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path)
    clock = [2000.0]
    rt = Runtime(cfg, mono_fn=lambda: clock[0])
    rt.navigator.handle_sentence(
        nmea.encode_rmc(GeoPoint(59.0, 18.0), sog_knots=1.0, cog_deg=90)
    )
    rt.navigator.handle_sentence(nmea.encode_hdm(90.0))
    clock[0] = 2003.5  # 3.5 s later
    health = rt.telemetry()["health"]
    assert health["fix_age_s"] == pytest.approx(3.5, abs=1e-6)
    assert health["heading_age_s"] == pytest.approx(3.5, abs=1e-6)
    assert health["depth_age_s"] is None  # never sent a depth sentence
