"""GPS walk characterization (analysis/gps_walk.py, issue #154) -- synthetic
recordings, no hardware."""
from __future__ import annotations

import gzip
import json
import math
import random

from vanchor.analysis import gps_walk as gw


def _write_recording(path, *, hours_s=600.0, hz=5.0, walk_tau=30.0,
                     walk_sigma=1.0, vel_noise=0.05, hacc=1.5, seed=7):
    """Stationary boat: an Ornstein-Uhlenbeck position walk + white velocity."""
    rng = random.Random(seed)
    lat0, lon0 = 59.66, 13.32
    dt = 1.0 / hz
    n = int(hours_s * hz)
    e = n_off = 0.0
    a = math.exp(-dt / walk_tau)
    b = walk_sigma * math.sqrt(1 - a * a)
    lines = []
    coslat = math.cos(math.radians(lat0))
    for i in range(n):
        e = a * e + b * rng.gauss(0, 1)
        n_off = a * n_off + b * rng.gauss(0, 1)
        lines.append(json.dumps({
            "t": 1786400000.0 + i * dt,
            "kind": "telemetry",
            "data": {
                "position": {"lat": lat0 + n_off / 111320.0,
                             "lon": lon0 + e / (111320.0 * coslat)},
                "fusion": {"ground_vel_n_mps": rng.gauss(0, vel_noise),
                           "ground_vel_e_mps": rng.gauss(0, vel_noise)},
                "gps": {"h_acc_m": hacc},
            },
        }))
        if i % 50 == 0:                       # interleave non-telemetry records
            lines.append(json.dumps({"t": 1786400000.0 + i * dt,
                                     "kind": "nmea", "data": "$GNRMC,..."}))
    with gzip.open(path, "wt") as fh:
        fh.write("\n".join(lines))


def test_analyze_recovers_walk_statistics(tmp_path):
    p = tmp_path / "session.ndjson.gz"
    _write_recording(p, walk_tau=30.0, walk_sigma=1.0, vel_noise=0.05)
    res = gw.analyze([str(p)])
    assert res["ok"] and res["samples"] == 3000
    assert 4.5 < res["rate_hz"] < 5.5
    # OU with sigma=1 -> per-axis std ~1 m, 2D RMS ~1.4 m (loose bounds).
    assert 0.5 < res["scatter"]["rms_2d_m"] < 3.0
    # Autocorrelation time in the ballpark of the injected 30 s.
    assert 10.0 < res["autocorr_time_s"] < 90.0
    # Velocity floor recovered (~0.05 m/s per axis).
    v = res["velocity_floor"]
    assert 0.03 < v["std_vel_n_mps"] < 0.08
    # hAcc honesty compares reported vs empirical.
    assert abs(res["hacc_honesty"]["mean_reported_hacc_m"] - 1.5) < 1e-6
    # Pure-walk signal: window-mean steps GROW with window size (short windows
    # are within the walk's memory; long ones approach independent draws) and
    # saturate near sqrt(2)*sigma. (Real GPS = walk + white noise; the minimum
    # of this curve is the optimal averaging window.)
    wd = res["window_deviation_m"]
    assert wd[60] > wd[5]
    assert wd[60] < 3.0                       # saturates near sqrt(2)*sigma ~ 1.4


def test_report_formats_and_suggests_constants(tmp_path):
    p = tmp_path / "session.ndjson.gz"
    _write_recording(p)
    rep = gw.format_report(gw.analyze([str(p)]))
    for needle in ("position scatter", "walk tau", "velocity floor",
                   "deadband", "outer position-loop tau"):
        assert needle in rep


def test_too_few_samples_is_a_clear_error(tmp_path):
    p = tmp_path / "tiny.ndjson"
    p.write_text(json.dumps({"t": 1.0, "kind": "telemetry",
                             "data": {"position": {"lat": 1, "lon": 2}}}))
    res = gw.analyze([str(p)])
    assert res["ok"] is False and "stationary" in res["error"]


def test_reads_plain_and_gz_and_merges_parts(tmp_path):
    p1 = tmp_path / "a.ndjson.gz"
    _write_recording(p1, hours_s=60.0)
    p2 = tmp_path / "b.ndjson"
    p2.write_text("\n".join(json.dumps({
        "t": 1786500000.0 + i, "kind": "telemetry",
        "data": {"position": {"lat": 59.66, "lon": 13.32}}}) for i in range(20)))
    samples = gw.read_samples([str(p2), str(p1)])   # order-independent
    assert len(samples) == 320
    assert samples[0].t < samples[-1].t              # time-sorted
