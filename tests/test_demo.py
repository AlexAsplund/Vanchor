"""Demo mode (`vanchor --demo`): config posture, seeded scenario, readonly."""

from __future__ import annotations

import asyncio
import shutil

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime, apply_demo_mode, demo_route_waypoints
from vanchor.core.config import AppConfig, load
from vanchor.core.models import ControlModeName, GeoPoint, GpsFix
from vanchor.ui.server import create_app


# ---- helpers (cloned from tests/test_api.py) ------------------------------- #


def _recv_until(ws, pred, tries=60):
    """Receive frames until ``pred(msg)`` is truthy; return that msg (or None)."""
    for _ in range(tries):
        msg = ws.receive_json()
        if pred(msg):
            return msg
    return None


def _role_msg(ws, tries=60):
    """Receive frames until a ``{type:"role"}`` message arrives; return it."""
    return _recv_until(ws, lambda m: m.get("type") == "role", tries=tries)


# ---- fixtures -------------------------------------------------------------- #


@pytest.fixture()
def demo_ro_client(tmp_path, monkeypatch):
    """TestClient with demo.enabled=True and demo.readonly=True."""
    monkeypatch.setenv("VANCHOR_ALLOWED_HOSTS", "testserver")
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    cfg.demo.enabled = True
    cfg.demo.readonly = True
    app = create_app(Runtime(cfg))
    with TestClient(app) as c:
        yield c


# ---- Config / override tests (pure, no server) ----------------------------- #


def test_demo_config_defaults_off():
    """Default config has demo.enabled=False, demo.readonly=False."""
    cfg = load(None)
    assert cfg.demo.enabled is False
    assert cfg.demo.readonly is False


def test_demo_config_from_dict():
    """AppConfig.from_dict round-trips demo keys; unknown keys are ignored."""
    cfg = AppConfig.from_dict({"demo": {"enabled": True, "scenario": "anchor"}})
    assert cfg.demo.enabled is True
    assert cfg.demo.scenario == "anchor"
    # Unknown key should not raise.
    cfg2 = AppConfig.from_dict({"demo": {"enabled": True, "unknown_key": "ignored"}})
    assert cfg2.demo.enabled is True


def test_apply_demo_mode_forces_sim(tmp_path):
    """apply_demo_mode overrides hardware, nmea_tcp, watchdog and sets lake position."""
    cfg = load(None)
    cfg.hardware.enabled = True
    cfg.hardware.gps_source = "serial"
    cfg.nmea_tcp.enabled = True
    cfg.watchdog.enabled = True

    apply_demo_mode(cfg, data_dir=str(tmp_path))

    assert cfg.hardware.enabled is False
    for device in ("gps", "compass", "depth", "motor"):
        assert cfg.hardware.source(device) == "sim", f"{device} source should be sim"
    assert cfg.nmea_tcp.enabled is False
    assert cfg.watchdog.enabled is False
    assert cfg.sim.start_lat == cfg.demo.start_lat
    assert cfg.sim.time_scale == 1.0
    assert cfg.data_dir == str(tmp_path)


def test_apply_demo_mode_ephemeral_dir_and_chart_symlink(tmp_path):
    """apply_demo_mode creates an ephemeral data dir and symlinks the depth chart."""
    # Create a source dir with a dummy depthchart.npz.
    src_dir = tmp_path / "src_data"
    src_dir.mkdir()
    (src_dir / "depthchart.npz").write_bytes(b"dummy")

    cfg = load(None)
    cfg.data_dir = str(src_dir)
    old_dir = cfg.data_dir

    apply_demo_mode(cfg)  # no data_dir arg -> mkdtemp
    try:
        assert cfg.data_dir != old_dir
        symlink = __import__("pathlib").Path(cfg.data_dir) / "depthchart.npz"
        assert symlink.is_symlink()
    finally:
        shutil.rmtree(cfg.data_dir, ignore_errors=True)


def test_env_var_demo(monkeypatch):
    """VANCHOR_DEMO=1 enables demo mode via env override."""
    monkeypatch.setenv("VANCHOR_DEMO", "1")
    cfg = load(None)
    assert cfg.demo.enabled is True


# ---- Scenario tests (Runtime methods directly) ----------------------------- #


def test_demo_route_waypoints_shape():
    """demo_route_waypoints returns 3 waypoints, all near the seed, names present."""
    wps = demo_route_waypoints(59.8779, 12.0293)
    assert len(wps) == 3
    for wp in wps:
        assert "name" in wp
        assert abs(wp["lat"] - 59.8779) < 0.012
        assert abs(wp["lon"] - 12.0293) < 0.012


def _give_fix(rt: Runtime, lat: float = 59.8779, lon: float = 12.0293) -> None:
    """Inject a GPS fix into the runtime state so position is not None."""
    rt.state.fix = GpsFix(point=GeoPoint(lat, lon))


def test_demo_scenario_engages_route(tmp_path):
    """_run_demo_scenario engages a looping waypoint route via handle_command."""
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    cfg.demo.enabled = True
    rt = Runtime(cfg)
    _give_fix(rt)

    asyncio.run(rt._run_demo_scenario())

    assert rt.state.mode == ControlModeName.WAYPOINT
    assert rt.state.route_loop is True
    assert len(rt.state.waypoints) == 3


def test_demo_scenario_anchor(tmp_path):
    """_run_demo_scenario with scenario=anchor engages anchor_hold."""
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    cfg.demo.enabled = True
    cfg.demo.scenario = "anchor"
    rt = Runtime(cfg)
    _give_fix(rt)

    asyncio.run(rt._run_demo_scenario())

    assert rt.state.mode == ControlModeName.ANCHOR_HOLD
    assert rt.state.anchor is not None


def test_demo_scenario_respects_existing_mode(tmp_path):
    """_run_demo_scenario bails out if a mode is already engaged (one-shot)."""
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    cfg.demo.enabled = True
    rt = Runtime(cfg)
    _give_fix(rt)
    # Pre-engage anchor_hold.
    rt.handle_command({"type": "anchor_hold", "radius_m": 8})
    assert rt.state.mode == ControlModeName.ANCHOR_HOLD

    asyncio.run(rt._run_demo_scenario())

    # Scenario did not change the already-engaged mode.
    assert rt.state.mode == ControlModeName.ANCHOR_HOLD


def test_telemetry_carries_demo_flags(tmp_path):
    """telemetry() exposes demo_mode and demo_readonly flags."""
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    cfg.demo.enabled = True
    cfg.demo.readonly = True
    rt = Runtime(cfg)
    t = rt.telemetry()
    assert t["demo_mode"] is True
    assert t["demo_readonly"] is True

    # Non-demo runtime: both flags are False.
    cfg2 = load(None)
    cfg2.data_dir = str(tmp_path)
    rt2 = Runtime(cfg2)
    t2 = rt2.telemetry()
    assert t2["demo_mode"] is False
    assert t2["demo_readonly"] is False


# ---- Readonly server tests (TestClient) ------------------------------------ #


def test_readonly_first_client_is_observer(demo_ro_client):
    """In demo-readonly mode the first WS client is observer (no helm assigned)."""
    with demo_ro_client.websocket_connect("/ws") as ws:
        ws.receive_json()  # snapshot
        role = _role_msg(ws)
        assert role is not None
        assert role["role"] == "observer"
        assert role["helm_present"] is False
        assert role.get("readonly") is True


def test_readonly_take_helm_denied(demo_ro_client):
    """take_helm is refused in demo-readonly mode."""
    with demo_ro_client.websocket_connect("/ws") as ws:
        ws.receive_json()  # snapshot
        _role_msg(ws)      # consume initial role
        ws.send_json({"type": "take_helm"})
        denied = _recv_until(ws, lambda m: m.get("type") == "role_denied")
        assert denied is not None
        assert "read-only" in denied.get("error", "")
        # Role stays observer.
        ws.send_json({"type": "ping"})
        # Wait for pong (confirms connection is still alive).
        pong = _recv_until(ws, lambda m: m.get("type") == "pong")
        assert pong is not None


def test_readonly_command_denied_stop_honoured(demo_ro_client):
    """Observer WS: non-stop commands are denied; stop is honoured (SAFETY FLOOR)."""
    with demo_ro_client.websocket_connect("/ws") as ws:
        ws.receive_json()  # snapshot
        _role_msg(ws)      # consume initial role
        # A mode-changing command should be denied.
        ws.send_json({"type": "heading_hold", "target_deg": 90})
        denied = _recv_until(ws, lambda m: m.get("type") == "role_denied")
        assert denied is not None
        # STOP must still work (seq=7).
        ws.send_json({"type": "stop", "seq": 7})
        ack = _recv_until(ws, lambda m: m.get("type") == "ack")
        assert ack is not None
        assert ack.get("seq") == 7


def test_readonly_rest_command_403(demo_ro_client):
    """REST: non-stop commands get 403; stop gets 200; mutating endpoints blocked; GET ok."""
    # Non-stop command -> 403.
    r = demo_ro_client.post("/api/command", json={"type": "heading_hold"})
    assert r.status_code == 403

    # stop -> 200 ok (SAFETY FLOOR).
    r = demo_ro_client.post("/api/command", json={"type": "stop"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Mutating /api/boat endpoint -> blocked by middleware.
    r = demo_ro_client.post("/api/boat", json={})
    assert r.status_code == 403

    # GET /api/state -> allowed.
    r = demo_ro_client.get("/api/state")
    assert r.status_code == 200


def test_demo_badge_in_shell(demo_ro_client):
    """The SIM-pill markup is present in the served shell (id renamed in task 3)."""
    r = demo_ro_client.get("/")
    assert r.status_code == 200
    assert 'id="sim-indicator"' in r.text


# ---- S1: demo inertness — Runtime must force sim regardless of config source #


def test_runtime_auto_applies_demo_mode_from_yaml(tmp_path):
    """S1: Runtime construction forces sim posture when demo.enabled=True even
    without --demo CLI flag (yaml/env source), so a real motor is never reached.
    """
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    cfg.demo.enabled = True
    # Simulate yaml/env source: hardware enabled + serial motor source set.
    cfg.hardware.enabled = True
    cfg.hardware.motor_source = "serial"
    cfg.nmea_tcp.enabled = True

    rt = Runtime(cfg)

    # apply_demo_mode must have been called: hardware off, all sources sim.
    assert rt.config.hardware.enabled is False
    assert rt.config.nmea_tcp.enabled is False
    for device in ("gps", "compass", "depth", "motor"):
        assert rt.config.hardware.source(device) == "sim", (
            f"{device} source should be sim after auto-apply of demo mode"
        )


def test_runtime_demo_already_applied_not_reapplied(tmp_path):
    """S1: Runtime must not create a second tmpdir when apply_demo_mode was
    already called upstream (main() CLI --demo path)."""
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    # Simulate main() having already called apply_demo_mode.
    apply_demo_mode(cfg, data_dir=str(tmp_path))
    first_dir = cfg.data_dir

    rt = Runtime(cfg)

    # data_dir must not have changed (no second mkdtemp).
    assert rt.config.data_dir == first_dir


# ---- S1b: hand-on-throttle guard in _run_demo_scenario ------------------- #


def test_demo_scenario_skips_if_driving(tmp_path):
    """S1b: _run_demo_scenario must not seed the scenario if the operator has
    thrust > 0.05 (hand on throttle while in MANUAL mode)."""
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    cfg.demo.enabled = True
    rt = Runtime(cfg)
    _give_fix(rt)

    # Simulate operator with throttle on.
    from vanchor.core.models import MotorCommand
    rt.state.motor_command = MotorCommand(thrust=0.3, steering=0.0)

    asyncio.run(rt._run_demo_scenario())

    # Scenario should NOT have engaged (mode stays MANUAL).
    assert rt.state.mode == ControlModeName.MANUAL
