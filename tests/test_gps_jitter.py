"""Indoor GPS-jitter sim model (matches a real stationary M9N) + the accuracy-
weighted position filter that damps it."""
import math
import statistics

from vanchor.core.geo import EARTH_RADIUS_M, offset_meters
from vanchor.core.models import BoatState, GeoPoint
from vanchor.nav.gps_filter import GpsPositionFilter
from vanchor.sim.devices import SimGps

_ORIGIN = GeoPoint(59.7, 12.15)


def _indoor_sim():
    truth = BoatState(point=_ORIGIN)  # stationary
    return SimGps(lambda: truth, bus=None, update_hz=10.0, emit_velocity=True,
                  position_noise_m=0.35, walk_sigma_m=5.5, walk_tau_s=40.0,
                  vel_bias_sigma_mps=0.35, vel_tau_s=8.0, reported_hacc_m=15.0, seed=7)


def _rms_scatter(points):
    lat0 = statistics.fmean(p.lat for p in points)
    lon0 = statistics.fmean(p.lon for p in points)
    k = math.pi / 180 * EARTH_RADIUS_M
    e = [(p.lon - lon0) * k * math.cos(math.radians(lat0)) for p in points]
    n = [(p.lat - lat0) * k for p in points]
    return math.hypot(statistics.pstdev(e), statistics.pstdev(n))


def test_indoor_jitter_matches_measured_character():
    g = _indoor_sim()
    fixes = [g.sample_fix() for _ in range(3000)]  # 300 s @ 10 Hz
    rms = _rms_scatter([f.point for f in fixes])
    assert 3.0 < rms < 9.0, rms                     # measured ~5.7 m 2D RMS
    speed = statistics.fmean(math.hypot(f.vel_e_mps, f.vel_n_mps) for f in fixes)
    assert 0.15 < speed < 0.8, speed                # measured ~0.4 m/s phantom
    assert fixes[0].h_acc_m == 15.0                 # reports its poor accuracy


def test_off_profile_is_plain_white_noise():
    truth = BoatState(point=_ORIGIN)
    g = SimGps(lambda: truth, bus=None, update_hz=10.0, emit_velocity=True, seed=1)
    fixes = [g.sample_fix() for _ in range(500)]
    assert _rms_scatter([f.point for f in fixes]) < 1.0   # ~0.35 m white, no walk
    assert all(f.vel_n_mps == 0.0 and f.vel_e_mps == 0.0 for f in fixes)  # no phantom


def test_filter_reduces_stationary_scatter():
    g = _indoor_sim()
    filt = GpsPositionFilter()
    raw, filtered, now = [], [], 0.0
    for _ in range(3000):
        f = g.sample_fix()
        raw.append(f.point)
        filtered.append(filt.update(f.point, f.h_acc_m, now))
        now += 0.1
    r_raw, r_filt = _rms_scatter(raw), _rms_scatter(filtered)
    print(f"\nindoor jitter: raw RMS {r_raw:.2f} m -> filtered {r_filt:.2f} m "
          f"({100*(1-r_filt/r_raw):.0f}% reduction)")
    assert r_filt < r_raw                            # it helps
    assert r_filt < 0.9 * r_raw                      # by a non-trivial margin


def test_filter_passthrough_for_a_good_fix():
    filt = GpsPositionFilter()
    now = 0.0
    import random
    rng = random.Random(0)
    devs = []
    for _ in range(200):
        p = offset_meters(_ORIGIN, rng.gauss(0, 1.0), rng.gauss(0, 1.0))  # 1 m noise
        out = filt.update(p, 1.5, now)               # hAcc 1.5 < good 3 -> passthrough
        now += 0.1
        devs.append(math.hypot((out.lat - p.lat), (out.lon - p.lon)))
    assert max(devs[5:]) < 1e-9                       # output == input (no lag)
