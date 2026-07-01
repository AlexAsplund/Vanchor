"""Smoke tests for the FastAPI surface using Starlette's TestClient.

These run the full app lifespan (which starts the simulator + controller loops)
without any network or hardware."""

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.ui.server import create_app, shape_frame


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Allow the TestClient's default Host ("testserver") via the env var so
    # the host-check middleware doesn't reject test requests.  Production
    # deployments do NOT include "testserver", keeping the protection strict.
    monkeypatch.setenv("VANCHOR_ALLOWED_HOSTS", "testserver")
    from vanchor.core.config import load

    cfg = load(None)
    cfg.data_dir = str(tmp_path)  # isolate: never write the repo's boats.json/devices.json
    app = create_app(Runtime(cfg))
    with TestClient(app) as c:
        yield c


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Vanchor-NG" in r.text


def test_state_endpoint_shape(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] == "manual"
    assert "truth" in data and "environment" in data
    assert set(data["motor"]) >= {"thrust", "steering", "steer_angle_deg"}
    assert "depth_m" in data and "depth_points" in data
    assert set(data["sensors"]) >= {"heading_rejected", "position_rejected"}
    assert set(data["steering"]) >= {"target_deg", "angle_deg", "range_deg", "wrap_pct"}
    assert "boat" in data and "calibration" in data and "sim_enabled" in data


def test_boat_profile_get_and_update(client):
    assert "max_speed_mps" in client.get("/api/boat").json()
    body = client.post("/api/boat", json={"max_speed_mps": 1.9, "thruster_mount": "stern"}).json()
    assert body["max_speed_mps"] == 1.9 and body["thruster_mount"] == "stern"


def test_command_changes_mode(client):
    assert client.get("/api/state").json()["mode"] == "manual"
    r = client.post("/api/command", json={"type": "anchor_hold", "radius_m": 5})
    assert r.json() == {"ok": True}
    assert client.get("/api/state").json()["mode"] == "anchor_hold"


def test_set_environment_command(client):
    client.post(
        "/api/command",
        json={"type": "set_environment", "wind_speed": 9.0, "wind_dir": 200.0},
    )
    env = client.get("/api/state").json()["environment"]
    assert env["wind_speed"] == 9.0
    assert env["wind_dir"] == 200.0


def test_set_environment_variability(client):
    client.post(
        "/api/command",
        json={"type": "set_environment", "wind_variability": 0.4, "current_variability": 0.2},
    )
    env = client.get("/api/state").json()["environment"]
    assert env["wind_variability"] == 0.4
    assert env["current_variability"] == 0.2


def test_weather_presets_listed(client):
    presets = client.get("/api/weather/presets").json()["presets"]
    ids = {p["id"] for p in presets}
    assert {"calm", "lake", "river", "coastal"} <= ids
    lake = next(p for p in presets if p["id"] == "lake")
    assert set(lake) >= {
        "id", "label", "current_speed", "current_dir", "wind_speed",
        "wind_dir", "gust_amplitude_mps", "wind_variability", "current_variability",
    }


def test_weather_preset_command_applies(client):
    client.post("/api/command", json={"type": "weather_preset", "id": "river"})
    env = client.get("/api/state").json()["environment"]
    assert env["current_speed"] > 0.5  # river has strong current


def test_route_plan_missing_dest(client):
    r = client.post("/api/route/plan", json={"mode": "fastest"})
    body = r.json()
    assert body["ok"] is False
    assert "waypoints" in body and "message" in body


def test_route_island_missing_coords(client):
    r = client.post("/api/route/island", json={})
    body = r.json()
    assert body["ok"] is False
    assert body["loop"] is True
    assert "waypoints" in body and "message" in body


def test_tune_jobs_listed(client):
    jobs = client.get("/api/tune/jobs").json()["jobs"]
    assert {j["name"] for j in jobs} == {"heading", "anchor", "cruise", "drift"}


def test_tune_endpoint_runs_and_applies(client):
    r = client.post("/api/tune", json={"job": "cruise", "max_evals": 15, "apply": True})
    data = r.json()
    assert data["job"] == "cruise"
    assert "tuned_params" in data and "baseline_cost" in data
    assert data["tuned_cost"] <= data["baseline_cost"] + 1e-9
    assert data.get("applied") is True


def test_tune_unknown_job_returns_error(client):
    assert "error" in client.post("/api/tune", json={"job": "nope"}).json()


def test_websocket_streams_and_accepts_commands(client):
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert "mode" in first
        ws.send_json({"type": "anchor_hold", "radius_m": 6})
        # The next snapshot the server pushes should reflect the new mode.
        for _ in range(20):
            msg = ws.receive_json()
            if msg["mode"] == "anchor_hold":
                break
        assert msg["mode"] == "anchor_hold"


# ---- Fix 2: DNS-rebinding / host validation -------------------------------- #

def test_host_check_rejects_dns_rebinding_domain(client):
    """A request with an attacker-controlled domain in Host must be rejected."""
    r = client.get("/api/state", headers={"Host": "attacker.com"})
    assert r.status_code == 400


def test_host_check_allows_ip_literal(client):
    r = client.get("/api/state", headers={"Host": "192.168.1.100"})
    assert r.status_code == 200


def test_host_check_allows_localhost(client):
    r = client.get("/api/state", headers={"Host": "localhost"})
    assert r.status_code == 200


def test_host_check_allows_mdns(client):
    r = client.get("/api/state", headers={"Host": "boat.local"})
    assert r.status_code == 200


def test_host_check_allows_ip_with_port(client):
    r = client.get("/api/state", headers={"Host": "10.0.0.1:8080"})
    assert r.status_code == 200


def test_host_check_allows_env_var_name(tmp_path, monkeypatch):
    """A hostname explicitly listed in VANCHOR_ALLOWED_HOSTS must be accepted."""
    monkeypatch.setenv("VANCHOR_ALLOWED_HOSTS", "testserver,mypilot.home")
    from vanchor.core.config import load

    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    app = create_app(Runtime(cfg))
    with TestClient(app) as c:
        r = c.get("/api/state", headers={"Host": "mypilot.home"})
        assert r.status_code == 200


# ---- Fix 3: broadcaster resilience ---------------------------------------- #

def test_broadcaster_continues_after_client_disconnect(client):
    """When one WS client drops, the broadcaster must not die and must keep
    sending frames to remaining clients."""
    with client.websocket_connect("/ws") as ws_good:
        ws_good.receive_json()  # consume initial snapshot

        # Open a second client and immediately close it — simulates an abrupt
        # disconnect that will cause the broadcaster to error on the next send.
        with client.websocket_connect("/ws") as ws_bad:
            ws_bad.receive_json()

        # ws_bad is now closed.  The broadcaster should discard it and keep
        # sending to ws_good.
        for _ in range(10):
            msg = ws_good.receive_json()
            assert "mode" in msg


# ---- Fix 4: /api/log strips bulk fields by default ------------------------- #

def test_log_strips_depth_points_by_default(client):
    """/api/log must not include depth_points in default (non-full) output."""
    # Drive some telemetry frames into the ring first.
    client.get("/api/state")
    r = client.get("/api/log?n=10")
    assert r.status_code == 200
    frames = r.json()["telemetry"]
    for frame in frames:
        assert "depth_points" not in frame


def test_log_full_includes_depth_points(client):
    """With ?full=1, /api/log must return all fields including depth_points."""
    client.get("/api/state")
    r = client.get("/api/log?n=10&full=1")
    assert r.status_code == 200
    frames = r.json()["telemetry"]
    # depth_points should be present in at least one frame (recorder auto-fills
    # on each telemetry() call).
    assert any("depth_points" in f for f in frames)


# ---- WS application-level heartbeat (ping/pong) ---------------------------- #


@pytest.fixture()
def runtime_client(tmp_path, monkeypatch):
    """Like ``client`` but also yields the Runtime for direct state inspection."""
    monkeypatch.setenv("VANCHOR_ALLOWED_HOSTS", "testserver")
    from vanchor.core.config import load

    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    app = create_app(rt)
    with TestClient(app) as c:
        yield c, rt


def test_ws_ping_updates_liveness(runtime_client):
    """A ping message over WS must advance runtime._last_client_seen."""
    import time

    c, rt = runtime_client
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()  # initial snapshot
        before = rt._last_client_seen
        time.sleep(0.02)  # ensure the monotonic clock advances before pinging
        ws.send_json({"type": "ping"})
        # Drain messages until we receive the pong (telemetry frames may arrive first).
        for _ in range(20):
            msg = ws.receive_json()
            if msg.get("type") == "pong":
                break
    assert rt._last_client_seen is not None
    assert rt._last_client_seen > before


def test_ws_ping_not_forwarded_to_controller(runtime_client, caplog):
    """A ping must not reach the controller: no mode change, no unknown-command warning."""
    import logging

    c, rt = runtime_client
    with caplog.at_level(logging.WARNING, logger="vanchor.controller"):
        with c.websocket_connect("/ws") as ws:
            ws.receive_json()  # initial snapshot
            ws.send_json({"type": "ping"})
            # Wait for the pong; telemetry frames may arrive first.
            for _ in range(20):
                msg = ws.receive_json()
                if msg.get("type") == "pong":
                    break
    assert rt.state.mode.value == "manual"
    assert not any("unknown command" in r.message for r in caplog.records)


# ---- Fix 3: shape_frame decimation ----------------------------------------


def test_shape_frame_full_returns_complete_snapshot():
    """Full frames carry depth_points, waypoints and track.points."""
    snap = {
        "mode": "manual",
        "depth_points": [[59.0, 18.0, 5.0]],
        "waypoints": [{"lat": 59.0, "lon": 18.0, "name": "wp1", "heading": 0.0}],
        "track": {"recording": False, "count": 3, "points": [[59.0, 18.0], [59.001, 18.0]]},
        "depth_count": 1,
    }
    out = shape_frame(snap, full=True)
    assert out is snap or out == snap
    assert "waypoints" in out
    assert "depth_points" in out
    assert "points" in out["track"]
    assert out["track"]["count"] == 3


def test_shape_frame_non_full_strips_bulky_arrays():
    """Non-full frames: depth_points absent, waypoints absent, track.points absent."""
    snap = {
        "mode": "anchor_hold",
        "depth_points": [[59.0, 18.0, 5.0]],
        "waypoints": [{"lat": 59.0, "lon": 18.0, "name": "wp1", "heading": 0.0}],
        "track": {"recording": True, "count": 7, "points": [[59.0, 18.0]]},
        "depth_count": 42,
    }
    out = shape_frame(snap, full=False)
    assert "depth_points" not in out         # omitted
    assert "waypoints" not in out            # absent (not null/empty)
    assert "track" in out
    assert "points" not in out["track"]      # array stripped
    assert out["track"]["recording"] is True
    assert out["track"]["count"] == 7
    assert out["mode"] == "anchor_hold"
    assert out["depth_count"] == 42


def test_shape_frame_non_full_tolerates_missing_track():
    """Non-full frames: if snapshot has no track key it stays absent (no KeyError)."""
    snap = {"mode": "manual", "depth_count": 0}
    out = shape_frame(snap, full=False)
    assert "track" not in out
    assert out["mode"] == "manual"


def test_shape_frame_non_full_tolerates_missing_waypoints():
    """Non-full frames: if snapshot has no waypoints key it stays absent (no KeyError)."""
    snap = {"mode": "manual", "depth_count": 0,
            "track": {"recording": False, "count": 0}}
    out = shape_frame(snap, full=False)
    assert "waypoints" not in out
    assert "track" in out and "points" not in out["track"]


# ---- Fix 1: depth overlay endpoints: limit param + truncated flag ----------


def test_depth_contours_truncated_when_limit_hit(runtime_client):
    """contours endpoint: truncated=True when result hits the requested limit."""
    c, rt = runtime_client
    rt.depth_map.contours = [
        {"d": float(i), "pts": [[59.0 + i * 0.001, 18.0], [59.001 + i * 0.001, 18.001]]}
        for i in range(200)
    ]
    # Explicit limit=100 → 100 items, truncated=True
    r = c.get("/api/depth/contours?limit=100")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 100
    assert data["truncated"] is True

    # No limit → default 5000 >> 200 → all returned, truncated=False
    r = c.get("/api/depth/contours")
    data = r.json()
    assert data["count"] == 200
    assert data["truncated"] is False


def test_depth_contours_limit_clamped(runtime_client):
    """contours endpoint: limit is clamped to [100, 8000] server-side."""
    c, rt = runtime_client
    rt.depth_map.contours = [
        {"d": float(i), "pts": [[59.0, 18.0 + i * 0.001]]}
        for i in range(500)
    ]
    # limit=5 below minimum (100) → clamped to 100
    r = c.get("/api/depth/contours?limit=5")
    data = r.json()
    assert data["count"] == 100
    assert data["truncated"] is True

    # limit=99999 above maximum (8000) → clamped to 8000; 500 < 8000 → all returned
    r = c.get("/api/depth/contours?limit=99999")
    data = r.json()
    assert data["count"] == 500
    assert data["truncated"] is False


def test_depth_composition_truncated_when_limit_hit(runtime_client):
    """composition endpoint: truncated=True when result hits the requested limit."""
    c, rt = runtime_client
    rt.depth_map.composition = [
        {"pct": float(i % 100), "ring": [[59.0, 18.0], [59.001, 18.001], [59.001, 18.0]]}
        for i in range(200)
    ]
    # Explicit limit=100 → 100 items, truncated=True
    r = c.get("/api/depth/composition?limit=100")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 100
    assert data["truncated"] is True

    # No limit → default 4000 >> 200 → all returned, truncated=False
    r = c.get("/api/depth/composition")
    data = r.json()
    assert data["count"] == 200
    assert data["truncated"] is False


def test_depth_composition_limit_clamped(runtime_client):
    """composition endpoint: limit is clamped to [100, 8000] server-side."""
    c, rt = runtime_client
    rt.depth_map.composition = [
        {"pct": float(i % 100), "ring": [[59.0, 18.0], [59.001, 18.001], [59.001, 18.0]]}
        for i in range(500)
    ]
    # limit=5 below minimum (100) → clamped to 100
    r = c.get("/api/depth/composition?limit=5")
    data = r.json()
    assert data["count"] == 100
    assert data["truncated"] is True

    # limit=99999 above maximum (8000) → clamped to 8000; 500 < 8000 → all returned
    r = c.get("/api/depth/composition?limit=99999")
    data = r.json()
    assert data["count"] == 500
    assert data["truncated"] is False
