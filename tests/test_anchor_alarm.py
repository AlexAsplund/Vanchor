"""Tests for the passive anchor alarm (adoption #10).

Motor-OFF GPS watch circle: AnchorAlarmStore persistence, AnchorAlarmWatcher
evaluate/latch/hysteresis/stale/on_breach, and Runtime integration tests.

Memory note: do NOT wrap Runtime in TestClient — use Runtime methods directly
and call runtime.evaluate_anchor_alarm() to exercise the supervisor step.
"""
from __future__ import annotations

import json
import time

import pytest

from vanchor.app import Runtime
from vanchor.core.anchor_alarm import AnchorAlarmStore, AnchorAlarmWatcher
from vanchor.core import contract
from vanchor.core.config import load
from vanchor.core.geo import destination_point
from vanchor.core.models import ControlModeName, GeoPoint, GpsFix


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _runtime(tmp_path) -> Runtime:
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    return Runtime(cfg)


def _fix(rt: Runtime, lat: float, lon: float) -> None:
    """Inject a fresh GPS fix into the runtime state."""
    rt.state.fix = GpsFix(point=GeoPoint(lat, lon))
    rt.state.fix_received_mono = time.monotonic()


# --------------------------------------------------------------------------- #
# Store tests
# --------------------------------------------------------------------------- #

def test_store_roundtrip_and_atomic(tmp_path):
    """set() → file exists, no .tmp residue, fresh store reloads identical values."""
    store = AnchorAlarmStore(str(tmp_path))
    assert store.armed is False
    assert store.lat is None
    assert store.lon is None

    p = GeoPoint(59.0, 18.0)
    now = 1_700_000_000.0
    store.set(p, 40.0, now)

    path = tmp_path / "anchor_alarm.json"
    assert path.exists()
    assert not (tmp_path / "anchor_alarm.json.tmp").exists()

    reloaded = AnchorAlarmStore(str(tmp_path))
    assert reloaded.armed is True
    assert abs(reloaded.lat - 59.0) < 1e-9
    assert abs(reloaded.lon - 18.0) < 1e-9
    assert abs(reloaded.radius_m - 40.0) < 1e-9
    assert abs(reloaded.set_at - now) < 1e-6


def test_store_tolerates_missing_and_corrupt_file(tmp_path):
    """No file → disarmed; garbage bytes → disarmed, no raise."""
    store = AnchorAlarmStore(str(tmp_path))
    assert store.armed is False  # fresh install

    corrupt = tmp_path / "anchor_alarm.json"
    corrupt.write_bytes(b"NOT JSON !!!\x00\xff")
    store2 = AnchorAlarmStore(str(tmp_path))
    assert store2.armed is False


def test_store_clear_keeps_last_circle_but_disarms(tmp_path):
    store = AnchorAlarmStore(str(tmp_path))
    p = GeoPoint(60.0, 25.0)
    store.set(p, 50.0, 1234567890.0)
    assert store.armed is True

    store.clear()
    assert store.armed is False
    # Last circle coordinates preserved for re-arm UX.
    assert abs(store.lat - 60.0) < 1e-9
    assert abs(store.lon - 25.0) < 1e-9
    assert abs(store.radius_m - 50.0) < 1e-9

    # Reload also disarmed.
    reloaded = AnchorAlarmStore(str(tmp_path))
    assert reloaded.armed is False


# --------------------------------------------------------------------------- #
# Watcher tests (pure, no Runtime)
# --------------------------------------------------------------------------- #

def _watcher(tmp_path, radius_m: float = 30.0, lat: float = 59.0, lon: float = 18.0):
    store = AnchorAlarmStore(str(tmp_path))
    w = AnchorAlarmWatcher(store, stale_fix_s=30.0)
    w.set(GeoPoint(lat, lon), radius_m, now=0.0)
    return w


def test_disarmed_watcher_is_quiet(tmp_path):
    """Disarmed watcher returns armed=False, firing=False regardless of position."""
    store = AnchorAlarmStore(str(tmp_path))
    w = AnchorAlarmWatcher(store)
    snap = w.evaluate(GeoPoint(59.0, 18.0), 0.0)
    assert snap["armed"] is False
    assert snap["firing"] is False
    assert snap["breach_count"] == 0


def test_breach_latches_and_counts(tmp_path):
    """Inside → not firing; outside at 2×radius → firing, breach_count=1; second tick outside → still 1."""
    w = _watcher(tmp_path, radius_m=30.0, lat=59.0, lon=18.0)
    center = GeoPoint(59.0, 18.0)

    # Inside — no breach.
    snap_in = w.evaluate(center, 0.0)
    assert snap_in["firing"] is False
    assert snap_in["breach_count"] == 0

    # 60 m east — 2×radius, well outside.
    outside = destination_point(center, 60.0, 90.0)
    snap_out = w.evaluate(outside, 0.0)
    assert snap_out["firing"] is True
    assert snap_out["breach_count"] == 1

    # Second evaluate outside — still 1 (no re-fire).
    snap_out2 = w.evaluate(outside, 0.0)
    assert snap_out2["breach_count"] == 1


def test_breach_clears_with_hysteresis(tmp_path):
    """From firing: 0.9×radius → still firing; 0.5×radius → cleared."""
    w = _watcher(tmp_path, radius_m=30.0, lat=59.0, lon=18.0)
    center = GeoPoint(59.0, 18.0)

    # Breach first.
    outside = destination_point(center, 60.0, 90.0)
    w.evaluate(outside, 0.0)
    assert w.firing is True

    # 0.9×30 = 27 m — inside radius but above 0.8×30=24 m hysteresis band → still firing.
    near = destination_point(center, 27.0, 90.0)
    snap_near = w.evaluate(near, 0.0)
    assert snap_near["firing"] is True

    # 0.5×30 = 15 m — below hysteresis band → cleared.
    close = destination_point(center, 15.0, 90.0)
    snap_close = w.evaluate(close, 0.0)
    assert snap_close["firing"] is False


def test_on_breach_hook_fires_once_per_breach_and_is_isolated(tmp_path):
    """Two hooks: first raises, second still called; called exactly once per transition."""
    w = _watcher(tmp_path, radius_m=30.0, lat=59.0, lon=18.0)
    center = GeoPoint(59.0, 18.0)
    outside = destination_point(center, 60.0, 90.0)

    calls: list[dict] = []

    def bad_hook(snap):
        raise RuntimeError("intentional test error")

    def good_hook(snap):
        calls.append(snap)

    w.on_breach.append(bad_hook)
    w.on_breach.append(good_hook)

    # First breach → good_hook called once despite bad_hook raising.
    w.evaluate(outside, 0.0)
    assert len(calls) == 1

    # Second evaluate outside → no re-fire (already firing).
    w.evaluate(outside, 0.0)
    assert len(calls) == 1

    # Come back inside to clear.
    close = destination_point(center, 5.0, 90.0)
    w.evaluate(close, 0.0)
    assert w.firing is False

    # Second breach.
    w.evaluate(outside, 0.0)
    assert len(calls) == 2


def test_stale_fix_flags_but_keeps_latch(tmp_path):
    """Armed + breach-fired; evaluate(None) and (pos, age=999) → stale, firing still True."""
    w = _watcher(tmp_path, radius_m=30.0, lat=59.0, lon=18.0)
    center = GeoPoint(59.0, 18.0)
    outside = destination_point(center, 60.0, 90.0)

    # Breach first.
    w.evaluate(outside, 0.0)
    assert w.firing is True
    dist_before = w.distance_m

    # No position.
    snap1 = w.evaluate(None, None)
    assert snap1["stale"] is True
    assert snap1["firing"] is True
    assert snap1["distance_m"] == (round(dist_before, 1) if dist_before is not None else None)

    # Position present but very old fix.
    snap2 = w.evaluate(outside, 999.0)
    assert snap2["stale"] is True
    assert snap2["firing"] is True


def test_set_clamps_radius(tmp_path):
    """radius < 5 → 5.0; radius > 500 → 500.0."""
    store = AnchorAlarmStore(str(tmp_path))
    w = AnchorAlarmWatcher(store)
    center = GeoPoint(59.0, 18.0)

    snap_low = w.set(center, 1.0, now=0.0)
    assert snap_low["radius_m"] == 5.0

    snap_high = w.set(center, 10_000.0, now=0.0)
    assert snap_high["radius_m"] == 500.0


# --------------------------------------------------------------------------- #
# Runtime integration tests
# --------------------------------------------------------------------------- #

def test_command_set_uses_current_position_and_persists(tmp_path):
    rt = _runtime(tmp_path)
    _fix(rt, 59.0, 18.0)
    rt.handle_command({"type": "anchor_alarm_set", "radius_m": 40})

    snap = rt.anchor_alarm.snapshot()
    assert snap["armed"] is True
    assert abs(snap["lat"] - 59.0) < 1e-6
    assert abs(snap["lon"] - 18.0) < 1e-6

    path = tmp_path / "anchor_alarm.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["armed"] is True


def test_command_set_without_fix_is_refused(tmp_path):
    """No GPS fix → command ignored, alarm stays disarmed, no armed=True file."""
    rt = _runtime(tmp_path)
    # Don't set a fix — state.position will be None or null.
    rt.handle_command({"type": "anchor_alarm_set", "radius_m": 30})
    assert rt.anchor_alarm.store.armed is False
    path = tmp_path / "anchor_alarm.json"
    # Either the file doesn't exist, or it says armed=false.
    if path.exists():
        data = json.loads(path.read_text())
        assert data.get("armed") is not True


def test_command_set_with_explicit_latlon(tmp_path):
    """Explicit lat/lon bypasses the current-position check."""
    rt = _runtime(tmp_path)
    # No GPS fix injected.
    rt.handle_command({"type": "anchor_alarm_set", "lat": 60.0, "lon": 25.0, "radius_m": 50})
    snap = rt.anchor_alarm.snapshot()
    assert snap["armed"] is True
    assert abs(snap["lat"] - 60.0) < 1e-6
    assert abs(snap["lon"] - 25.0) < 1e-6


def test_alarm_survives_restart(tmp_path):
    """Arm in runtime A; runtime B on the same dir reloads armed + evaluate works."""
    rt1 = _runtime(tmp_path)
    _fix(rt1, 59.0, 18.0)
    rt1.handle_command({"type": "anchor_alarm_set", "radius_m": 35})
    assert rt1.anchor_alarm.store.armed is True

    rt2 = _runtime(tmp_path)
    assert rt2.anchor_alarm.store.armed is True
    assert abs(rt2.anchor_alarm.store.lat - 59.0) < 1e-6
    assert abs(rt2.anchor_alarm.store.radius_m - 35.0) < 1e-6

    # evaluate works immediately on rt2 (returns a valid snapshot).
    _fix(rt2, 59.0, 18.0)
    snap = rt2.evaluate_anchor_alarm()
    assert snap["armed"] is True


def test_passive_watch_never_touches_motor_or_mode(tmp_path):
    """THE safety test: no motor commands, no mode changes while alarm watches."""
    rt = _runtime(tmp_path)
    _fix(rt, 59.0, 18.0)
    rt.handle_command({"type": "anchor_alarm_set", "lat": 59.0, "lon": 18.0, "radius_m": 30})

    # Record calls to controller.handle_command and motor.apply.
    motor_calls: list = []
    cmd_calls: list = []

    original_handle = rt.controller.handle_command
    original_motor_apply = rt.controller.motor.apply

    def mock_handle_command(cmd):
        cmd_calls.append(cmd)
        return original_handle(cmd)

    def mock_motor_apply(*args, **kwargs):
        motor_calls.append(args)
        return original_motor_apply(*args, **kwargs)

    rt.controller.handle_command = mock_handle_command
    rt.controller.motor.apply = mock_motor_apply

    initial_mode = rt.state.mode

    # Place the boat far outside the watch circle.
    far = destination_point(GeoPoint(59.0, 18.0), 200.0, 90.0)
    rt.state.fix = GpsFix(point=far)
    rt.state.fix_received_mono = time.monotonic()

    # Run the evaluator several times.
    for _ in range(5):
        rt.evaluate_anchor_alarm()

    assert rt.anchor_alarm.firing is True
    assert cmd_calls == [], f"Expected no controller commands, got: {cmd_calls}"
    assert motor_calls == [], f"Expected no motor.apply calls, got: {motor_calls}"
    assert rt.state.mode == initial_mode
    assert rt.state.motor_command.thrust == 0.0


def test_supervise_once_runs_the_alarm_step(tmp_path):
    """_supervise_once() must register and run evaluate_anchor_alarm."""
    rt = _runtime(tmp_path)
    center = GeoPoint(59.0, 18.0)
    rt.handle_command({
        "type": "anchor_alarm_set",
        "lat": center.lat, "lon": center.lon, "radius_m": 30,
    })

    # Move far outside.
    far = destination_point(center, 200.0, 90.0)
    rt.state.fix = GpsFix(point=far)
    rt.state.fix_received_mono = time.monotonic()

    assert rt.anchor_alarm.firing is False
    rt._supervise_once()
    assert rt.anchor_alarm.firing is True


def test_recover_engages_anchor_hold_at_alarm_point_and_disarms(tmp_path):
    """Recover: anchor_hold engages at the alarm point (not current position), alarm disarmed."""
    rt = _runtime(tmp_path)
    alarm_point = GeoPoint(59.0, 18.0)
    # Arm at alarm_point.
    rt.handle_command({
        "type": "anchor_alarm_set",
        "lat": alarm_point.lat, "lon": alarm_point.lon, "radius_m": 30,
    })
    # Boat is somewhere else.
    q = destination_point(alarm_point, 100.0, 90.0)
    _fix(rt, q.lat, q.lon)

    rt.handle_command({"type": "anchor_alarm_recover"})

    assert rt.state.mode == ControlModeName.ANCHOR_HOLD
    # Anchor set at the alarm point, not at Q.
    assert rt.state.anchor is not None
    assert abs(rt.state.anchor.lat - alarm_point.lat) < 1e-6
    assert abs(rt.state.anchor.lon - alarm_point.lon) < 1e-6

    # Alarm disarmed.
    assert rt.anchor_alarm.store.armed is False
    path = tmp_path / "anchor_alarm.json"
    data = json.loads(path.read_text())
    assert data["armed"] is False


def test_recover_refused_keeps_alarm_armed(tmp_path):
    """Controller refusing (no-op mock) → mode unchanged, alarm stays armed."""
    rt = _runtime(tmp_path)
    _fix(rt, 59.0, 18.0)
    rt.handle_command({"type": "anchor_alarm_set", "lat": 59.0, "lon": 18.0, "radius_m": 30})
    assert rt.anchor_alarm.store.armed is True

    # Monkeypatch handle_command to a no-op (simulates the device gate refusing).
    rt.controller.handle_command = lambda cmd: None

    rt.handle_command({"type": "anchor_alarm_recover"})

    # Mode must be unchanged (MANUAL) and alarm must stay armed.
    assert rt.state.mode == ControlModeName.MANUAL
    assert rt.anchor_alarm.store.armed is True


def test_recover_without_armed_alarm_is_noop(tmp_path):
    """Recover when alarm is not armed is a no-op (no raise, no mode change)."""
    rt = _runtime(tmp_path)
    _fix(rt, 59.0, 18.0)
    initial_mode = rt.state.mode
    # No alarm armed.
    rt.handle_command({"type": "anchor_alarm_recover"})
    assert rt.state.mode == initial_mode


def test_telemetry_exposes_anchor_alarm_block(tmp_path):
    """telemetry()['anchor_alarm'] has exactly the snapshot keys; calling twice is pure."""
    rt = _runtime(tmp_path)
    t1 = rt.telemetry()
    assert "anchor_alarm" in t1
    aa = t1["anchor_alarm"]
    expected_keys = {"armed", "lat", "lon", "radius_m", "distance_m",
                     "firing", "stale", "fix_age_s", "set_at", "breach_count"}
    assert set(aa.keys()) == expected_keys

    # Calling telemetry() twice must not change firing or breach_count.
    t2 = rt.telemetry()
    assert t2["anchor_alarm"]["firing"] == t1["anchor_alarm"]["firing"]
    assert t2["anchor_alarm"]["breach_count"] == t1["anchor_alarm"]["breach_count"]


def test_contract_declares_alarm_commands_and_telemetry(tmp_path):
    """Contract declares the telemetry field and all three commands."""
    assert "anchor_alarm" in contract.TELEMETRY_FIELDS
    assert "anchor_alarm_set" in contract.COMMANDS
    assert "anchor_alarm_clear" in contract.COMMANDS
    assert "anchor_alarm_recover" in contract.COMMANDS
