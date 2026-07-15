"""Periodic-safety-supervisor tests (review findings M2/H4/#7).

The side-effectful safety evaluations that used to live in ``Runtime.telemetry()``
were extracted into a dedicated ~1 Hz supervisor task so that:

* ``telemetry()`` is a PURE snapshot -- polling ``GET /api/state`` no longer
  double-records soundings or perturbs failsafe timing;
* safety keeps running REGARDLESS of replay mode and connected-client count;
* one exception in an evaluator can't kill the safety loop.

These drive the runtime methods directly (a TestClient against a full Runtime can
hang on depth data) except for the one broadcaster smoke test.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.core.config import AppConfig
from vanchor.core.models import ControlModeName, GeoPoint, GpsFix
from vanchor.ui.server import create_app


def _underway_runtime(now: list[float]) -> Runtime:
    rt = Runtime(mono_fn=lambda: now[0])
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    rt.state.mode = ControlModeName.HEADING_HOLD  # making way
    return rt


# --------------------------------------------------------------------------- #
# Supervisor runs safety regardless of clients / replay
# --------------------------------------------------------------------------- #
def test_supervisor_engages_link_failsafe_with_no_client():
    now = [1000.0]
    rt = _underway_runtime(now)
    rt.config.safety.link_loss_timeout_s = 10.0
    rt.config.safety.link_loss_continue_mission = False  # test the hold path
    rt.client_connected()
    rt.client_disconnected()
    now[0] = 1011.0  # past the timeout
    assert rt._ui_clients == 0
    rt._supervise_once()
    assert rt._link_failsafe_engaged
    assert rt.state.mode == ControlModeName.ANCHOR_HOLD


def test_supervisor_engages_link_failsafe_during_replay():
    """Replay swaps the FRAME shown to clients (telemetry() early-returns), but
    live safety evaluation must keep running via the supervisor."""
    now = [1000.0]
    rt = _underway_runtime(now)
    rt.config.safety.link_loss_timeout_s = 10.0
    rt.config.safety.link_loss_continue_mission = False  # test the hold path
    rt.client_connected()
    rt.client_disconnected()
    rt.replay.active = True  # telemetry() would now early-return the replay frame
    now[0] = 1011.0
    rt._supervise_once()
    assert rt._link_failsafe_engaged
    assert rt.state.mode == ControlModeName.ANCHOR_HOLD


def test_supervisor_captures_launch_and_updates_trip():
    now = [1000.0]
    rt = Runtime(now_fn=lambda: now[0])
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    assert rt.state.launch is None
    rt._supervise_once()
    assert rt.state.launch == GeoPoint(59.0, 18.0)


# --------------------------------------------------------------------------- #
# telemetry() is a pure snapshot (no side effects)
# --------------------------------------------------------------------------- #
def test_telemetry_does_not_capture_launch_or_record_soundings(tmp_path):
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    rt.state.launch = None
    rt.state.depth_m = 5.0  # a sounding WOULD record if telemetry did it
    n0 = len(rt.depth_map.points)

    t1 = rt.telemetry()
    t2 = rt.telemetry()

    # Pure snapshot: launch not captured, no soundings recorded, trip untouched.
    assert rt.state.launch is None
    assert len(rt.depth_map.points) == n0
    assert t1["trip"] == t2["trip"]
    assert t2["trip"]["distance_m"] == 0.0
    # But the snapshot still EXPOSES the (read-only) depth + trip blocks.
    assert "depth_points" in t2 and "depth_count" in t2
    assert "trip" in t2


def test_api_state_twice_is_side_effect_free(tmp_path):
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    rt.state.depth_m = 5.0
    app = create_app(rt)
    # Do NOT enter the lifespan (that would start the broadcaster+supervisor and
    # can hang on depth data); call the state endpoint's runtime path directly.
    n0 = len(rt.depth_map.points)
    launch0 = rt.state.launch
    rt.telemetry()
    rt.telemetry()
    assert len(rt.depth_map.points) == n0
    assert rt.state.launch == launch0


# --------------------------------------------------------------------------- #
# Depth-sounding accumulation seam (broadcaster-driven, ~5 Hz)
# --------------------------------------------------------------------------- #
def test_record_depth_sounding_accumulates(tmp_path):
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    rt.state.depth_m = 4.0
    n0 = len(rt.depth_map.points)
    rt.record_depth_sounding()
    assert len(rt.depth_map.points) == n0 + 1


def test_record_depth_sounding_noop_during_replay(tmp_path):
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    rt.state.depth_m = 4.0
    rt.replay.active = True
    n0 = len(rt.depth_map.points)
    rt.record_depth_sounding()
    assert len(rt.depth_map.points) == n0


# --------------------------------------------------------------------------- #
# Off-hot-path depth persistence (finding M3)
# --------------------------------------------------------------------------- #
async def test_maybe_persist_depth_offloads_and_guards(tmp_path):
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    rt._depth_saved_n = len(rt.depth_map.points)

    # Fewer than 25 new soundings -> no save yet.
    saved: list[str] = []
    rt.depth_map.save = lambda path: saved.append(path)  # type: ignore[assignment]
    await rt._maybe_persist_depth()
    assert saved == []

    # 30 new soundings -> a save, run OFF the event loop, and the watermark moves.
    for i in range(30):
        rt.depth_map.points.append((59.0 + i * 0.001, 18.0, 5.0))
    await rt._maybe_persist_depth()
    assert saved  # persisted
    assert rt._depth_saved_n == len(rt.depth_map.points)

    # In-flight guard: no overlapping save while one is marked in flight.
    saved.clear()
    rt.depth_map.points.append((60.0, 18.0, 5.0))
    rt._depth_saved_n = 0  # would otherwise trigger a save
    rt._depth_save_in_flight = True
    await rt._maybe_persist_depth()
    assert saved == []


# --------------------------------------------------------------------------- #
# Supervisor is exception-proof
# --------------------------------------------------------------------------- #
def test_supervise_once_survives_failing_evaluator():
    """A raising evaluator must not stop later steps nor raise out of the pass."""
    now = [1000.0]
    rt = _underway_runtime(now)
    rt.config.safety.link_loss_timeout_s = 10.0
    rt.config.safety.link_loss_continue_mission = False  # test the hold path
    rt.client_connected()
    rt.client_disconnected()
    now[0] = 1011.0

    def boom():
        raise RuntimeError("injected evaluator failure")

    # evaluate_rtl_recommend runs BEFORE evaluate_link_failsafe in the pass.
    rt.evaluate_rtl_recommend = boom  # type: ignore[assignment]
    rt._supervise_once()  # must not raise
    # The step after the failing one still ran.
    assert rt._link_failsafe_engaged
    assert rt.state.mode == ControlModeName.ANCHOR_HOLD


async def test_supervisor_loop_survives_failures_and_is_cancelable():
    rt = Runtime()

    def boom():
        raise RuntimeError("repeated failure")

    rt.evaluate_rtl_recommend = boom  # type: ignore[assignment]
    task = asyncio.ensure_future(rt._run_supervisor(period_s=0.01))
    await asyncio.sleep(0.05)
    assert not task.done()  # kept running despite repeated per-tick failures
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------- #
# health.devices block (item 6)
# --------------------------------------------------------------------------- #
def test_health_devices_omitted_for_sim_only_runtime():
    rt = Runtime()  # sim devices lack healthy/last_data_monotonic
    health = rt.telemetry()["health"]
    assert "devices" not in health


def test_health_devices_shape_with_health_reporting_device():
    rt = Runtime()

    class _FakeSerialDev:
        healthy = True
        last_data_monotonic = None

    rt.gps = _FakeSerialDev()
    # Never-received stamp -> null age.
    dev = rt._device_health(now=100.0)
    assert dev["gps"] == {"healthy": True, "data_age_s": None}
    # With a stamp, age is computed against the injected monotonic now.
    rt.gps.last_data_monotonic = 90.0
    dev = rt._device_health(now=100.0)
    assert dev["gps"] == {"healthy": True, "data_age_s": 10.0}
    # A falsy-but-present healthy is reported (not confused with "no attr").
    rt.gps.healthy = False
    assert rt._device_health(now=100.0)["gps"]["healthy"] is False
    # And it now shows up in the telemetry health block.
    assert "devices" in rt.telemetry()["health"]


# --------------------------------------------------------------------------- #
# Broadcaster still emits frames after the refactor
# --------------------------------------------------------------------------- #
def test_broadcaster_emits_frames(tmp_path):
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path)
    app = create_app(Runtime(cfg), telemetry_hz=20.0)
    with TestClient(app) as c:
        with c.websocket_connect("/ws") as ws:
            msg = ws.receive_json()
            assert "mode" in msg


# --------------------------------------------------------------------------- #
# Opt-in: continue the mission on link loss (pocket-the-phone workflow)
# --------------------------------------------------------------------------- #
def test_link_loss_continue_mission_keeps_guided_mode():
    now = [1000.0]
    rt = _underway_runtime(now)                       # HEADING_HOLD, making way
    rt.config.safety.link_loss_timeout_s = 10.0
    rt.config.safety.link_loss_continue_mission = True
    rt.client_connected()
    rt.client_disconnected()
    now[0] = 1011.0
    rt._supervise_once()
    assert rt._link_failsafe_engaged                   # latched (fires once)
    assert rt.state.mode == ControlModeName.HEADING_HOLD  # mission continues


def test_link_loss_manual_still_stops_even_with_continue_mission():
    """The MANUAL deadman is safety floor: the opt-in must not weaken it."""
    from vanchor.core.models import MotorCommand
    now = [1000.0]
    rt = Runtime(mono_fn=lambda: now[0])
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    rt.state.mode = ControlModeName.MANUAL
    rt.state.motor_command = MotorCommand(thrust=0.5)  # driving by hand
    rt.config.safety.link_loss_timeout_s = 10.0
    rt.config.safety.link_loss_continue_mission = True
    rt.client_connected()
    rt.client_disconnected()
    now[0] = 1011.0
    rt._supervise_once()
    assert rt._link_failsafe_engaged
    assert rt.state.mode == ControlModeName.MANUAL     # stop lands in manual...
    cmd = rt.controller.control_tick(0.2)              # next tick applies the stop
    assert cmd.thrust == 0.0                           # ...with thrust CUT
