"""Tests for the trip log (#66): accumulation, persistence, GPX, auto start/stop."""

import json
import os

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.core.config import AppConfig
from vanchor.core.geo import destination_point, haversine_m, mps_to_knots
from vanchor.core.models import GeoPoint
from vanchor.nav.trip import TripLog, trip_to_gpx
from vanchor.ui.server import create_app

HERE = GeoPoint(59.66275, 13.32247)


@pytest.fixture()
def log(tmp_path):
    # No auto-start so manual recording is deterministic; tight breadcrumb spacing.
    return TripLog(
        str(tmp_path), min_distance_m=10.0, auto=False, start_speed_kn=0.5
    )


# ---------------------------------------------------------------------- #
# Distance / speed accumulation
# ---------------------------------------------------------------------- #
def test_distance_accumulates_over_known_track(log):
    # A straight 100 m line, 20 m steps east. Sum of segments = 100 m.
    pts = [destination_point(HERE, d, 90.0) for d in range(0, 101, 20)]
    log.start("line", now=0.0)
    t = 0.0
    for p in pts:
        log.update(p, sog_kn=2.0, now=t)
        t += 1.0
    trip = log.current
    assert trip is not None
    expected = sum(haversine_m(a, b) for a, b in zip(pts, pts[1:]))
    assert trip.distance_m == pytest.approx(expected, rel=1e-6)
    assert len(trip.points) == len(pts)


def test_min_distance_filter_skips_close_points(log):
    log.start(None, now=0.0)
    log.update(HERE, sog_kn=1.0, now=0.0)
    log.update(destination_point(HERE, 3.0, 90.0), sog_kn=1.0, now=1.0)  # too close
    log.update(destination_point(HERE, 30.0, 90.0), sog_kn=1.0, now=2.0)  # kept
    assert len(log.current.points) == 2
    assert log.current.distance_m == pytest.approx(30.0, abs=0.1)


def test_max_and_avg_speed(log):
    log.start(None, now=0.0)
    p1 = HERE
    p2 = destination_point(HERE, 100.0, 90.0)
    log.update(p1, sog_kn=1.0, now=0.0)
    log.update(p2, sog_kn=4.0, now=50.0)  # 100 m in 50 s
    trip = log.current
    assert trip.max_speed_kn == pytest.approx(4.0)
    # 100 m / 50 s = 2 m/s -> knots
    assert trip.avg_speed_kn(now=50.0) == pytest.approx(mps_to_knots(2.0), rel=1e-6)
    assert trip.duration_s(now=50.0) == pytest.approx(50.0)


# ---------------------------------------------------------------------- #
# Persistence + list + get + delete + GPX
# ---------------------------------------------------------------------- #
def test_stop_persists_and_lists(log, tmp_path):
    log.start("outing", now=0.0)
    log.update(HERE, sog_kn=1.0, now=0.0)
    log.update(destination_point(HERE, 40.0, 90.0), sog_kn=1.0, now=10.0)
    saved = log.stop(now=20.0)
    assert saved is not None
    assert log.current is None

    listing = log.list_trips()
    assert len(listing) == 1
    assert listing[0]["name"] == "outing"
    assert "points" not in listing[0]  # summary only
    assert listing[0]["point_count"] == 2

    full = log.get_trip(saved.id)
    assert full is not None
    assert len(full["points"]) == 2
    assert full["points"][0] == [HERE.lat, HERE.lon]


def test_gpx_contains_the_points(log):
    log.start("gpxtrip", now=0.0)
    log.update(HERE, sog_kn=1.0, now=0.0)
    log.update(destination_point(HERE, 40.0, 90.0), sog_kn=1.0, now=10.0)
    saved = log.stop(now=20.0)
    gpx = log.gpx(saved.id)
    assert gpx is not None
    assert "<gpx" in gpx and "<trk>" in gpx and "<trkseg>" in gpx
    assert f'lat="{HERE.lat}"' in gpx
    assert "gpxtrip" in gpx
    # one trkpt per recorded point
    assert gpx.count("<trkpt") == 2


def test_delete(log):
    log.start(None, now=0.0)
    log.update(HERE, sog_kn=1.0, now=0.0)
    saved = log.stop(now=1.0)
    assert log.delete_trip(saved.id) is True
    assert log.get_trip(saved.id) is None
    assert log.delete_trip(saved.id) is False  # already gone
    assert log.list_trips() == []


def test_gpx_missing_trip_is_none(log):
    assert log.gpx("nope") is None
    assert log.get_trip("nope") is None


# ---------------------------------------------------------------------- #
# Auto start / stop via the injected clock
# ---------------------------------------------------------------------- #
def test_auto_start_when_making_way(tmp_path):
    log = TripLog(
        str(tmp_path), min_distance_m=5.0, auto=True,
        start_speed_kn=0.5, idle_timeout_s=100.0,
    )
    # Idle below threshold -> no trip.
    log.update(HERE, sog_kn=0.1, now=0.0)
    assert log.current is None
    # Makes way -> auto-starts.
    log.update(HERE, sog_kn=1.0, now=1.0)
    assert log.current is not None
    assert log.current.auto is True


def test_auto_stop_after_idle_timeout(tmp_path):
    log = TripLog(
        str(tmp_path), min_distance_m=5.0, auto=True,
        start_speed_kn=0.5, idle_timeout_s=100.0,
    )
    log.update(HERE, sog_kn=1.0, now=0.0)  # auto-start
    assert log.current is not None
    p2 = destination_point(HERE, 50.0, 90.0)
    log.update(p2, sog_kn=1.0, now=50.0)  # still moving
    assert log.current is not None
    # Idle for < timeout: still active.
    log.update(p2, sog_kn=0.0, now=120.0)
    assert log.current is not None
    # Idle reaches the timeout (100 s since last moving at t=50): auto-stop.
    log.update(p2, sog_kn=0.0, now=151.0)
    assert log.current is None
    # And it was persisted.
    assert len(log.list_trips()) == 1


def test_manual_start_overrides_and_persists_previous(tmp_path):
    log = TripLog(str(tmp_path), min_distance_m=5.0, auto=False)
    log.start("first", now=0.0)
    log.update(HERE, sog_kn=1.0, now=0.0)
    log.start("second", now=10.0)  # should finalize "first"
    assert log.current.name == "second"
    names = {t["name"] for t in log.list_trips()}
    assert "first" in names


def test_trip_to_gpx_escapes_name():
    gpx = trip_to_gpx({"id": "x", "name": "A & B", "points": [[1.0, 2.0]]})
    assert "A &amp; B" in gpx
    assert 'lat="1.0"' in gpx


# ---------------------------------------------------------------------- #
# Fix 1: live duration uses monotonic clock, not wall clock
# ---------------------------------------------------------------------- #
def test_wall_clock_step_does_not_corrupt_live_duration(tmp_path):
    """An NTP-style wall-clock jump mid-trip must not inflate the live duration.

    Scenario: boat is underway, NTP syncs and the wall clock leaps forward by
    one hour.  The live duration (snapshot / avg_speed) must still reflect the
    real elapsed time as measured by the monotonic clock, not the wall-clock
    delta.
    """
    mono = {"t": 0.0}
    wall = {"t": 1_600_000_000.0}  # arbitrary wall-clock epoch

    log = TripLog(
        str(tmp_path),
        min_distance_m=5.0,
        auto=False,
        mono_fn=lambda: mono["t"],
    )

    log.start("ntp_test", now=wall["t"])

    # Advance both clocks by 60 s (normal operation).
    mono["t"] += 60.0
    wall["t"] += 60.0
    log.update(HERE, sog_kn=1.0, now=wall["t"])

    snap = log.snapshot(now=wall["t"])
    assert snap["duration_s"] == pytest.approx(60.0), (
        "duration should be 60 s after 60 s of real elapsed time"
    )

    # NTP/GPS step: wall clock jumps forward 1 hour; monotonic is unaffected.
    wall["t"] += 3600.0
    # mono["t"] stays at 60.0 — monotonic never jumps.

    snap = log.snapshot(now=wall["t"])
    # Without the fix this would return ~3660 s (wall-clock delta).
    # With the fix, monotonic governs: still 60 s.
    assert snap["duration_s"] == pytest.approx(60.0), (
        "duration must remain 60 s after a wall-clock NTP step"
    )
    # avg_speed must also be unaffected (it is derived from duration).
    # distance is 0 (only one point) so avg_speed is 0 — just verify no crash.
    assert snap["avg_speed_kn"] == pytest.approx(0.0, abs=0.01)


def test_monotonic_duration_after_resume_from_file(tmp_path):
    """A trip loaded from JSON has no monotonic anchor (_mono_start is None).

    duration_s must fall back gracefully to wall-clock for the pre-restart
    portion.  Once a caller sets the anchor (simulating what a resume would do),
    the monotonic delta is added correctly.
    """
    from vanchor.nav.trip import Trip

    # Simulate loading a trip that was started 5 minutes ago (wall-clock).
    wall_start = 1_600_000_000.0
    wall_now = wall_start + 300.0

    trip = Trip(id="trip-test", name="resumed", started_at=wall_start)
    # No _mono_start set — simulates the state right after loading from JSON.
    assert trip._mono_start is None

    # Should fall back to wall-clock: 300 s.
    assert trip.duration_s(wall_now) == pytest.approx(300.0)

    # Simulate a resume: set the monotonic anchor and wall-clock offset.
    mono_at_resume = 5000.0
    trip._wall_offset = wall_now - wall_start   # 300 s pre-restart portion
    trip._mono_start = mono_at_resume

    # After 30 more monotonic seconds: pre-restart (300) + post-restart (30) = 330.
    assert trip.duration_s(
        now=wall_now + 999.0,  # wall-clock irrelevant once anchor is set
        mono_now=mono_at_resume + 30.0,
    ) == pytest.approx(330.0)


# ---------------------------------------------------------------------- #
# Fix 2: atomic save uses os.replace
# ---------------------------------------------------------------------- #
def test_atomic_save_uses_os_replace(tmp_path, monkeypatch):
    """_save must write to a .tmp file first and use os.replace to move it.

    This guarantees that a crash mid-write leaves either the old complete file
    or the new complete file, never a partial one.
    """
    replaced: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy_replace(src: str, dst: str) -> None:
        replaced.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)

    log = TripLog(str(tmp_path), auto=False)
    log.start("atomic", now=0.0)
    log.update(HERE, sog_kn=1.0, now=0.0)
    log.stop(now=10.0)

    assert len(replaced) == 1, "os.replace must be called exactly once per save"
    src, dst = replaced[0]
    assert src.endswith(".tmp"), "source must be the .tmp staging file"
    assert not os.path.exists(src), "temp file must be gone after the replace"
    assert os.path.isfile(dst), "final trip file must exist after the replace"

    # Verify the final file is valid JSON (not corrupted / truncated).
    with open(dst, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["name"] == "atomic"
    assert data["duration_s"] == pytest.approx(10.0)


# ---------------------------------------------------------------------- #
# Runtime + REST integration
# ---------------------------------------------------------------------- #
def _runtime(tmp_path, **control):
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path)
    for k, v in control.items():
        setattr(cfg.control, k, v)
    # Deterministic clock for the runtime.
    return Runtime(cfg)


def test_runtime_trip_commands_and_telemetry(tmp_path):
    clock = {"t": 1000.0}
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path)
    cfg.control.auto_trip = False
    rt = Runtime(cfg, now_fn=lambda: clock["t"])

    tel = rt.telemetry()
    assert tel["trip"] == {
        "active": False, "name": None, "distance_m": 0.0,
        "duration_s": 0.0, "avg_speed_kn": 0.0, "max_speed_kn": 0.0,
    }

    rt.handle_command({"type": "trip_start", "name": "demo"})
    tel = rt.telemetry()
    assert tel["trip"]["active"] is True
    assert tel["trip"]["name"] == "demo"

    rt.handle_command({"type": "trip_stop"})
    assert rt.telemetry()["trip"]["active"] is False
    assert len(rt.trip_list()) == 1
    assert rt.trip_list()[0]["name"] == "demo"


def test_rest_endpoints(tmp_path):
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path)
    cfg.control.auto_trip = False
    rt = Runtime(cfg)
    app = create_app(rt)
    with TestClient(app) as c:
        rt.trip_start("rest")
        # give it a couple of points
        rt.trip.update(HERE, 1.0, rt._now_fn())
        rt.trip.update(destination_point(HERE, 30.0, 90.0), 1.0, rt._now_fn())
        rt.trip_stop()

        listing = c.get("/api/trips").json()["trips"]
        assert len(listing) == 1
        tid = listing[0]["id"]

        full = c.get(f"/api/trips/{tid}").json()
        assert len(full["points"]) >= 1

        gpx = c.get(f"/api/trips/{tid}.gpx")
        assert gpx.status_code == 200
        assert gpx.headers["content-type"].startswith("application/gpx+xml")
        assert "<trk>" in gpx.text

        assert c.get("/api/trips/nope").status_code == 404
        assert c.get("/api/trips/nope.gpx").status_code == 404

        assert c.delete(f"/api/trips/{tid}").json() == {"ok": True}
        assert c.get("/api/trips").json()["trips"] == []
