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
        buf.add_heading((90.0 + rng.gauss(0, head_sigma)) % 360, i * 0.1)
    return buf


def test_tuner_recovers_bias_and_noise():
    cal, warnings = tune(_still_buffer())
    assert abs(cal.gyro_bias_dps - 0.15) < 0.02
    assert abs(cal.yaw_rate_sigma_dps - 0.05) < 0.02
    assert abs(cal.gps_vel_sigma_mps - 0.04) < 0.02
    assert abs(cal.heading_sigma_deg - 1.2) < 0.4
    assert warnings == []
    # every gain got tuned
    assert cal.vel_tau_s and cal.heading_gain and cal.crab_min_sog_mps


def test_tuner_gains_are_monotonic_in_noise():
    quiet, _ = tune(_still_buffer(vel_sigma=0.03, head_sigma=1.0))
    noisy, _ = tune(_still_buffer(vel_sigma=0.25, head_sigma=6.0))
    assert noisy.vel_tau_s > quiet.vel_tau_s        # noisier velocity -> more smoothing
    assert noisy.heading_gain < quiet.heading_gain  # noisier compass -> gentler blend


def test_tuner_warns_when_moving():
    buf = CaptureBuffer()
    for i in range(60):
        buf.add_imu(0.0, i * 0.02)
    for i in range(60):
        buf.add_gps(59.0, 18.0, 1.5, 0.0, i * 0.1)  # a clear 1.5 m/s drift
    _, warnings = tune(buf)
    assert any("moving" in w for w in warnings)


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
