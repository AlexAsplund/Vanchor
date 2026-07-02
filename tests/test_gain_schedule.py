"""Tests for adaptive helm gain scheduling + per-boat saved gain profiles (#31).

Covers:
  * :class:`GainSchedule` interpolation, clamping and the neutral default,
  * the helm's effective steering gain (``kp_eff``) scaling with SOG, and a
    neutral schedule being a no-op (regression),
  * a boat profile's saved gains applying on profile switch,
  * saving applied gains round-tripping through ``boat_gains.json``,
  * the tuner-persist path writing the active profile only behind the flag.
"""

from __future__ import annotations

import pytest

from vanchor.analysis.tuning import gains_block_from_tuning
from vanchor.app import Runtime
from vanchor.controller.controller import GainSchedule, Helm
from vanchor.core.config import AppConfig, ControlConfig
from vanchor.core.models import ControlModeName, GuidedSetpoint
from vanchor.core.pid import PID
from vanchor.core.state import NavigationState


# --------------------------------------------------------------------------- #
# GainSchedule (pure math)
# --------------------------------------------------------------------------- #
def test_schedule_neutral_default_is_flat_one():
    sch = GainSchedule()
    assert sch.is_neutral
    for sog in (0.0, 0.3, 1.0, 2.0, 10.0):
        assert sch.multiplier(sog) == 1.0


def test_schedule_more_gain_when_slow():
    # Physically-correct shape: weak authority at low speed -> MORE gain.
    sch = GainSchedule(sog_lo_kn=0.3, sog_hi_kn=2.0, mult_lo=2.0, mult_hi=0.5)
    assert sch.multiplier(0.0) == 2.0          # held flat at/below lo
    assert sch.multiplier(0.3) == 2.0
    assert sch.multiplier(2.0) == 0.5          # held flat at/above hi
    assert sch.multiplier(5.0) == 0.5
    mid = sch.multiplier(1.15)                 # halfway between the breakpoints
    assert mid == pytest.approx(1.25, abs=1e-9)
    # Monotonically decreasing with speed.
    assert sch.multiplier(0.5) > sch.multiplier(1.0) > sch.multiplier(1.9)
    assert not sch.is_neutral


def test_schedule_clamps_multiplier():
    sch = GainSchedule(sog_lo_kn=0.0, sog_hi_kn=1.0, mult_lo=99.0, mult_hi=99.0,
                       mult_min=0.1, mult_max=3.0)
    assert sch.multiplier(0.5) == 3.0          # clamped up to the ceiling
    sch2 = GainSchedule(sog_lo_kn=0.0, sog_hi_kn=1.0, mult_lo=-5.0, mult_hi=-5.0,
                        mult_min=0.2, mult_max=3.0)
    assert sch2.multiplier(0.5) == 0.2         # clamped up to the floor


def test_schedule_degenerate_band_uses_low():
    sch = GainSchedule(sog_lo_kn=2.0, sog_hi_kn=1.0, mult_lo=1.5, mult_hi=0.5)
    assert sch.multiplier(1.5) == 1.5          # hi <= lo -> mult_lo


# --------------------------------------------------------------------------- #
# Helm.kp_eff scales with SOG
# --------------------------------------------------------------------------- #
def _guided_helm(schedule) -> Helm:
    pid = PID(kp=0.04, ki=0.0, kd=0.0, output_min=-1.0, output_max=1.0)
    return Helm(pid, steer_tau=0.0, gain_schedule=schedule)


def _drive(helm: Helm, sog_knots: float) -> float:
    state = NavigationState()
    state.heading_deg = 0.0
    state.sog_knots = sog_knots
    sp = GuidedSetpoint(target_heading=30.0, thrust=0.6)  # above STEER_EPS
    helm.compute(sp, state, dt=0.2)
    return helm.kp_eff


def test_kp_eff_scales_across_sog():
    sch = GainSchedule(sog_lo_kn=0.3, sog_hi_kn=2.0, mult_lo=2.0, mult_hi=0.5)
    helm = _guided_helm(sch)
    slow = _drive(helm, 0.2)
    fast = _drive(helm, 2.5)
    assert slow == pytest.approx(0.04 * 2.0)   # base * mult_lo
    assert fast == pytest.approx(0.04 * 0.5)   # base * mult_hi
    assert slow > fast                         # more effective gain when slow
    # The base gain is NOT permanently mutated by the schedule.
    assert helm.pid.kp == pytest.approx(0.04)


def test_neutral_schedule_keeps_constant_kp():
    helm = _guided_helm(GainSchedule())        # neutral
    for sog in (0.0, 0.5, 1.0, 3.0):
        assert _drive(helm, sog) == pytest.approx(0.04)
    assert helm.pid.kp == pytest.approx(0.04)


def test_no_schedule_matches_plain_pid():
    helm = _guided_helm(None)
    assert _drive(helm, 1.0) == pytest.approx(0.04)
    assert helm.pid.kp == pytest.approx(0.04)


# --------------------------------------------------------------------------- #
# Runtime wiring: default schedule is neutral (non-regression)
# --------------------------------------------------------------------------- #
def _runtime(tmp_path) -> Runtime:
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path)
    return Runtime(cfg)


def test_runtime_default_schedule_is_neutral(tmp_path):
    rt = _runtime(tmp_path)
    sch = rt.controller.helm.gain_schedule
    assert sch is not None and sch.is_neutral


def test_config_schedule_defaults_are_neutral():
    c = ControlConfig()
    assert c.steer_gain_mult_lo == 1.0 and c.steer_gain_mult_hi == 1.0


# --------------------------------------------------------------------------- #
# Per-boat saved gain profiles
# --------------------------------------------------------------------------- #
def test_saved_gains_apply_on_profile_switch(tmp_path):
    rt = _runtime(tmp_path)
    ids = [p["id"] for p in rt.boats.list()]
    assert len(ids) >= 2
    first, second = ids[0], ids[1]

    # Save the current (distinctive) gains into the active (first) profile.
    rt.controller.helm.pid.kp = 0.077
    rt.save_boat_gains()  # active profile
    assert rt.boat_gains()["heading"]["kp"] == pytest.approx(0.077)

    # Switch to a profile with no saved gains -> gains are left as they are.
    rt.boat_profiles_activate(second)
    assert rt.controller.helm.pid.kp == pytest.approx(0.077)
    rt.controller.helm.pid.kp = 0.02  # scribble

    # Switch back -> the first profile's saved gains are re-applied.
    rt.boat_profiles_activate(first)
    assert rt.controller.helm.pid.kp == pytest.approx(0.077)


def test_saved_gains_round_trip_through_disk(tmp_path):
    rt = _runtime(tmp_path)
    active = rt.boats.active_id
    rt.controller.helm.pid.kp = 0.066
    rt.controller.helm.gain_schedule.mult_lo = 1.8
    rt.save_boat_gains()

    # A fresh Runtime over the same data dir re-applies the saved gains at start.
    rt2 = _runtime(tmp_path)
    assert rt2.boats.active_id == active
    assert rt2.controller.helm.pid.kp == pytest.approx(0.066)
    assert rt2.controller.helm.gain_schedule.mult_lo == pytest.approx(1.8)


def test_profile_without_gains_keeps_defaults(tmp_path):
    rt = _runtime(tmp_path)
    base_kp = rt.controller.helm.pid.kp
    # No gains saved for anyone: switching profiles must not disturb the gains.
    ids = [p["id"] for p in rt.boats.list()]
    rt.boat_profiles_activate(ids[1])
    assert rt.controller.helm.pid.kp == pytest.approx(base_kp)
    assert rt.boat_gains() == {}


# --------------------------------------------------------------------------- #
# Tuner integration
# --------------------------------------------------------------------------- #
def test_gains_block_from_tuning_shapes():
    assert gains_block_from_tuning("heading", {"heading_kp": 0.05, "heading_kd": 0.03}) == {
        "heading": {"kp": 0.05, "kd": 0.03}
    }
    assert gains_block_from_tuning("cruise", {"kp": 0.7, "ki": 0.2}) == {
        "cruise": {"kp": 0.7, "ki": 0.2}
    }
    assert gains_block_from_tuning("drift", {"kp": 0.6, "ki": 0.3}) == {
        "drift": {"kp": 0.6, "ki": 0.3}
    }
    assert gains_block_from_tuning("anchor", {"kp": 0.1, "kd": 0.5, "idle_deadband_m": 0.9}) == {
        "anchor": {"kp": 0.1, "kd": 0.5, "idle_deadband_m": 0.9}
    }
    assert gains_block_from_tuning("nope", {}) == {}


def test_tuner_persist_writes_active_profile_only_with_flag(tmp_path):
    rt = _runtime(tmp_path)
    active = rt.boats.active_id
    params = {"heading_kp": 0.05, "heading_kd": 0.03}

    # Default (endpoint) behaviour: live-apply only, nothing persisted.
    rt.apply_tuned_gains("heading", params)
    assert rt.controller.helm.pid.kp == pytest.approx(0.05)  # applied live
    assert rt.boat_gains() == {}                              # not persisted

    # With the flag: also persisted into the active profile's saved gains.
    rt.apply_tuned_gains("heading", params, persist=True)
    saved = rt.boat_gains()
    assert saved["heading"]["kp"] == pytest.approx(0.05)
    assert saved["heading"]["kd"] == pytest.approx(0.03)

    # And it survives a "restart" (fresh Runtime, same data dir).
    rt2 = _runtime(tmp_path)
    assert rt2.boats.active_id == active
    assert rt2.controller.helm.pid.kp == pytest.approx(0.05)
