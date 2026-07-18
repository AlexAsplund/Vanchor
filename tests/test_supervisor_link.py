"""Tests for the app-side supervisor link (SupervisorClient + Runtime integration)."""
from __future__ import annotations
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from vanchor.supervisor_client import SupervisorClient


# ------------------------------------------------------------------ #
# SupervisorClient.status() — None on connection refused
# ------------------------------------------------------------------ #

def test_status_returns_none_when_unreachable(tmp_path):
    """Bind to nothing: client must return None, not raise."""
    token_file = tmp_path / "token"
    token_file.write_text("abc123")
    # Use a port that's almost certainly not listening
    client = SupervisorClient("http://127.0.0.1:19873", str(token_file))
    result = client.status(timeout=0.5)
    assert result is None


# ------------------------------------------------------------------ #
# Token header is sent
# ------------------------------------------------------------------ #

class _TokenCapture(BaseHTTPRequestHandler):
    """Captures the X-Supervisor-Token header and responds 200 JSON."""
    received_token: str | None = None

    def do_GET(self):
        _TokenCapture.received_token = self.headers.get("X-Supervisor-Token", "")
        body = json.dumps({"supervisor_version": "0.1.0", "api_version": 1,
                           "containers": [], "disk": {}, "backups": {},
                           "job": None, "last_job": None, "warnings": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass  # silence


def test_token_header_sent(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TokenCapture)
    host, port = server.server_address
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    token_file = tmp_path / "token"
    token_file.write_text("mytoken12345")
    client = SupervisorClient(f"http://{host}:{port}", str(token_file))
    result = client.status(timeout=3.0)

    server.shutdown()
    assert result is not None
    assert _TokenCapture.received_token == "mytoken12345"


# ------------------------------------------------------------------ #
# Runtime._supervisor_snapshot() — direct call, no loops
# ------------------------------------------------------------------ #

def test_supervisor_snapshot_disabled(tmp_path):
    """When supervisor.enabled=False the snapshot must return available=False."""
    from vanchor.app import Runtime
    from vanchor.core.config import load

    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    cfg.supervisor.enabled = False

    runtime = Runtime(cfg)
    snap = runtime._supervisor_snapshot()

    assert snap["available"] is False
    assert "app_version" in snap


def test_supervisor_snapshot_enabled_but_no_status(tmp_path):
    """Enabled but no status yet (supervisor unreachable) -> available=False."""
    from vanchor.app import Runtime
    from vanchor.core.config import load

    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    cfg.supervisor.enabled = True
    cfg.supervisor.url = "http://127.0.0.1:19874"  # not listening

    runtime = Runtime(cfg)
    runtime._supervisor_status = None  # explicitly None

    snap = runtime._supervisor_snapshot()
    assert snap["available"] is False
    assert "app_version" in snap


def test_supervisor_snapshot_maps_status(tmp_path):
    """When status is a canned dict the snapshot maps it correctly."""
    from vanchor.app import Runtime
    from vanchor.core.config import load

    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    cfg.supervisor.enabled = True

    runtime = Runtime(cfg)
    runtime._supervisor_status = {
        "supervisor_version": "0.1.0",
        "api_version": 1,
        "containers": [
            {"name": "vanchor", "tag": "1.5.0a8", "previous_tag": "1.5.0a7"}
        ],
        "disk": {"data_used_pct": 50.0, "warn": False, "crit": False},
        "warnings": [],
        "job": None,
        "last_job": None,
        "backups": {"count": 2, "latest": "2026-07-18T00:00:00Z"},
    }

    snap = runtime._supervisor_snapshot()
    assert snap["available"] is True
    assert snap["supervisor_version"] == "0.1.0"
    assert snap["api_version"] == 1
    assert snap["tag"] == "1.5.0a8"
    assert snap["previous_tag"] == "1.5.0a7"
    assert snap["disk"]["data_used_pct"] == 50.0
    assert snap["warnings"] == []
    assert snap["backups"]["count"] == 2
