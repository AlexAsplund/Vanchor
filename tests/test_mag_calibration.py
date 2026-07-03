"""Tests for the interactive magnetometer hard/soft-iron calibration (#41).

Covers the numeric fit (recover a known offset + scale from synthetic distorted
data), JSON persistence round-trip + startup reload, degenerate-input rejection,
and the capture runner + its server endpoints (driven with a fake sample
provider so no hardware is needed).
"""

from __future__ import annotations

import asyncio
import math

import numpy as np
import pytest

from vanchor.controller.calibration import (
    MagCalibration,
    MagCalibrationRunner,
    MagCalibrationStore,
    fit_hard_soft_iron,
)


# --------------------------------------------------------------------------- #
# Synthetic data helpers
# --------------------------------------------------------------------------- #
def _sphere_points(n: int = 200, seed: int = 0) -> np.ndarray:
    """``n`` roughly-even unit-sphere points (deterministic)."""
    rng = np.random.default_rng(seed)
    v = rng.normal(size=(n, 3))
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    return v


def _distort(unit: np.ndarray, offset, scale, field: float = 50.0) -> np.ndarray:
    """Turn unit-sphere points into a hard/soft-iron-distorted magnetometer
    capture: ``raw = offset + scale ⊙ (field * unit)`` (diagonal soft iron)."""
    return np.asarray(offset) + np.asarray(scale) * (field * unit)


# --------------------------------------------------------------------------- #
# Fit
# --------------------------------------------------------------------------- #
def test_fit_recovers_known_offset_and_scale():
    true_offset = np.array([12.0, -7.0, 30.0])
    true_scale = np.array([1.0, 1.4, 0.75])  # soft-iron ellipse
    raw = _distort(_sphere_points(300, seed=1), true_offset, true_scale, field=48.0)

    cal = fit_hard_soft_iron(raw)

    # Hard-iron offset (the persisted compass offset) recovered tightly.
    assert np.allclose(cal.offset, true_offset, atol=0.5)
    # Correcting the raw data yields a near-perfect sphere.
    assert cal.residual < 1e-3
    assert cal.quality > 0.99
    corrected = np.array([cal.apply(p) for p in raw])
    mags = np.linalg.norm(corrected, axis=1)
    assert np.std(mags) / np.mean(mags) < 1e-2
    # A full spread of points lights every heading bin.
    assert cal.coverage == pytest.approx(1.0)


def test_fit_recovers_offset_with_noise():
    rng = np.random.default_rng(7)
    true_offset = np.array([-5.0, 20.0, 3.0])
    true_scale = np.array([1.1, 0.9, 1.05])
    raw = _distort(_sphere_points(400, seed=3), true_offset, true_scale, field=45.0)
    raw = raw + rng.normal(scale=0.4, size=raw.shape)  # sensor noise

    cal = fit_hard_soft_iron(raw)

    assert np.allclose(cal.offset, true_offset, atol=1.5)
    assert cal.quality > 0.9


def test_fit_apply_matches_matrix_math():
    cal = fit_hard_soft_iron(
        _distort(_sphere_points(120, seed=5), [1.0, 2.0, 3.0], [1.2, 0.8, 1.0])
    )
    p = np.array([10.0, -4.0, 6.0])
    expected = np.asarray(cal.matrix) @ (p - np.asarray(cal.offset))
    assert np.allclose(cal.apply(p), expected)


# --------------------------------------------------------------------------- #
# Applied correction -> heading (#5): calibration must feed the heading path
# --------------------------------------------------------------------------- #
def test_stored_calibration_corrects_raw_vector_to_heading(tmp_path):
    """(#5) The PERSISTED calibration, applied to a raw distorted magnetometer
    vector, recovers the correct magnetic heading. This is the correction that
    the #41 calibration was fitted for but was never actually applied to a raw
    reading before a heading was derived."""
    true_offset = np.array([12.0, -7.0, 5.0])
    true_scale = np.array([1.0, 1.4, 0.8])  # diagonal soft iron
    field = 48.0
    capture = _distort(_sphere_points(300, seed=21), true_offset, true_scale, field)

    store = MagCalibrationStore(str(tmp_path))
    store.save(fit_hard_soft_iron(capture))
    # Reload exactly what a restart would use, proving the *stored* cal is applied.
    cal = MagCalibrationStore(str(tmp_path)).calibration
    assert cal is not None

    for heading in (0.0, 45.0, 90.0, 137.0, 250.0, 359.0):
        rad = math.radians(heading)
        # A true (undistorted) field at this heading in the sensor frame; the
        # convention is heading = atan2(-y, x) -> u = (cos H, -sin H, 0).
        u = np.array([math.cos(rad), -math.sin(rad), 0.0])
        raw = true_offset + true_scale * (field * u)  # same distortion as capture
        got = cal.heading_deg(raw)
        err = abs(((got - heading + 180.0) % 360.0) - 180.0)
        assert err < 1.0, (heading, got)


def test_uncorrected_raw_vector_reads_wrong_heading():
    """Sanity: WITHOUT applying the calibration the same raw vector reads a
    materially wrong heading, so the correction is doing real work (this guards
    against the correction silently becoming a no-op)."""
    identity = MagCalibration(
        offset=(0.0, 0.0, 0.0),
        matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        field_strength=1.0,
        residual=0.0,
        quality=1.0,
        n_samples=0,
    )
    true_offset = np.array([12.0, -7.0, 5.0])
    true_scale = np.array([1.0, 1.4, 0.8])
    field = 48.0
    heading = 90.0
    rad = math.radians(heading)
    u = np.array([math.cos(rad), -math.sin(rad), 0.0])
    raw = true_offset + true_scale * (field * u)
    err = abs(((identity.heading_deg(raw) - heading + 180.0) % 360.0) - 180.0)
    assert err > 5.0  # the raw distorted vector is well off the true heading


# --------------------------------------------------------------------------- #
# Degenerate / bad input
# --------------------------------------------------------------------------- #
def test_too_few_samples_rejected():
    with pytest.raises(ValueError, match="at least"):
        fit_hard_soft_iron(_sphere_points(5))


def test_coplanar_capture_rejected():
    # All z == 0: a flat ring, not an ellipsoid -> rank-deficient / non-ellipsoid.
    pts = _sphere_points(120, seed=2)
    pts[:, 2] = 0.0
    with pytest.raises(ValueError):
        fit_hard_soft_iron(pts * 40.0 + np.array([3.0, 4.0, 0.0]))


def test_wrong_shape_rejected():
    with pytest.raises(ValueError, match="N, 3"):
        fit_hard_soft_iron(np.zeros((50, 2)))


def test_non_finite_rejected():
    pts = _distort(_sphere_points(60, seed=4), [0, 0, 0], [1, 1, 1])
    pts[0, 0] = np.inf
    with pytest.raises(ValueError):
        fit_hard_soft_iron(pts)


def test_duplicate_samples_do_not_count():
    # 100 identical rows collapse to 1 distinct point -> too few.
    dup = np.tile(np.array([1.0, 2.0, 3.0]), (100, 1))
    with pytest.raises(ValueError, match="at least"):
        fit_hard_soft_iron(dup)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def test_persistence_round_trip(tmp_path):
    raw = _distort(_sphere_points(150, seed=8), [4.0, -2.0, 9.0], [1.3, 0.7, 1.1])
    cal = fit_hard_soft_iron(raw)

    store = MagCalibrationStore(str(tmp_path))
    assert store.calibration is None  # nothing persisted yet
    store.save(cal)

    # A fresh store over the same dir reloads the calibration on startup.
    reloaded = MagCalibrationStore(str(tmp_path))
    assert reloaded.calibration is not None
    assert np.allclose(reloaded.calibration.offset, cal.offset)
    assert np.allclose(reloaded.calibration.matrix, cal.matrix)
    assert reloaded.calibration.field_strength == pytest.approx(cal.field_strength)
    assert reloaded.calibration.quality == pytest.approx(cal.quality)


def test_dict_round_trip_is_exact():
    cal = fit_hard_soft_iron(
        _distort(_sphere_points(120, seed=9), [1.0, 1.0, 1.0], [1.0, 1.2, 0.9])
    )
    back = MagCalibration.from_dict(cal.to_dict())
    assert back is not None
    assert np.allclose(back.offset, cal.offset)
    assert np.allclose(back.matrix, cal.matrix)


def test_from_dict_rejects_malformed():
    assert MagCalibration.from_dict({"offset": [1, 2]}) is None
    assert MagCalibration.from_dict({"offset": [1, 2, 3], "matrix": [[1, 2, 3]]}) is None
    assert MagCalibration.from_dict({}) is None


def test_store_ignores_corrupt_file(tmp_path):
    (tmp_path / "mag_calibration.json").write_text("{ not json ]")
    store = MagCalibrationStore(str(tmp_path))
    assert store.calibration is None  # loads as "no calibration", no raise


def test_store_clear_removes_file(tmp_path):
    cal = fit_hard_soft_iron(
        _distort(_sphere_points(120, seed=11), [0, 0, 0], [1, 1, 1])
    )
    store = MagCalibrationStore(str(tmp_path))
    store.save(cal)
    assert (tmp_path / "mag_calibration.json").exists()
    store.clear()
    assert store.calibration is None
    assert not (tmp_path / "mag_calibration.json").exists()
    store.clear()  # idempotent — no raise on a missing file


# --------------------------------------------------------------------------- #
# Runner (capture session) with a fake provider
# --------------------------------------------------------------------------- #
class _FakeProvider:
    """Replays a synthetic distorted capture one sample per call, then None."""

    def __init__(self, points: np.ndarray) -> None:
        self._pts = [tuple(map(float, p)) for p in points]
        self._i = 0

    def __call__(self):
        if self._i >= len(self._pts):
            return None
        p = self._pts[self._i]
        self._i += 1
        return p


def test_runner_add_sample_then_fit(tmp_path):
    raw = _distort(_sphere_points(200, seed=12), [6.0, -3.0, 15.0], [1.25, 0.8, 1.1])
    store = MagCalibrationStore(str(tmp_path))
    runner = MagCalibrationRunner(lambda: None, store)

    assert runner.start() is True
    assert runner.start() is False  # already running
    for p in raw:
        runner.add_sample(*p)
    out = runner.stop()

    assert out["ok"] is True
    assert out["result"]["quality"] > 0.99
    assert np.allclose(out["result"]["offset"], [6.0, -3.0, 15.0], atol=0.6)
    # Persisted + reloadable.
    assert MagCalibrationStore(str(tmp_path)).calibration is not None


def test_runner_stop_rejects_short_capture(tmp_path):
    store = MagCalibrationStore(str(tmp_path))
    runner = MagCalibrationRunner(lambda: None, store)
    runner.start()
    runner.add_sample(1.0, 2.0, 3.0)
    out = runner.stop()
    assert out["ok"] is False
    assert "at least" in out["message"]
    assert store.calibration is None  # nothing saved


def test_runner_add_sample_ignored_when_not_running(tmp_path):
    runner = MagCalibrationRunner(lambda: None, MagCalibrationStore(str(tmp_path)))
    assert runner.add_sample(1.0, 2.0, 3.0) is False


def test_runner_dedup_and_non_finite(tmp_path):
    runner = MagCalibrationRunner(lambda: None, MagCalibrationStore(str(tmp_path)))
    runner.start()
    assert runner.add_sample(1.0, 1.0, 1.0) is True
    assert runner.add_sample(1.0, 1.0, 1.0) is False  # duplicate of previous
    assert runner.add_sample(float("nan"), 0.0, 0.0) is False
    assert len(runner.samples) == 1


def test_runner_cancel_discards(tmp_path):
    runner = MagCalibrationRunner(lambda: None, MagCalibrationStore(str(tmp_path)))
    runner.start()
    runner.add_sample(1.0, 2.0, 3.0)
    out = runner.cancel()
    assert out["running"] is False
    assert runner.samples == []


async def test_runner_async_loop_collects_from_provider(tmp_path):
    raw = _distort(_sphere_points(150, seed=13), [2.0, 5.0, -4.0], [1.1, 0.9, 1.2])
    store = MagCalibrationStore(str(tmp_path))
    runner = MagCalibrationRunner(_FakeProvider(raw), store, poll_hz=1000.0)
    runner.start()
    # Let the poll loop drain the provider.
    for _ in range(50):
        if len(runner.samples) >= len(raw):
            break
        await asyncio.sleep(0.01)
    out = runner.stop()
    assert out["ok"] is True
    assert len(runner.samples) == len(raw)
    assert out["result"]["quality"] > 0.95


# --------------------------------------------------------------------------- #
# Server endpoints (fake runtime, fake provider)
# --------------------------------------------------------------------------- #
def _fake_runtime(tmp_path):
    class _Cfg:
        data_dir = str(tmp_path)

    class _FakeRuntime:
        def __init__(self) -> None:
            self.config = _Cfg()
            # Flat mag_* scalars the default provider reads (None -> no live feed).
            self.state = type("S", (), {"mag_x": None, "mag_y": None, "mag_z": None})()

        def telemetry(self):
            return {}

    return _FakeRuntime()


def test_server_app_registers_mag_routes(tmp_path):
    from vanchor.ui import server as server_mod

    app = server_mod.create_app(_fake_runtime(tmp_path))
    paths = {r.path for r in app.routes}
    assert "/api/calibrate/mag/start" in paths
    assert "/api/calibrate/mag/stop" in paths
    assert "/api/calibrate/mag/status" in paths
    assert "/api/calibrate/mag/cancel" in paths


def test_server_mag_endpoints_end_to_end(tmp_path):
    """Drive start -> (poll-loop drains a fake provider) -> stop over HTTP.

    The app is used WITHOUT the lifespan context so the telemetry broadcaster
    never starts (it would spin on the fake runtime); the endpoints themselves
    run on the TestClient's event loop, so the runner's async capture loop works.
    """
    from fastapi.testclient import TestClient

    from vanchor.ui import server as server_mod

    raw = _distort(_sphere_points(200, seed=14), [3.0, -6.0, 8.0], [1.2, 0.85, 1.0])
    rt = _fake_runtime(tmp_path)
    app = server_mod.create_app(rt)

    # Pre-seed the lazily-attached runner with a deterministic synthetic provider
    # (no live magnetometer on the bench). The endpoints reuse this instance.
    runner = MagCalibrationRunner(_FakeProvider(raw), MagCalibrationStore(str(tmp_path)))
    rt._mag_cal_runner = runner

    client = TestClient(app)  # no `with`: lifespan/broadcaster stays off

    started = client.post("/api/calibrate/mag/start").json()
    assert started["started"] is True
    assert started["running"] is True

    # Feed the synthetic capture directly into the seeded runner (deterministic;
    # the async provider-poll loop is covered separately). Then status + stop go
    # through the HTTP endpoints.
    for p in raw:
        runner.add_sample(*p)
    status = client.get("/api/calibrate/mag/status").json()
    assert status["n_samples"] == len(raw)

    out = client.post("/api/calibrate/mag/stop").json()
    assert out["ok"] is True
    assert out["result"]["quality"] > 0.95
    assert np.allclose(out["result"]["offset"], [3.0, -6.0, 8.0], atol=0.8)
    # Persisted: a fresh store over the same dir reloads it.
    assert MagCalibrationStore(str(tmp_path)).calibration is not None


def test_server_mag_stop_without_samples_is_clean(tmp_path):
    """With no live magnetometer, stop rejects cleanly (ok=False), no crash."""
    from fastapi.testclient import TestClient

    from vanchor.ui import server as server_mod

    app = server_mod.create_app(_fake_runtime(tmp_path))
    client = TestClient(app)
    client.post("/api/calibrate/mag/start")
    out = client.post("/api/calibrate/mag/stop").json()
    assert out["ok"] is False
    assert "at least" in out["message"]
    # Cancel is also clean.
    assert client.post("/api/calibrate/mag/cancel").json()["running"] is False
