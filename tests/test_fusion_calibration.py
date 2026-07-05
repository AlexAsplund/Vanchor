"""Fusion calibration: the still-capture tuner, persistence, navigator wiring,
and the runtime capture -> tune -> save -> reset flow."""
import asyncio
import random

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.core.models import GeoPoint, ImuSample
from vanchor.core.state import NavigationState
from vanchor.nav import nmea
from vanchor.nav.calibration import (
    CaptureBuffer,
    FusionCalibration,
    clear_calibration,
    load_calibration,
    save_calibration,
    tune,
)
from vanchor.nav.fusion import NavFusion
from vanchor.nav.navigator import Navigator


def _run(coro):
    asyncio.new_event_loop().run_until_complete(coro)


def _still_buffer(vel_sigma=0.04, head_sigma=1.2, bias=0.15, seed=0):
    rng = random.Random(seed)
    buf = CaptureBuffer()
    for i in range(300):
        buf.add_imu(bias + rng.gauss(0, 0.05), i * 0.02)
    for i in range(150):
        buf.add_gps(59.0 + rng.gauss(0, 3e-7), 18.0 + rng.gauss(0, 3e-7),
                    rng.gauss(0, vel_sigma), rng.gauss(0, vel_sigma), i * 0.1)
        buf.add_heading((90.0 + rng.gauss(0, head_sigma)) % 360,
                        cog=None, sog=0.0, thrust=0.0, gyro=0.0, t=i * 0.1)
    return buf


def test_tuner_recovers_bias_and_noise():
    cal, warnings = tune(_still_buffer(), "still")
    assert abs(cal.gyro_bias_dps - 0.15) < 0.02
    assert abs(cal.yaw_rate_sigma_dps - 0.05) < 0.02
    assert abs(cal.gps_vel_sigma_mps - 0.04) < 0.02
    assert abs(cal.heading_sigma_deg - 1.2) < 0.4
    assert warnings == []
    # every gain got tuned
    assert cal.vel_tau_s and cal.heading_gain and cal.crab_min_sog_mps


def test_tuner_gains_are_monotonic_in_noise():
    quiet, _ = tune(_still_buffer(vel_sigma=0.03, head_sigma=1.0), "still")
    noisy, _ = tune(_still_buffer(vel_sigma=0.25, head_sigma=6.0), "still")
    assert noisy.vel_tau_s > quiet.vel_tau_s        # noisier velocity -> more smoothing
    assert noisy.heading_gain < quiet.heading_gain  # noisier compass -> gentler blend


def test_tuner_warns_when_moving():
    buf = CaptureBuffer()
    for i in range(60):
        buf.add_imu(0.0, i * 0.02)
    for i in range(60):
        buf.add_gps(59.0, 18.0, 1.5, 0.0, i * 0.1)  # a clear 1.5 m/s drift
    _, warnings = tune(buf, "still")
    assert any("moving" in w for w in warnings)


def test_align_tuner_recovers_mounting_offset():
    # Boat drives due north (course 0); compass reads 6° (a mounting offset).
    buf = CaptureBuffer()
    for i in range(40):
        buf.add_heading(6.0, cog=0.0, sog=2.0, thrust=0.4, gyro=0.0, t=i * 0.2)
    cal, warnings = tune(buf, "align")
    # offset s.t. heading + offset ~= course -> -6
    assert cal.heading_offset_deg is not None and abs(cal.heading_offset_deg + 6.0) < 0.5
    assert warnings == []


def test_align_warns_without_motion():
    buf = CaptureBuffer()
    for i in range(40):
        buf.add_heading(6.0, cog=0.0, sog=0.1, thrust=0.0, gyro=0.0, t=i * 0.2)  # too slow
    cal, warnings = tune(buf, "align")
    assert cal.heading_offset_deg is None and any("straight" in w for w in warnings)


def test_interference_tuner_measures_heading_drift_vs_thrust():
    # Boat held still (gyro says no rotation) but the compass drifts with thrust.
    buf = CaptureBuffer()
    for i in range(40):
        thrust = i / 39.0                       # ramp 0 -> 1
        buf.add_heading(90.0 + 8.0 * thrust,    # compass swings up to 8° at full thrust
                        cog=None, sog=0.0, thrust=thrust, gyro=0.0, t=i * 0.3)
    cal, warnings = tune(buf, "interference")
    assert cal.motor_interference_deg is not None and abs(cal.motor_interference_deg - 8.0) < 0.5
    assert cal.motor_interference_slope is not None and abs(cal.motor_interference_slope - 8.0) < 1.0
    # score: 8 deg drift over a 20 deg "unusable" scale -> ~60/100
    assert cal.motor_interference_score is not None and abs(cal.motor_interference_score - 60) <= 3
    assert warnings == []


def test_interference_score_bounds():
    # No drift -> 100 (perfect); a huge drift -> 0 (unusable).
    perfect = CaptureBuffer()
    huge = CaptureBuffer()
    for i in range(40):
        thrust = i / 39.0
        perfect.add_heading(90.0, cog=None, sog=0.0, thrust=thrust, gyro=0.0, t=i * 0.3)
        huge.add_heading(90.0 + 40.0 * thrust, cog=None, sog=0.0, thrust=thrust, gyro=0.0, t=i * 0.3)
    assert tune(perfect, "interference")[0].motor_interference_score == 100
    assert tune(huge, "interference")[0].motor_interference_score == 0


def test_interference_recommendations_scale_with_severity():
    from vanchor.nav.calibration import interference_recommendations
    assert interference_recommendations(None) == []
    good = interference_recommendations(90)
    assert len(good) == 1 and "well sited" in good[0].lower()
    moderate = " ".join(interference_recommendations(60)).lower()
    assert "farther from the motor" in moderate and "twist" in moderate
    assert "mu-metal" not in moderate and "dual-antenna" not in moderate
    severe = " ".join(interference_recommendations(30)).lower()
    assert "mu-metal" in severe and "faraday" in severe        # honest shielding physics
    assert "dual-antenna" in severe and "software compensation" in severe


def test_stop_and_get_carry_interference_recommendations(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    from vanchor.core.models import MotorCommand
    rt.start_fusion_capture("interference")
    for i in range(40):                                        # ramp thrust, big drift
        t = i / 39.0
        rt.navigator.state.motor_command = MotorCommand(thrust=t)  # set before the sample
        rt.navigator.handle_sentence(nmea.encode_hdt((90.0 + 30.0 * t) % 360))
    stop = rt.stop_fusion_capture()
    assert stop["mode"] == "interference" and stop["recommendations"]
    assert any("dual-antenna" in r for r in stop["recommendations"])  # severe -> escalated


def test_calibration_merge_keeps_each_modes_measurement():
    still = FusionCalibration(gyro_bias_dps=0.1, vel_tau_s=2.5)
    align = FusionCalibration(heading_offset_deg=-6.0)
    merged = still.merged_with(align)
    assert merged.gyro_bias_dps == 0.1 and merged.vel_tau_s == 2.5  # kept
    assert merged.heading_offset_deg == -6.0                        # added


def test_calibration_persistence_round_trip(tmp_path):
    cal = FusionCalibration(gyro_bias_dps=0.2, vel_tau_s=1.5, heading_gain=0.04)
    save_calibration(tmp_path, cal)
    loaded = load_calibration(tmp_path)
    assert loaded.gyro_bias_dps == 0.2 and loaded.vel_tau_s == 1.5
    clear_calibration(tmp_path)
    assert load_calibration(tmp_path) is None


def test_navigator_removes_gyro_bias_and_sets_gains():
    st = NavigationState()
    fusion = NavFusion()
    nav = Navigator(st, bus=None, mono_fn=lambda: 0.0, fusion=fusion)
    nav.apply_calibration(FusionCalibration(gyro_bias_dps=0.2, vel_tau_s=3.3))
    assert fusion.vel_tau_s == 3.3                  # gain applied live
    nav.handle_sentence(nmea.encode_hdt(0.0))        # seed heading
    _run(nav._on_imu(ImuSample(gz=0.2, source="t")))  # raw 0.2 - bias 0.2 -> 0
    assert abs(st.yaw_rate_dps) < 1e-9               # fused rate has the bias removed
    # resetting reverts the gain to the NavFusion default
    nav.apply_calibration(FusionCalibration())
    assert fusion.vel_tau_s == NavFusion().vel_tau_s


def test_navigator_applies_heading_offset():
    st = NavigationState()
    nav = Navigator(st, bus=None, mono_fn=lambda: 0.0, fusion=NavFusion())
    nav.apply_calibration(FusionCalibration(heading_offset_deg=5.0))
    nav.handle_sentence(nmea.encode_hdt(100.0))
    assert abs(st.heading_deg - 105.0) < 1e-6          # mounting offset applied
    nav.apply_calibration(FusionCalibration())          # revert
    nav.handle_sentence(nmea.encode_hdt(100.0))
    assert abs(st.heading_deg - 100.0) < 1e-6


def test_navigator_capture_collects_samples():
    st = NavigationState()
    nav = Navigator(st, bus=None, mono_fn=lambda: 0.0, fusion=NavFusion())
    nav.start_capture()
    _run(nav._on_imu(ImuSample(gz=0.1, source="t")))
    nav.handle_sentence(nmea.encode_rmc(GeoPoint(59.0, 18.0), sog_knots=0.0, cog_deg=0.0))
    capturing, samples, _ = nav.capture_status()
    assert capturing and samples >= 2
    buf = nav.stop_capture()
    assert buf is not None and buf.count >= 2
    assert nav.capture_status()[0] is False          # stopped


def test_runtime_capture_save_reset_flow(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    assert rt.fusion_calibration()["enabled"] is True
    assert rt.start_fusion_capture()["ok"] is True
    rng = random.Random(1)
    for i in range(40):
        _run(rt.navigator._on_imu(ImuSample(gz=0.1 + rng.gauss(0, 0.03), source="t")))
        rt.navigator.handle_sentence(
            nmea.encode_rmc(GeoPoint(59.0, 18.0), sog_knots=0.0, cog_deg=0.0))
    stop = rt.stop_fusion_capture()
    assert stop["ok"] and stop["calibration"]["samples"] > 0
    assert rt.save_fusion_calibration(stop["calibration"])["ok"]
    assert rt.fusion_calibration()["calibration"] is not None
    assert load_calibration(str(tmp_path)) is not None   # persisted
    rt.reset_fusion_calibration()
    assert rt.fusion_calibration()["calibration"] is None
