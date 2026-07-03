"""Tests for the low-battery thrust-derating ladder (roadmap #49).

As the battery state-of-charge drops through the configured rungs the maximum
applied thrust is capped in progressive steps (a soft derate), and only at the
lowest stage is the boat handed off to the existing RTL/failsafe. STOP and every
failsafe still take precedence and are never blocked.
"""

from __future__ import annotations

import pytest

from vanchor.app import Runtime
from vanchor.controller.safety import BatteryLadder, SafetyConfig, SafetyGovernor
from vanchor.core.config import load
from vanchor.core.models import ControlModeName, GeoPoint, MotorCommand
from vanchor.core.state import NavigationState


def _state() -> NavigationState:
    s = NavigationState()
    s.mode = ControlModeName.MANUAL
    return s


# --------------------------------------------------------------------------- #
# Pure ladder: SoC -> cap
# --------------------------------------------------------------------------- #
def _ladder(**kw) -> BatteryLadder:
    class _Cfg:
        battery_ladder = kw.get("rungs", [[40.0, 0.7], [25.0, 0.45], [15.0, 0.25]])
        battery_rtl_soc_pct = kw.get("rtl", 10.0)
        battery_ladder_enabled = kw.get("enabled", True)

    return BatteryLadder.from_config(_Cfg())


def test_cap_full_above_top_rung():
    lad = _ladder()
    assert lad.cap_for(100.0) == 1.0
    assert lad.cap_for(41.0) == 1.0
    assert lad.cap_for(40.0) == 0.7  # at the rung threshold it engages


def test_cap_is_monotonically_non_increasing_as_soc_drops():
    lad = _ladder()
    caps = [lad.cap_for(soc) for soc in range(100, -1, -1)]
    for a, b in zip(caps, caps[1:]):
        assert b <= a  # never RISES as SoC falls
    # And the ladder actually bites at the documented rungs.
    assert lad.cap_for(30.0) == 0.7
    assert lad.cap_for(20.0) == 0.45
    assert lad.cap_for(12.0) == 0.25
    assert lad.cap_for(0.0) == 0.25


def test_caps_are_clamped_to_unit_interval():
    lad = _ladder(rungs=[[50.0, 5.0], [20.0, -3.0]])
    assert lad.cap_for(45.0) == 1.0  # 5.0 clamped down to 1.0
    assert lad.cap_for(10.0) == 0.0  # -3.0 clamped up to 0.0


def test_disabled_ladder_never_derates():
    lad = _ladder(enabled=False)
    assert lad.cap_for(1.0) == 1.0
    assert lad.at_rtl(1.0) is False


def test_at_rtl_lowest_stage():
    lad = _ladder(rtl=10.0)
    assert lad.at_rtl(11.0) is False
    assert lad.at_rtl(10.0) is True
    assert lad.at_rtl(5.0) is True


def test_malformed_rungs_are_skipped():
    lad = _ladder(rungs=[[40.0, 0.7], "bogus", [None, 0.5], [25.0, 0.45]])
    assert lad.cap_for(30.0) == 0.7
    assert lad.cap_for(20.0) == 0.45


# --------------------------------------------------------------------------- #
# Governor honours the cap (soft derate) but STOP/failsafes still win
# --------------------------------------------------------------------------- #
def test_governor_caps_thrust_magnitude():
    gov = SafetyGovernor(SafetyConfig(max_thrust_slew_per_s=100.0, reverse_delay_s=0.0))
    gov.set_thrust_cap(0.4)
    cmd, status = gov.govern(MotorCommand(thrust=1.0), _state(), dt=1.0, fix_is_fresh=True)
    assert cmd.thrust == pytest.approx(0.4)
    assert status.thrust_derated is True
    assert status.thrust_cap == pytest.approx(0.4)


def test_governor_cap_never_raises_thrust():
    gov = SafetyGovernor(SafetyConfig(max_thrust_slew_per_s=100.0))
    gov.set_thrust_cap(0.5)
    # A command already under the cap is untouched.
    cmd, status = gov.govern(MotorCommand(thrust=0.3), _state(), dt=1.0, fix_is_fresh=True)
    assert cmd.thrust == pytest.approx(0.3)


def test_stepping_soc_down_gives_monotonic_thrust_caps():
    lad = _ladder()
    gov = SafetyGovernor(SafetyConfig(max_thrust_slew_per_s=100.0, reverse_delay_s=0.0))
    applied = []
    for soc in (100.0, 40.0, 25.0, 15.0, 8.0):
        gov.set_thrust_cap(lad.cap_for(soc))
        cmd, _ = gov.govern(MotorCommand(thrust=1.0), _state(), dt=1.0, fix_is_fresh=True)
        applied.append(cmd.thrust)
    # Full thrust commanded throughout; the applied cap only ever steps DOWN.
    for a, b in zip(applied, applied[1:]):
        assert b <= a + 1e-9
    assert applied[0] == pytest.approx(1.0)
    assert applied[-1] == pytest.approx(0.25)


def test_stop_zeroes_instantly_at_any_stage():
    gov = SafetyGovernor(SafetyConfig(max_thrust_slew_per_s=100.0))
    for cap in (1.0, 0.7, 0.45, 0.25, 0.0):
        gov.set_thrust_cap(cap)
        # A STOP command arrives at the governor as thrust 0 -> under any cap.
        cmd, _ = gov.govern(MotorCommand(thrust=0.0), _state(), dt=1.0, fix_is_fresh=True)
        assert cmd.thrust == 0.0


def test_fix_failsafe_still_wins_under_a_cap():
    gov = SafetyGovernor(
        SafetyConfig(max_thrust_slew_per_s=100.0, fix_timeout_s=1.0,
                     fix_failsafe_enabled=True)
    )
    gov.set_thrust_cap(0.5)
    # Age past the fix timeout with no fresh fix -> thrust forced to zero,
    # not merely capped.
    cmd, status = gov.govern(MotorCommand(thrust=1.0), _state(), dt=2.0, fix_is_fresh=False)
    assert status.fix_lost is True
    assert cmd.thrust == 0.0


# --------------------------------------------------------------------------- #
# Runtime wiring: evaluate_battery_ladder drives the governor + RTL hand-off
# --------------------------------------------------------------------------- #
def _runtime(tmp_path) -> Runtime:
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    return Runtime(cfg)


def test_runtime_ladder_sets_governor_cap_from_soc(tmp_path):
    rt = _runtime(tmp_path)
    rt.simulator.battery.set_soc(100.0)
    assert rt.evaluate_battery_ladder() == pytest.approx(1.0)
    rt.simulator.battery.set_soc(30.0)
    assert rt.evaluate_battery_ladder() == pytest.approx(0.7)
    assert rt.controller.safety.thrust_cap == pytest.approx(0.7)
    rt.simulator.battery.set_soc(20.0)
    assert rt.evaluate_battery_ladder() == pytest.approx(0.45)


def test_runtime_lowest_stage_hands_off_to_rtl(tmp_path):
    rt = _runtime(tmp_path)
    # Autonomous RTL is opt-in (#7): the lowest stage only self-drives when the
    # operator enabled auto_rtl.
    rt.config.safety.auto_rtl = True
    calls = []
    rt._schedule_auto_rtl = lambda: calls.append(True)  # type: ignore[method-assign]
    rt.state.launch = GeoPoint(59.0, 18.0)

    rt.simulator.battery.set_soc(20.0)
    rt.evaluate_battery_ladder()
    assert calls == []  # above the RTL stage -> derate only, no hand-off

    rt.simulator.battery.set_soc(8.0)  # below battery_rtl_soc_pct (10)
    rt.evaluate_battery_ladder()
    assert calls == [True]  # handed off once
    # Idempotent: still critically low, but not re-triggered.
    rt.evaluate_battery_ladder()
    assert calls == [True]

    # Recovering above the stage re-arms the one-shot.
    rt.simulator.battery.set_soc(50.0)
    rt.evaluate_battery_ladder()
    rt.simulator.battery.set_soc(5.0)
    rt.evaluate_battery_ladder()
    assert calls == [True, True]


def test_runtime_handoff_without_launch_still_derates(tmp_path):
    rt = _runtime(tmp_path)
    calls = []
    rt._schedule_auto_rtl = lambda: calls.append(True)  # type: ignore[method-assign]
    rt.state.launch = None
    rt.simulator.battery.set_soc(5.0)
    cap = rt.evaluate_battery_ladder()
    # No launch point -> no RTL plan, but the lowest derate cap still holds.
    assert calls == []
    assert cap == pytest.approx(0.25)


def test_lowest_stage_recommends_only_when_auto_rtl_off(tmp_path):
    # #7: with auto_rtl off (the default), an empty battery must NOT self-drive
    # RTL -- it raises the RTL recommendation/alarm instead. The derate cap still
    # applies.
    rt = _runtime(tmp_path)
    assert rt.config.safety.auto_rtl is False
    calls = []
    rt._schedule_auto_rtl = lambda: calls.append(True)  # type: ignore[method-assign]
    rt.state.launch = GeoPoint(59.0, 18.0)  # a launch point exists...

    rt.simulator.battery.set_soc(5.0)  # below battery_rtl_soc_pct (10)
    cap = rt.evaluate_battery_ladder()
    assert calls == []  # ...but no autonomous RTL is engaged
    assert rt.state.rtl_recommended is True  # recommendation raised instead
    assert cap == pytest.approx(0.25)  # derate still holds


def test_disabled_ladder_leaves_cap_full(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    cfg.safety.battery_ladder_enabled = False
    rt = Runtime(cfg)
    rt.simulator.battery.set_soc(5.0)
    assert rt.evaluate_battery_ladder() == pytest.approx(1.0)
    assert rt.controller.safety.thrust_cap == pytest.approx(1.0)
