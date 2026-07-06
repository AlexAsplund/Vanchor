"""Vectored / azimuth station-keeping (#35).

The trolling motor can rotate far past the autopilot's +/-35 deg band. The
opt-in vectored anchor hold exploits that: it computes the ground-frame thrust
direction that nulls position error + estimated drift and swings the motor
AZIMUTH toward it (via a direct ManualSetpoint, bypassing the helm's autopilot
steering cap), instead of re-orienting the hull first.

These tests run the SAME app-like closed loop (Fossen 3-DOF boat with the full
180 deg head, helm scaled to 35/180, real steering slew limit, noisy simulated
GPS/compass at 5 Hz) for the baseline and the vectored law, and check:

* default config (flag off) is bit-for-bit today's behaviour, capped at 35 deg;
* in a steady beam set the vectored hold beats the baseline on RMS radial
  error and % time in radius (both raw truth-distance and the #34
  HoldQuality metric);
* enabling it is stable: bounded heading (no spin-out), stays on station, and
  it genuinely commands azimuths beyond the 35 deg band;
* calm water degrades to the same idle behaviour as the baseline;
* the unit-level law geometry (azimuth against the drift, reverse when the
  push is astern) and the config/command/telemetry wiring.
"""

import math
from functools import lru_cache

import pytest

from vanchor.controller.controller import Controller, Helm
from vanchor.controller.modes import AnchorConfig, AnchorHoldMode
from vanchor.controller.safety import SafetyConfig
from vanchor.core.config import AppConfig, ControlConfig
from vanchor.core.geo import angle_difference, destination_point, haversine_m
from vanchor.core.models import (
    BoatState,
    ControlModeName,
    Environment,
    GeoPoint,
    GpsFix,
    ManualSetpoint,
)
from vanchor.core.state import NavigationState
from vanchor.nav.navigator import Navigator
from vanchor.sim.devices import SimCompass, SimGps
from vanchor.sim.fossen import FossenParams
from vanchor.sim.simulator import Simulator

START = GeoPoint(59.66275, 13.32247)
MAX_STEER_DEG = 180.0          # full mechanical swing (matches app default)
AUTOPILOT_DEG = 35.0           # the band the autopilot limits itself to
RADIUS_M = 5.0
STERN_X_M = -1.7               # stern mount: thruster arm AFT of the CG (< 0)


# --------------------------------------------------------------------------- #
# Deterministic closed-loop harness (mirrors the live app wiring)
# --------------------------------------------------------------------------- #
def _run_hold(
    *,
    vectored: bool,
    azimuth_deg: float = 120.0,
    steer_sign: float = 1.0,
    duration_s: float = 240.0,
    current: float = 0.35,
    current_dir: float = 90.0,
    wind: float = 0.0,
    wind_dir: float = 90.0,
    thruster_x_m: float = 1.7,
    helm_steer_sign: float = 1.0,
):
    """Drop an anchor hold at t=2 s in a steady set and record the whole run.

    Wired like the app: the sim boat maps steering [-1,1] onto the FULL
    +/-180 deg head, the helm scales guided steering to 35/180 (the autopilot
    band), and the governor slews the head at the physical 50 deg/s.

    ``thruster_x_m`` (+ bow / - stern) picks the SIM mount; ``helm_steer_sign``
    (and the AnchorConfig ``steer_sign``) mirror it so a stern boat (which
    steers/yaws the opposite way) can be exercised. The defaults (bow arm 1.7,
    steer_sign +1) reproduce today's bow harness bit-for-bit.
    """
    sim = Simulator(
        start=BoatState(point=START, heading_deg=0.0),
        params=FossenParams(max_steer_angle_deg=MAX_STEER_DEG, thruster_x_m=thruster_x_m),
        environment=Environment(
            current_speed=current, current_dir=current_dir,
            wind_speed=wind, wind_dir=wind_dir,
        ),
        model="fossen",
    )
    state = NavigationState()
    state.max_steer_angle_deg = MAX_STEER_DEG
    nav = Navigator(state, bus=None)
    ctrl = Controller(
        state,
        sim.motor,
        bus=None,
        helm=Helm(
            autopilot_steer_scale=AUTOPILOT_DEG / MAX_STEER_DEG,
            steer_sign=helm_steer_sign,
        ),
        anchor_config=AnchorConfig(
            vectored=vectored, vector_azimuth_deg=azimuth_deg, steer_sign=steer_sign
        ),
        safety_config=SafetyConfig(max_steer_slew_per_s=50.0 / MAX_STEER_DEG),
    )
    gps = SimGps(sim.truth, bus=None, update_hz=5.0)
    compass = SimCompass(sim.truth, bus=None, update_hz=5.0)
    nav.handle_sentence(gps.sample(sim.truth()))
    nav.handle_sentence(compass.sample(sim.truth()))

    dt, ctrl_dt = 0.05, 0.2
    t = 0.0
    next_gps = next_compass = next_ctrl = 0.0
    dropped = False
    rec: list[tuple] = []  # (t, truth_dist_m, truth_heading, thrust, steering)
    while t < duration_s:
        if not dropped and t >= 2.0:
            ctrl.handle_command({"type": "anchor_hold", "radius_m": RADIUS_M})
            dropped = True
        sim.step(dt)
        truth = sim.truth()
        if t >= next_gps:
            nav.handle_sentence(gps.sample(truth))
            next_gps += 0.2
        if t >= next_compass:
            nav.handle_sentence(compass.sample(truth))
            next_compass += 0.2
        if t >= next_ctrl:
            ctrl.control_tick(ctrl_dt)
            next_ctrl += ctrl_dt
        d = haversine_m(truth.point, state.anchor) if state.anchor else float("nan")
        rec.append(
            (round(t, 3), d, truth.heading_deg,
             state.motor_command.thrust, state.motor_command.steering)
        )
        t += dt
    return rec, state, ctrl


@lru_cache(maxsize=8)
def _cached_run(vectored: bool, current: float, azimuth_deg: float = 120.0):
    return _run_hold(vectored=vectored, current=current, azimuth_deg=azimuth_deg)


def _hold_metrics(rec, tail_s: float = 120.0) -> dict:
    """RMS radial error / % in radius over the trailing window (truth), plus
    heading boundedness (max unwrapped rotation) and peak steering command."""
    t_end = rec[-1][0]
    tail = [r for r in rec if r[0] >= t_end - tail_s]
    net = 0.0
    max_abs_rot = 0.0
    for a, b in zip(rec, rec[1:]):
        net += angle_difference(a[2], b[2])
        max_abs_rot = max(max_abs_rot, abs(net))
    return {
        "rms_m": math.sqrt(sum(r[1] ** 2 for r in tail) / len(tail)),
        "pct_in_radius": 100.0 * sum(1 for r in tail if r[1] <= RADIUS_M) / len(tail),
        "max_dist_m": max(r[1] for r in tail),
        "max_abs_rot_deg": max_abs_rot,
        "max_steer_deg": max(abs(r[4]) for r in rec) * MAX_STEER_DEG,
    }


# --------------------------------------------------------------------------- #
# Non-regression: flag off == today's behaviour, bit for bit
# --------------------------------------------------------------------------- #
def test_default_flag_off_reproduces_baseline_bit_for_bit():
    # With vectored=False the new knobs must be completely inert: a run with
    # defaults and a run with the knobs cranked (but the flag off) must emit
    # the IDENTICAL command stream (the sim + sensors are seeded/deterministic).
    rec_default, state_a, _ = _run_hold(vectored=False, azimuth_deg=35.0)
    rec_knobs, state_b, _ = _run_hold(vectored=False, azimuth_deg=170.0, steer_sign=-1.0)
    assert [(r[3], r[4]) for r in rec_default] == [(r[3], r[4]) for r in rec_knobs]
    assert state_a.stationkeep_vectored is False
    assert state_b.stationkeep_vectored is False


def test_default_flag_off_stays_within_autopilot_band():
    rec, _, _ = _cached_run(False, 0.35)
    assert _hold_metrics(rec)["max_steer_deg"] <= AUTOPILOT_DEG + 1e-6


# --------------------------------------------------------------------------- #
# The win: beam set, vectored vs +/-35 baseline
# --------------------------------------------------------------------------- #
def test_vectored_beats_baseline_in_beam_set():
    # Steady 0.35 m/s beam current. Measured (deterministic): baseline RMS
    # ~3.3 m / max excursion ~4.8 m (drift-out-recover limit cycle); vectored
    # RMS ~1.3 m / max ~1.6 m (pushes straight against the set). Thresholds
    # leave margin but still demand a clear win.
    base_rec, base_state, _ = _cached_run(False, 0.35)
    vec_rec, vec_state, _ = _cached_run(True, 0.35)
    base = _hold_metrics(base_rec)
    vec = _hold_metrics(vec_rec)

    assert vec["rms_m"] < 0.75 * base["rms_m"]
    assert vec["max_dist_m"] < base["max_dist_m"]
    assert vec["pct_in_radius"] >= base["pct_in_radius"]
    # Same story on the shared #34 HoldQuality metric (perceived distance).
    assert vec_state.hold_rms_m < base_state.hold_rms_m
    assert vec_state.hold_pct_in_radius >= base_state.hold_pct_in_radius


def test_vectored_beats_baseline_in_stronger_set():
    base_rec, _, _ = _cached_run(False, 0.5)
    vec_rec, _, _ = _cached_run(True, 0.5)
    base = _hold_metrics(base_rec)
    vec = _hold_metrics(vec_rec)
    assert vec["rms_m"] < 0.75 * base["rms_m"]
    assert vec["pct_in_radius"] >= base["pct_in_radius"]


def test_vectored_actually_uses_the_wider_azimuth():
    # The point of the feature: in a beam set the motor must swing well beyond
    # the 35 deg autopilot band (measured peak ~105 deg with 120 authority).
    rec, state, _ = _cached_run(True, 0.35)
    m = _hold_metrics(rec)
    assert m["max_steer_deg"] > AUTOPILOT_DEG + 10.0
    assert m["max_steer_deg"] <= 120.0 + 1e-6  # ... but never past its authority
    assert state.stationkeep_vectored is True


# --------------------------------------------------------------------------- #
# Stability when enabled: bounded heading, no spin-out, stays on station
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("current", [0.35, 0.5])
def test_vectored_is_stable_no_spinout(current):
    rec, _, _ = _cached_run(True, current)
    m = _hold_metrics(rec)
    # The hull may swing to point into the set (~90 deg here) but must never
    # wind up / spin: total unwrapped rotation stays well under a full turn.
    assert m["max_abs_rot_deg"] < 270.0
    assert m["pct_in_radius"] >= 95.0
    assert m["max_dist_m"] < RADIUS_M


def test_vectored_calm_water_idles_like_baseline():
    # No set: both laws should settle to an idle hold (no thrust, no steering
    # activity) -- the vectored law must not invent work in calm water.
    base_rec, _, _ = _run_hold(vectored=False, current=0.0, duration_s=90.0)
    vec_rec, _, _ = _run_hold(vectored=True, current=0.0, duration_s=90.0)
    base = _hold_metrics(base_rec, tail_s=45.0)
    vec = _hold_metrics(vec_rec, tail_s=45.0)
    assert vec["rms_m"] <= base["rms_m"] + 0.1
    assert vec["max_steer_deg"] <= base["max_steer_deg"] + 1e-6
    assert abs(vec_rec[-1][3]) < 0.05  # ends idling, not thrusting


# --------------------------------------------------------------------------- #
# Stern-mount validation (#35 follow-up)
# --------------------------------------------------------------------------- #
# Only the BOW mount was sim-verified in #35. A stern mount (thruster_x_m < 0)
# FLIPS the sign of the thrust-induced yaw (Fossen N = thruster_x_m * Fy): the
# same physical azimuth that walks a BOW hull bow-into-the-set instead walks a
# STERN hull stern-into-the-set. The thrust DIRECTION is unchanged (the mount
# steer_sign pre-cancels the helm flip, so the boat still pushes against the
# set); only which END weathervanes into the set flips. These tests confirm the
# flipped coupling stays BOUNDED (no spin-out) and holds AT LEAST AS WELL as the
# +/-35 baseline for the SAME stern boat.
@lru_cache(maxsize=8)
def _cached_stern_run(vectored: bool, current: float, azimuth_deg: float = 120.0):
    return _run_hold(
        vectored=vectored, current=current, azimuth_deg=azimuth_deg,
        steer_sign=-1.0, helm_steer_sign=-1.0, thruster_x_m=STERN_X_M,
    )


@pytest.mark.parametrize("current", [0.35, 0.5])
def test_vectored_stern_is_stable_no_spinout(current):
    # The flipped (stern) yaw coupling must not wind up / spin: the hull swings
    # to point stern-into-the-set (~90 deg here) but total unwrapped rotation
    # stays well under a full turn, and it stays on station.
    rec, _, _ = _cached_stern_run(True, current)
    m = _hold_metrics(rec)
    assert m["max_abs_rot_deg"] < 270.0
    assert m["pct_in_radius"] >= 95.0
    assert m["max_dist_m"] < RADIUS_M


@pytest.mark.parametrize("current", [0.35, 0.5])
def test_vectored_stern_beats_baseline(current):
    # Same stern boat, same beam set: vectored vs the +/-35 baseline. Measured
    # (deterministic): baseline RMS ~4.0-4.4 m / max excursion ~6 m; vectored
    # RMS ~1.6-1.9 m / max ~2 m -- a decisive win. Assert the honest, margined
    # claim.
    base_rec, base_state, _ = _cached_stern_run(False, current)
    vec_rec, vec_state, _ = _cached_stern_run(True, current)
    base = _hold_metrics(base_rec)
    vec = _hold_metrics(vec_rec)
    assert vec["rms_m"] < 0.6 * base["rms_m"]
    assert vec["max_dist_m"] < base["max_dist_m"]
    assert vec["pct_in_radius"] >= base["pct_in_radius"]
    # Same story on the shared #34 HoldQuality metric (perceived distance).
    assert vec_state.hold_rms_m < base_state.hold_rms_m
    assert vec_state.hold_pct_in_radius >= base_state.hold_pct_in_radius


def test_vectored_stern_uses_the_wider_azimuth():
    # A stern hold must also reach beyond the 35 deg autopilot band (measured
    # peak ~110 deg with 120 authority) and flag the vectored telemetry.
    rec, state, _ = _cached_stern_run(True, 0.35)
    m = _hold_metrics(rec)
    assert m["max_steer_deg"] > AUTOPILOT_DEG + 10.0
    assert m["max_steer_deg"] <= 120.0 + 1e-6
    assert state.stationkeep_vectored is True


def test_vectored_stern_settles_stern_into_the_set():
    # The self-aligning-yaw FLIP made concrete: where a BOW hull settles
    # bow-into-the-set (bow ~270 deg, into a set running east), the STERN hull
    # settles STERN-into-the-set -- bow pointing DOWNSTREAM (~90 deg) -- the
    # mirror-image equilibrium. And it is SETTLED, not spinning.
    stern_rec, _, _ = _cached_stern_run(True, 0.35)
    bow_rec, _, _ = _cached_run(True, 0.35)
    stern_final = stern_rec[-1][2]
    bow_final = bow_rec[-1][2]
    # Bow points downstream (set dir 90 deg) for the stern mount; opposite for bow.
    assert abs(angle_difference(stern_final, 90.0)) < 35.0
    assert abs(angle_difference(bow_final, 270.0)) < 35.0
    assert abs(angle_difference(stern_final, bow_final)) > 120.0
    # Settled: negligible net rotation over the last 30 s (no slow spin).
    late = [r for r in stern_rec if r[0] >= stern_rec[-1][0] - 30.0]
    net = sum(angle_difference(a[2], b[2]) for a, b in zip(late, late[1:]))
    assert abs(net) < 15.0


def test_vectored_stern_pushes_thrust_against_the_set():
    # Aim check for a stern mount, through the FULL helm pipeline: boat on the
    # mark, bow north, drift pushing EAST. Despite the stern helm flipping
    # steering polarity, the mount pre-cancel keeps the PHYSICAL thrust aimed
    # WEST -- straight against the set -- from the very first tick.
    state = _stationary_state(heading=0.0)
    state.est_drift_settled = True
    state.est_drift_mps = 0.3
    state.est_drift_dir = 90.0
    state.est_drift_east = 0.3
    state.est_drift_north = 0.0
    mode = AnchorHoldMode(
        AnchorConfig(vectored=True, vector_azimuth_deg=120.0, steer_sign=-1.0)
    )
    mode.activate(state)
    sp = mode.update(state, 0.2)
    cmd = Helm(steer_sign=-1.0).compute(sp, state, 0.2)
    assert cmd.thrust != 0.0
    azimuth = cmd.steering * MAX_STEER_DEG  # physical motor angle off the bow
    # Reverse thrust pushes opposite the motor axis.
    push = state.heading_deg + azimuth + (180.0 if cmd.thrust < 0 else 0.0)
    # Push opposes the set (drift dir 90 deg -> ground-frame push ~270 deg).
    assert abs(angle_difference(push, 270.0)) < 5.0


# --------------------------------------------------------------------------- #
# Analysis-runner support for the wide azimuth (#35 follow-up, concern (b))
# --------------------------------------------------------------------------- #
def test_analysis_runner_scores_vectored_vs_baseline():
    # The analysis harness builds its sim boat with the model-default 35 deg
    # swing, so a Scenario could not exercise the wide azimuth. The new opt-in
    # scenario flags give it the full swing + the vectored law, for BOTH mounts,
    # so vectored vs baseline can be scored end-to-end through run_scenario.
    from vanchor.analysis.metrics import anchor_metrics
    from vanchor.analysis.runner import Command, Scenario, run_scenario

    def hold(*, vectored: bool, thruster_x_m: float):
        scen = Scenario(
            name="sk", start=START, model="fossen", duration_s=200.0,
            environment=Environment(current_speed=0.35, current_dir=90.0),
            commands=[Command(2.0, {"type": "anchor_hold", "radius_m": RADIUS_M})],
            sim_max_steer_angle_deg=MAX_STEER_DEG,
            station_keep_vectored=vectored,
            station_keep_azimuth_deg=120.0,
            thruster_x_m=thruster_x_m,
        )
        return anchor_metrics(run_scenario(scen).tail(120.0))

    for x in (1.7, STERN_X_M):  # bow and stern
        base = hold(vectored=False, thruster_x_m=x)
        vec = hold(vectored=True, thruster_x_m=x)
        assert vec.steady_rms_m < base.steady_rms_m
        assert vec.within_radius_pct >= base.within_radius_pct


def test_analysis_runner_defaults_leave_boat_unchanged():
    # The new fields must default OFF so every existing scenario/experiment
    # builds the exact same boat + controller as before (no wide swing, bow
    # mount, classic PD hold).
    from vanchor.analysis.runner import Scenario

    s = Scenario(name="d")
    assert s.sim_max_steer_angle_deg is None
    assert s.station_keep_vectored is False
    assert s.station_keep_azimuth_deg == 35.0
    assert s.thruster_x_m is None


# --------------------------------------------------------------------------- #
# Unit-level law geometry (no sim loop)
# --------------------------------------------------------------------------- #
def _stationary_state(heading: float = 0.0) -> NavigationState:
    state = NavigationState()
    state.fix = GpsFix(point=START, sog_knots=0.0, cog_deg=0.0)
    state.heading_deg = heading
    state.anchor = START
    state.max_steer_angle_deg = MAX_STEER_DEG
    return state


def test_vectored_law_points_azimuth_against_settled_drift():
    # Boat on the mark, bow north, drift pushing EAST: the law must aim the
    # motor ~90 deg to PORT (push west) with forward thrust -- far beyond the
    # autopilot band, without waiting for the hull to turn.
    state = _stationary_state(heading=0.0)
    state.est_drift_settled = True
    state.est_drift_mps = 0.3
    state.est_drift_dir = 90.0
    state.est_drift_east = 0.3
    state.est_drift_north = 0.0
    mode = AnchorHoldMode(AnchorConfig(vectored=True, vector_azimuth_deg=120.0))
    mode.activate(state)
    sp = mode.update(state, 0.2)
    assert isinstance(sp, ManualSetpoint)
    assert sp.thrust > 0.05
    assert sp.steering * MAX_STEER_DEG == pytest.approx(-90.0, abs=2.0)
    assert state.stationkeep_vectored is True
    assert state.stationkeep_azimuth_deg == pytest.approx(-90.0, abs=2.0)


def test_vectored_law_reverses_when_push_is_astern():
    # Anchor well astern (bearing 180, bow north): push the boat backwards with
    # REVERSE thrust and a near-zero azimuth instead of swinging the hull 180.
    state = _stationary_state(heading=0.0)
    state.fix = GpsFix(point=destination_point(START, 20.0, 0.0), sog_knots=0.0, cog_deg=0.0)
    mode = AnchorHoldMode(AnchorConfig(vectored=True, vector_azimuth_deg=120.0))
    mode.activate(state)
    sp = mode.update(state, 0.2)
    assert isinstance(sp, ManualSetpoint)
    assert sp.thrust < 0.0
    assert abs(sp.steering * MAX_STEER_DEG) < 10.0


def test_vectored_law_clamps_to_configured_authority():
    # Same beam-drift geometry but only 35 deg of authority: deflection clamps
    # to the band (and thrust is reduced by the misalignment, not zeroed).
    state = _stationary_state(heading=0.0)
    state.est_drift_settled = True
    state.est_drift_mps = 0.3
    state.est_drift_dir = 90.0
    state.est_drift_east = 0.3
    state.est_drift_north = 0.0
    mode = AnchorHoldMode(AnchorConfig(vectored=True, vector_azimuth_deg=35.0))
    mode.activate(state)
    sp = mode.update(state, 0.2)
    assert sp.steering * MAX_STEER_DEG == pytest.approx(-35.0, abs=0.5)
    assert 0.0 < sp.thrust


def test_vectored_law_mirrors_stern_mount_sign():
    # A stern mount flips the helm's steer_sign; the config mirrors it so the
    # PHYSICAL azimuth is unchanged after the helm's multiplication.
    state = _stationary_state(heading=0.0)
    state.est_drift_settled = True
    state.est_drift_mps = 0.3
    state.est_drift_dir = 90.0
    state.est_drift_east = 0.3
    state.est_drift_north = 0.0
    bow = AnchorHoldMode(AnchorConfig(vectored=True, vector_azimuth_deg=120.0))
    stern = AnchorHoldMode(
        AnchorConfig(vectored=True, vector_azimuth_deg=120.0, steer_sign=-1.0)
    )
    bow.activate(state)
    sp_bow = bow.update(state, 0.2)
    stern.activate(state)
    sp_stern = stern.update(state, 0.2)
    helm_bow = Helm(steer_sign=1.0)
    helm_stern = Helm(steer_sign=-1.0)
    cmd_bow = helm_bow.compute(sp_bow, state, 0.2)
    cmd_stern = helm_stern.compute(sp_stern, state, 0.2)
    assert cmd_bow.steering == pytest.approx(cmd_stern.steering)


# --------------------------------------------------------------------------- #
# Wiring: config defaults, command opt-in, telemetry
# --------------------------------------------------------------------------- #
def test_defaults_are_off_everywhere():
    assert AnchorConfig().vectored is False
    assert AnchorConfig().vector_azimuth_deg == 35.0
    cc = ControlConfig()
    assert cc.station_keep_vectored is False
    assert cc.station_keep_azimuth_deg == 35.0


def test_state_telemetry_exposes_stationkeep():
    state = NavigationState()
    payload = state.to_dict()
    assert payload["stationkeep"] == {"vectored": False, "azimuth_deg": 0.0}
    state.stationkeep_vectored = True
    state.stationkeep_azimuth_deg = -92.34
    payload = state.to_dict()
    assert payload["stationkeep"] == {"vectored": True, "azimuth_deg": -92.3}


def test_anchor_hold_command_accepts_vectored_flag():
    from vanchor.sim.devices import SimMotorController

    state = NavigationState()
    state.fix = GpsFix(point=START)
    ctrl = Controller(state, SimMotorController(), bus=None)
    hold = ctrl.modes[ControlModeName.ANCHOR_HOLD]
    assert hold.config.vectored is False
    ctrl.handle_command({"type": "anchor_hold", "vectored": True})
    assert hold.config.vectored is True
    ctrl.handle_command({"type": "anchor_hold", "vectored": False})
    assert hold.config.vectored is False
    # Absent key leaves the setting untouched.
    ctrl.handle_command({"type": "anchor_hold", "vectored": True})
    ctrl.handle_command({"type": "anchor_hold"})
    assert hold.config.vectored is True


def test_mode_change_resets_stationkeep_telemetry():
    from vanchor.sim.devices import SimMotorController

    state = NavigationState()
    state.fix = GpsFix(point=START)
    state.max_steer_angle_deg = MAX_STEER_DEG
    ctrl = Controller(state, SimMotorController(), bus=None)
    ctrl.modes[ControlModeName.ANCHOR_HOLD].config.vectored = True
    ctrl.handle_command({"type": "anchor_hold"})
    ctrl.state.fix_seq += 1
    ctrl.control_tick(0.2)
    assert state.stationkeep_vectored is True
    ctrl.handle_command({"type": "stop"})
    assert state.stationkeep_vectored is False
    assert state.stationkeep_azimuth_deg == 0.0


def test_runtime_wires_station_keep_config(tmp_path):
    from vanchor.app import Runtime

    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.control.station_keep_vectored = True
    cfg.control.station_keep_azimuth_deg = 120.0
    rt = Runtime(cfg)
    hold = rt.controller.modes[ControlModeName.ANCHOR_HOLD]
    assert hold.config.vectored is True
    assert hold.config.vector_azimuth_deg == 120.0
    assert hold.config.steer_sign == 1.0  # bow mount
