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
        # The next snapshot the server pushes should reflect the new mode. A
        # `{type:"role"}` message (no "mode") now interleaves on connect, so read
        # via .get() and keep scanning.
        msg = None
        for _ in range(20):
            frame = ws.receive_json()
            if frame.get("mode") == "anchor_hold":
                msg = frame
                break
        assert msg is not None and msg["mode"] == "anchor_hold"


# ---- #21: versioned WS envelope + command acks ----------------------------- #

def _recv_until(ws, pred, tries=40):
    """Receive frames until ``pred(msg)`` is truthy; return that msg (or None)."""
    for _ in range(tries):
        msg = ws.receive_json()
        if pred(msg):
            return msg
    return None


def test_ws_telemetry_carries_envelope(client):
    """Telemetry frames gain type/v/seq/ts alongside the flat fields (#21)."""
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "telemetry"
        assert first["v"] == 1
        assert "ts" in first and isinstance(first["ts"], (int, float))
        assert first["seq"] == 0
        # Flat telemetry fields are still present (backward compatible).
        assert "mode" in first and "motor" in first
        # The next telemetry frame is now a shared BROADCAST frame with a GLOBAL
        # monotonic seq (#24 serialize-once), not a per-connection one. A role
        # message may interleave first, so scan to the next telemetry frame.
        second = _recv_until(ws, lambda m: m.get("type") == "telemetry")
        assert second is not None
        assert second["seq"] > first["seq"]  # monotonic (global broadcast seq)


def test_ws_command_with_seq_gets_ack(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # consume initial snapshot
        ws.send_json({"type": "anchor_hold", "radius_m": 6, "seq": 7})
        ack = _recv_until(ws, lambda m: m.get("type") == "ack")
        assert ack is not None
        assert ack == {"type": "ack", "v": 1, "seq": 7}


def test_ws_bare_command_gets_no_ack(client):
    """A command without a seq behaves exactly as before: no ack/nack reply."""
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "anchor_hold", "radius_m": 6})  # no seq
        # Every following frame within a window must be plain telemetry.
        for _ in range(15):
            m = ws.receive_json()
            assert m.get("type") not in ("ack", "nack")


def test_ws_failing_command_with_seq_gets_nack(client):
    """A handler exception on a seq'd command replies nack with the error."""
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        # set_gps_offset without true_lat/true_lon raises KeyError in the handler.
        ws.send_json({"type": "set_gps_offset", "seq": 9})
        nack = _recv_until(ws, lambda m: m.get("type") == "nack")
        assert nack is not None
        assert nack["type"] == "nack" and nack["v"] == 1 and nack["seq"] == 9
        assert isinstance(nack["error"], str) and nack["error"]


def test_ws_ping_pong_carries_version(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "ping"})
        pong = _recv_until(ws, lambda m: m.get("type") == "pong")
        assert pong == {"type": "pong", "v": 1}


def test_state_snapshot_has_no_envelope_keys(client):
    """The envelope is WS-only; /api/state stays a pure snapshot."""
    data = client.get("/api/state").json()
    for k in ("type", "v", "seq", "ts"):
        assert k not in data


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


def test_host_check_allows_bare_hostname(client):
    """A bare single-label LAN machine name (no dot) can't be a public domain."""
    r = client.get("/api/state", headers={"Host": "spark-11a6:8000"})
    assert r.status_code == 200


def test_host_check_allows_private_lan_suffixes(client):
    """Router/mDNS private zones (.lan/.home/.internal/.localdomain) are LAN-only."""
    for host in ("spark-11a6.local.lan", "pilot.home", "boat.internal",
                 "helm.localdomain"):
        r = client.get("/api/state", headers={"Host": host})
        assert r.status_code == 200, f"{host} should be allowed"


def test_host_check_still_rejects_public_fqdn(client):
    """Broadening to LAN names must not accept a public domain (rebinding)."""
    for host in ("evil.com", "attacker.example.org", "vanchor.io"):
        r = client.get("/api/state", headers={"Host": host})
        assert r.status_code == 400, f"{host} must be rejected"


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
        # sending telemetry to ws_good. Role/presence messages (no "mode") now
        # interleave as ws_bad connects/disconnects, so filter to telemetry.
        got = 0
        for _ in range(40):
            msg = ws_good.receive_json()
            if msg.get("type") == "telemetry":
                assert "mode" in msg
                got += 1
            if got >= 5:
                break
        assert got >= 5


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


def test_log_full_returns_other_fields_but_not_depth_points(client):
    """With ?full=1, /api/log returns the untrimmed frames -- but depth_points is
    no longer among them: the telemetry ring strips that bulky array BEFORE
    storing (the live layer keeps the authoritative copy; the WS full-frame path
    still ships it). So even full=1 has the scalar fields but not depth_points."""
    client.get("/api/state")
    r = client.get("/api/log?n=10&full=1")
    assert r.status_code == 200
    frames = r.json()["telemetry"]
    assert frames, "expected at least one recorded frame"
    # Scalar fields survive untrimmed...
    assert any("depth_count" in f for f in frames)
    # ...but the bulky depth_points array is never stored in the ring.
    assert all("depth_points" not in f for f in frames)


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


# ---- #24: multi-client roles (helm vs observer) ---------------------------- #


def _role_msg(ws, tries=60):
    """Receive frames until a ``{type:"role"}`` message arrives; return it."""
    return _recv_until(ws, lambda m: m.get("type") == "role", tries=tries)


def _role_matching(ws, want, tries=60):
    """Receive frames until a role message with ``role == want`` arrives."""
    return _recv_until(
        ws, lambda m: m.get("type") == "role" and m.get("role") == want, tries=tries
    )


def test_ws_first_client_helm_second_observer(client):
    """First WS client is designated helm; the second is an observer (#24)."""
    with client.websocket_connect("/ws") as ws1:
        ws1.receive_json()  # snapshot
        role1 = _role_msg(ws1)
        assert role1 is not None and role1["role"] == "helm"
        assert role1["v"] == 1

        with client.websocket_connect("/ws") as ws2:
            ws2.receive_json()  # snapshot
            role2 = _role_msg(ws2)
            assert role2 is not None and role2["role"] == "observer"
            assert role2["helm_present"] is True
            assert role2["clients"] == 2


def test_ws_observer_command_denied_mode_unchanged(client):
    """An observer's mode-changing command is role_denied and NOT forwarded."""
    with client.websocket_connect("/ws") as ws1:
        ws1.receive_json()
        with client.websocket_connect("/ws") as ws2:
            ws2.receive_json()
            assert _role_matching(ws2, "observer") is not None
            ws2.send_json({"type": "heading_hold", "target_deg": 90, "seq": 31})
            denied = _recv_until(ws2, lambda m: m.get("type") == "role_denied")
            assert denied is not None
            assert denied["seq"] == 31
            assert "take the helm" in denied["error"]
    # The controller never saw the command: mode stayed manual.
    assert client.get("/api/state").json()["mode"] == "manual"


def test_ws_observer_stop_is_honored(client):
    """SAFETY FLOOR: an observer's STOP is always honored (mode→manual, thrust 0)."""
    # Put the boat in a non-manual mode first (REST is un-gated setup).
    client.post("/api/command", json={"type": "heading_hold", "target_deg": 90})
    assert client.get("/api/state").json()["mode"] == "heading_hold"

    with client.websocket_connect("/ws") as ws1:
        ws1.receive_json()
        with client.websocket_connect("/ws") as ws2:
            ws2.receive_json()
            assert _role_matching(ws2, "observer") is not None
            ws2.send_json({"type": "stop", "seq": 42})
            reply = _recv_until(
                ws2, lambda m: m.get("type") in ("ack", "role_denied")
            )
            assert reply is not None and reply["type"] == "ack"
            assert reply["seq"] == 42

    # Stop took effect.
    state = client.get("/api/state").json()
    assert state["mode"] == "manual"
    assert abs(float(state["motor"]["thrust"])) < 0.05


def test_ws_take_helm_transfers_and_demotes(client):
    """take_helm from the observer makes it helm and demotes the first client."""
    with client.websocket_connect("/ws") as ws1:
        ws1.receive_json()
        assert _role_matching(ws1, "helm") is not None
        with client.websocket_connect("/ws") as ws2:
            ws2.receive_json()
            assert _role_matching(ws2, "observer") is not None

            ws2.send_json({"type": "take_helm"})
            # ws2 becomes helm; ws1 is demoted to observer.
            assert _role_matching(ws2, "helm") is not None
            assert _role_matching(ws1, "observer") is not None

            # ws1 (now observer) commands are denied.
            ws1.send_json({"type": "heading_hold", "target_deg": 45, "seq": 51})
            denied = _recv_until(ws1, lambda m: m.get("type") == "role_denied")
            assert denied is not None and denied["seq"] == 51
    assert client.get("/api/state").json()["mode"] == "manual"


def test_ws_helm_disconnect_promotes_observer(client):
    """When the helm disconnects, the oldest remaining client is auto-promoted."""
    ws1cm = client.websocket_connect("/ws")
    ws1 = ws1cm.__enter__()
    ws1.receive_json()
    assert _role_matching(ws1, "helm") is not None

    ws2cm = client.websocket_connect("/ws")
    ws2 = ws2cm.__enter__()
    ws2.receive_json()
    assert _role_matching(ws2, "observer") is not None

    # Drop the helm (ws1). ws2 must be promoted to helm.
    ws1cm.__exit__(None, None, None)
    try:
        promoted = _role_matching(ws2, "helm")
        assert promoted is not None
        assert promoted["helm_present"] is True
        assert promoted["clients"] == 1
    finally:
        ws2cm.__exit__(None, None, None)


def test_ws_broadcast_frame_carries_presence(client):
    """The high-rate broadcast telemetry frame carries clients/helm_present (#24)."""
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # connect snapshot (per-connection envelope, no presence)
        frame = _recv_until(
            ws, lambda m: m.get("type") == "telemetry" and "clients" in m
        )
        assert frame is not None
        assert frame["clients"] >= 1
        assert frame["helm_present"] is True
        # Presence scalars are shared, but per-client role is NOT in telemetry.
        assert "role" not in frame


def test_ws_broadcast_seq_is_global_and_monotonic(client):
    """Serialize-once (#24): broadcast frames share a GLOBAL monotonic seq."""
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # snapshot at its own per-connection seq (0)
        f1 = _recv_until(ws, lambda m: m.get("type") == "telemetry" and "clients" in m)
        f2 = _recv_until(ws, lambda m: m.get("type") == "telemetry" and "clients" in m)
        assert f1 is not None and f2 is not None
        assert f2["seq"] > f1["seq"]


# ---- #26: command audit log ------------------------------------------------ #


def test_audit_records_rest_command(client):
    """A REST command is audited with source "rest" and outcome "accepted"."""
    client.post("/api/command", json={"type": "anchor_hold", "radius_m": 5})
    cmds = client.get("/api/audit").json()["commands"]
    assert cmds, "audit should have at least one entry"
    last = cmds[-1]  # newest last
    assert last["type"] == "anchor_hold"
    assert last["source"] == "rest"
    assert last["outcome"] == "accepted"
    assert isinstance(last["ts"], (int, float))


def test_audit_records_ws_helm_accepted(client):
    """A helm WS command is audited source "helm" / outcome "accepted"."""
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "heading_hold", "target_deg": 90, "seq": 3})
        assert _recv_until(ws, lambda m: m.get("type") == "ack") is not None
    cmds = client.get("/api/audit").json()["commands"]
    hh = [c for c in cmds if c["type"] == "heading_hold"]
    assert hh and hh[-1]["source"] == "helm" and hh[-1]["outcome"] == "accepted"


def test_audit_records_observer_denied(client):
    """An observer's rejected command is audited source "observer"/"denied"."""
    with client.websocket_connect("/ws") as ws1:
        ws1.receive_json()
        with client.websocket_connect("/ws") as ws2:
            ws2.receive_json()
            assert _role_matching(ws2, "observer") is not None
            ws2.send_json({"type": "waypoint", "seq": 71})
            assert _recv_until(ws2, lambda m: m.get("type") == "role_denied") is not None
    cmds = client.get("/api/audit").json()["commands"]
    denied = [c for c in cmds if c["type"] == "waypoint"]
    assert denied and denied[-1]["source"] == "observer"
    assert denied[-1]["outcome"] == "denied"


def test_audit_records_ws_error(client):
    """A handler exception (seq'd) is audited with outcome "error" + detail."""
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        # set_gps_offset without coords raises KeyError in the handler -> nack.
        ws.send_json({"type": "set_gps_offset", "seq": 5})
        assert _recv_until(ws, lambda m: m.get("type") == "nack") is not None
    cmds = client.get("/api/audit").json()["commands"]
    err = [c for c in cmds if c["type"] == "set_gps_offset"]
    assert err and err[-1]["outcome"] == "error" and err[-1].get("detail")


def test_audit_does_not_record_ping(client):
    """Pings must never appear in the audit (only real commands are logged)."""
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "ping"})
        assert _recv_until(ws, lambda m: m.get("type") == "pong") is not None
        ws.send_json({"type": "anchor_hold", "radius_m": 4, "seq": 8})
        assert _recv_until(ws, lambda m: m.get("type") == "ack") is not None
    cmds = client.get("/api/audit").json()["commands"]
    assert all(c["type"] != "ping" for c in cmds)
    assert any(c["type"] == "anchor_hold" for c in cmds)


def test_audit_n_param_limits_returned(client):
    """?n caps how many entries are returned (most recent)."""
    for i in range(6):
        client.post("/api/command", json={"type": "anchor_hold", "radius_m": i + 1})
    cmds = client.get("/api/audit?n=3").json()["commands"]
    assert len(cmds) == 3  # only the 3 most recent
