"""Smoke tests for the FastAPI surface using Starlette's TestClient.

These run the full app lifespan (which starts the simulator + controller loops)
without any network or hardware."""

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.ui.server import create_app


@pytest.fixture()
def client():
    app = create_app(Runtime())
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
