"""ML anchor v2 (#34): mount/steer-sign correctness, the runtime
residual-decay guardrail, and the offline fine-tuning tool.

Steering polarity: the Helm multiplies the whole mode command by the boat's
``steer_sign`` (+1 bow, -1 stern), so AnchorMLMode always acts in the
normalised "helm frame". v2 makes the training env apply the same flip (so
stern scenarios train the polarity the runtime executes), records the trained
convention in the model JSON (``steer_sign``), and maps a policy trained in a
different convention back into the helm frame at load time.
"""

from __future__ import annotations

import gzip
import json
import math
import os

import numpy as np
import pytest

from vanchor.controller.anchor_ml import AnchorMLMode, _MODEL_PATH, pid_base
from vanchor.core.models import ControlModeName, Environment, GeoPoint, GpsFix
from vanchor.core.state import NavigationState

from .harness import Harness

STOCKHOLM = GeoPoint(59.3293, 18.0686)
_M_PER_DEG = 111320.0


def _state_at(dist_m: float, radius_m: float = 5.0) -> NavigationState:
    """A state with the boat ``dist_m`` north of the anchor, at rest."""
    st = NavigationState()
    st.anchor = STOCKHOLM
    st.anchor_radius_m = radius_m
    st.fix = GpsFix(
        point=GeoPoint(STOCKHOLM.lat + dist_m / _M_PER_DEG, STOCKHOLM.lon),
        sog_knots=0.0,
        cog_deg=0.0,
    )
    st.heading_deg = 0.0
    return st


def _write_policy(tmp_path, steer_sign: float | None):
    """Copy the shipped policy with a given steer_sign metadata value."""
    d = json.load(open(_MODEL_PATH))
    if steer_sign is not None:
        d["steer_sign"] = steer_sign
    else:
        d.pop("steer_sign", None)
    p = tmp_path / f"policy_{steer_sign}.json"
    p.write_text(json.dumps(d))
    return str(p)


# --------------------------------------------------------------------------- #
# Mount / steer-sign correctness
# --------------------------------------------------------------------------- #
def test_policy_defaults_to_bow_helm_convention():
    """The shipped model has no steer_sign metadata: it was trained in the
    bow/raw convention == the helm frame, so the residual applies unflipped."""
    m = AnchorMLMode()
    assert m.policy_steer_sign == 1.0
    assert m.steer_sign == 1.0  # default mount assumption mirrors the Helm


def test_policy_steer_sign_metadata_flips_steering_residual(tmp_path):
    """A model declaring steer_sign=-1 (trained raw on a stern mount) gets its
    steering residual mapped into the helm frame: exact mirror of the +1 load,
    with thrust untouched. Inside the PID deadband the command IS the residual,
    so the flip is directly observable."""
    m_plus = AnchorMLMode(model_path=_write_policy(tmp_path, 1.0))
    m_minus = AnchorMLMode(model_path=_write_policy(tmp_path, -1.0))
    st = _state_at(0.4)  # inside the 0.8 m deadband -> pid base = (0, 0)
    sp_plus = m_plus.update(st, 0.2)
    sp_minus = m_minus.update(st, 0.2)
    assert sp_plus.thrust == pytest.approx(sp_minus.thrust)
    assert sp_plus.steering == pytest.approx(-sp_minus.steering)


def test_controller_seeds_mode_with_helm_steer_sign():
    h = Harness(model="fossen")
    ml = h.controller.modes[ControlModeName.ANCHOR_ML]
    assert ml.steer_sign == h.controller.helm.steer_sign == 1.0


def _rig_stern_boat(h: Harness) -> None:
    """Turn the harness boat into a stern-mount one, the way app.py does:
    flip the physics lever arm AND the helm's steer_sign (+ the mode mirror)."""
    from vanchor.sim.fossen import FossenParams

    # Full mechanical steering swing, like the real deployment (config default
    # 180°, the wide worm-gear servo) -- the shipped full-azimuth Smart policy is
    # trained for and rescaled to that range.
    h.sim.boat.params = FossenParams(thruster_x_m=-1.7, max_steer_angle_deg=180.0)
    h.state.max_steer_angle_deg = 180.0
    rebuild = getattr(h.sim.boat, "_build_matrices", None)
    if callable(rebuild):
        rebuild()
    h.controller.helm.steer_sign = -1.0
    h.controller.modes[ControlModeName.ANCHOR_ML].steer_sign = -1.0


def test_stern_mount_boat_holds_station_with_anchor_ml():
    """End-to-end: a STERN-mounted boat (helm steer_sign -1) running the hybrid
    learned spot-lock under wind + current stays inside the watch circle -- the
    Helm's mount flip applies to the whole hybrid command, so the residual
    cannot destabilise a stern boat."""
    env = Environment(current_speed=0.3, current_dir=90.0, wind_speed=4.0, wind_dir=120.0)
    h = Harness(model="fossen", environment=env)
    _rig_stern_boat(h)
    h.command({"type": "anchor_ml", "radius_m": 6.0})
    distances = h.run(seconds=200)
    settled = distances[-150:]
    assert max(settled) < 6.0
    assert sum(settled) / len(settled) < 3.0


def test_training_env_mirrors_helm_steer_sign():
    """env.py applies the runtime Helm's mount flip: stern scenarios train the
    same helm-frame polarity the deployed pipeline executes."""
    from experiments.anchor_policy.env import AnchorEnv
    from experiments.anchor_policy.scenarios import sample_scenario

    env = AnchorEnv(duration_s=10.0)
    stern = dict(sample_scenario(3), thruster_x_m=-1.7)
    env.reset(stern)
    assert env._steer_sign == -1.0
    bow = dict(sample_scenario(3), thruster_x_m=1.7)
    env.reset(bow)
    assert env._steer_sign == 1.0


def test_training_env_pid_base_converges_on_stern_mount():
    """With the mount flip, the pure PID base (residual = 0) drives a STERN
    boat to the mark in the training env -- pre-fix its steering was inverted
    there and it could not close on the anchor."""
    from experiments.anchor_policy.env import AnchorEnv
    from experiments.anchor_policy.scenarios import sample_scenario

    env = AnchorEnv(duration_s=60.0)
    sc = dict(
        sample_scenario(7),
        thruster_x_m=-1.7,
        wind_speed=0.0, current_speed=0.0, gust=0.0,
        wind_var=0.0, cur_var=0.0,
        start_dist=8.0, u0=0.0, v0=0.0,
    )
    env.reset(sc)
    done, dist = False, sc["start_dist"]
    while not done:
        _, _, done, info = env.step(np.zeros(2))  # pure PID base
        dist = info["dist"]
    assert dist < 3.0


# --------------------------------------------------------------------------- #
# Residual-decay guardrail
# --------------------------------------------------------------------------- #
def _tick(mode: AnchorMLMode, dist_m: float, seconds: float, dt: float = 0.2):
    st = _state_at(dist_m)
    for _ in range(int(seconds / dt)):
        mode.update(st, dt)


def test_fresh_activation_starts_at_nominal_scale():
    m = AnchorMLMode()
    st = _state_at(1.0)
    m.activate(st)
    assert m.residual_scale_effective == m.residual_scale == pytest.approx(0.3)


def test_guardrail_decays_residual_when_hold_degrades():
    """Persistently far outside the watch circle -> the residual decays toward
    the pure-PID floor (and stays bounded at/above the configured minimum)."""
    m = AnchorMLMode()
    m.activate(_state_at(0.0))
    _tick(m, dist_m=15.0, seconds=180.0)  # 3x the radius for 3 minutes
    assert m.guard_hold_ratio > m.guard_bad_ratio
    assert m.residual_scale_effective < 0.05
    assert m.residual_scale_effective >= m.guard_min_scale


def test_guardrail_recovers_when_hold_is_good_again():
    m = AnchorMLMode()
    m.activate(_state_at(0.0))
    _tick(m, dist_m=15.0, seconds=180.0)
    assert m.residual_scale_effective < 0.05
    _tick(m, dist_m=1.0, seconds=400.0)   # holding well again
    assert m.guard_hold_ratio < m.guard_good_ratio
    assert m.residual_scale_effective > 0.9 * m.residual_scale


def test_guardrail_hysteresis_band_holds_scale_no_flapping():
    """Between the good and bad thresholds the scale is frozen: no rapid
    decay/recover flapping around a single threshold."""
    m = AnchorMLMode()
    m.activate(_state_at(0.0))
    mid = 0.5 * (m.guard_good_ratio + m.guard_bad_ratio)
    _tick(m, dist_m=mid * 5.0, seconds=300.0)  # settle the EMA mid-band
    assert m.guard_good_ratio < m.guard_hold_ratio < m.guard_bad_ratio
    m._scale_eff = 0.15  # a half-decayed scale
    _tick(m, dist_m=mid * 5.0, seconds=60.0)
    assert m.residual_scale_effective == pytest.approx(0.15)


def test_guardrail_never_exceeds_nominal():
    m = AnchorMLMode()
    m.activate(_state_at(0.0))
    _tick(m, dist_m=0.5, seconds=120.0)  # perfect hold from the start
    assert m.residual_scale_effective == pytest.approx(m.residual_scale)


def test_good_case_hold_unaffected_by_guardrail():
    """Non-regression: under ordinary wind/current the guardrail stays at the
    nominal scale and the hybrid holds as before (~the test_anchor_ml bar)."""
    env = Environment(current_speed=0.3, current_dir=90.0, wind_speed=4.0, wind_dir=120.0)
    h = Harness(model="fossen", environment=env)
    h.command({"type": "anchor_ml", "radius_m": 6.0})
    distances = h.run(seconds=200)
    settled = distances[-150:]
    assert max(settled) < 6.0
    assert sum(settled) / len(settled) < 3.0
    ml = h.controller.modes[ControlModeName.ANCHOR_ML]
    assert ml.residual_scale_effective > 0.9 * ml.residual_scale


# --------------------------------------------------------------------------- #
# Offline fine-tuning tool (data extraction / round-trip)
# --------------------------------------------------------------------------- #
def _telemetry_record(t, lat, lon, heading, thrust, steering, anchor,
                      mode="anchor_ml", mount="stern"):
    return {
        "t": t,
        "kind": "telemetry",
        "data": {
            "mode": mode,
            "position": {"lat": lat, "lon": lon},
            "anchor": {"lat": anchor.lat, "lon": anchor.lon},
            "anchor_radius_m": 5.0,
            "heading_deg": heading,
            "sog_knots": 0.2,
            "distance_to_anchor_m": _M_PER_DEG * abs(anchor.lat - lat),
            "motor": {"thrust": thrust, "steering": steering},
            "est_drift_settled": True,
            "est_drift_mps": 0.3,
            "est_drift_dir": 90.0,
            "boat": {"mass_kg": 320.0, "max_thrust_n": 260.0,
                     "thruster_mount": mount, "thruster_offset_m": None,
                     "hull_tracking": 1.2},
        },
    }


def _write_session(tmp_path, records, name="session-test"):
    d = tmp_path / name
    d.mkdir()
    with gzip.open(d / "0001.ndjson.gz", "wt", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return str(d)


def test_finetune_extracts_helm_frame_transitions(tmp_path):
    from experiments.anchor_policy import finetune as ft

    anchor = STOCKHOLM
    recs = [{"t": 0.0, "kind": "meta", "data": {"name": "x"}}]
    # Boat 5 m north of the mark, drifting 0.1 m/s further north, heading 0.
    for i in range(30):
        lat = anchor.lat + (5.0 + 0.02 * i) / _M_PER_DEG
        recs.append(_telemetry_record(
            t=0.2 * i, lat=lat, lon=anchor.lon, heading=0.0,
            thrust=0.5, steering=0.25, anchor=anchor, mount="stern"))
    session = _write_session(tmp_path, recs)

    eps = ft.extract_episodes(ft.iter_records(session))
    assert len(eps) == 1
    ep = eps[0]
    assert ep["obs"].shape[1] == 8
    assert len(ep["obs"]) == len(ep["act"]) == len(ep["dist"]) == 29
    # Heading 0, mark 5 m SOUTH of the boat -> e_fwd ~ -5 m (frame is /10).
    assert ep["obs"][0][0] == pytest.approx(-0.5, abs=0.02)
    assert ep["obs"][0][1] == pytest.approx(0.0, abs=1e-6)
    # Drifting northward at ~0.1 m/s with heading 0 -> body-frame forward
    # ground velocity +0.1 m/s (away from the astern mark), scaled /1.5.
    assert ep["obs"][5][2] == pytest.approx(0.1 / 1.5, abs=0.02)
    # Stern mount: the recorded post-Helm steering (0.25) is mapped BACK into
    # the helm frame the policy acts in (-0.25); thrust is untouched.
    assert ep["act"][5][0] == pytest.approx(0.5)
    assert ep["act"][5][1] == pytest.approx(-0.25)
    assert ep["drift_mps"] == pytest.approx(0.3)


def test_finetune_splits_episodes_on_gap_and_anchor_move(tmp_path):
    from experiments.anchor_policy import finetune as ft

    anchor = STOCKHOLM
    moved = GeoPoint(anchor.lat + 3.0 / _M_PER_DEG, anchor.lon)
    recs = []
    t = 0.0
    for i in range(10):  # episode 1
        recs.append(_telemetry_record(t, anchor.lat + 4e-5, anchor.lon, 0.0, 0.3, 0.0, anchor))
        t += 0.2
    t += 30.0            # recorder gap -> split
    for i in range(10):  # episode 2
        recs.append(_telemetry_record(t, anchor.lat + 4e-5, anchor.lon, 0.0, 0.3, 0.0, anchor))
        t += 0.2
    for i in range(10):  # anchor jumped >0.75 m -> episode 3
        recs.append(_telemetry_record(t, anchor.lat + 4e-5, anchor.lon, 0.0, 0.3, 0.0, moved))
        t += 0.2
    session = _write_session(tmp_path, recs)
    eps = ft.extract_episodes(ft.iter_records(session))
    assert len(eps) == 3


def test_finetune_derives_matched_scenarios_and_dry_run(tmp_path, capsys):
    from experiments.anchor_policy import finetune as ft

    anchor = STOCKHOLM
    recs = [_telemetry_record(0.2 * i, anchor.lat + 5.0 / _M_PER_DEG, anchor.lon,
                              0.0, 0.4, 0.1, anchor, mount="stern")
            for i in range(25)]
    session = _write_session(tmp_path, recs)
    eps = ft.extract_episodes(ft.iter_records(session))
    scens = ft.derive_scenarios(eps, k=8, seed=1)
    assert len(scens) == 8
    for sc in scens:
        assert sc["thruster_x_m"] == pytest.approx(-1.7)  # recorded stern mount
        assert 0.3 * 0.7 <= sc["current_speed"] <= 0.3 * 1.3
        assert 250.0 * 0.9 * 0.9 <= sc["max_thrust_n"] <= 260.0 * 1.1
        # Same keys the base training scenarios carry (env compatibility).
        from experiments.anchor_policy.scenarios import sample_scenario
        assert set(sc) == set(sample_scenario(0))
    # --dry-run: reports, trains nothing, writes nothing, exits 0.
    rc = ft.main([session, "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert not os.path.exists(os.path.join(str(tmp_path), "finetuned_policy.json"))


def test_finetune_policy_roundtrip_loads_in_runtime_mode(tmp_path):
    """A policy written by the fine-tune tool loads straight into AnchorMLMode
    (with the steer_sign convention metadata parsed) and into TinyPolicy."""
    from experiments.anchor_policy import finetune as ft
    from experiments.anchor_policy.policy import TinyPolicy

    base = json.load(open(_MODEL_PATH))
    theta = np.asarray(base["params"], dtype=np.float64)
    out = str(tmp_path / "finetuned_policy.json")
    ft.save_policy(theta, base["sizes"], out, ["/data/debug/session-x"])

    d = json.load(open(out))
    assert d["steer_sign"] == 1.0
    assert d["finetuned_from"] == ["session-x"]
    m = AnchorMLMode(model_path=out)
    assert m.policy_steer_sign == 1.0
    sp = m.update(_state_at(6.0), 0.2)
    assert -1.0 <= sp.thrust <= 1.0 and -1.0 <= sp.steering <= 1.0
    pol = TinyPolicy.load(out)
    assert pol.n_params == theta.size
