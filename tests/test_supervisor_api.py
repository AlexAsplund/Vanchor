"""Tests for vanchor_supervisor.api — HTTP API surface."""
from __future__ import annotations
import io
import json
import sys
import tarfile
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vanchor_supervisor.api import serve
from vanchor_supervisor.config import SupervisorSettings
from supervisor_fakes import FakeDockerBackend, FakeHealth


def _make_test_bundle(path: Path, manifest: dict) -> None:
    """Create a minimal valid bundle tar at *path* containing manifest.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest_bytes = json.dumps(manifest).encode()
    with tarfile.open(str(path), "w") as tf:
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        tf.addfile(info, io.BytesIO(manifest_bytes))


# ------------------------------------------------------------------ #
# Minimal fake core for API tests
# ------------------------------------------------------------------ #

class FakeCore:
    """Minimal stand-in for SupervisorCore for API surface tests.

    Mirrors the real SupervisorCore's public method signatures so api.py can
    call them without needing a full Docker environment.
    """

    def __init__(self, settings: SupervisorSettings, backend: FakeDockerBackend) -> None:
        self.settings = settings
        self.backend = backend
        self._active_job: dict | None = None
        self._last_job: dict | None = None
        self._containers_list = [
            {
                "name": "vanchor",
                "image": "ghcr.io/alexasplund/vanchor",
                "tag": "1.5.0a8",
                "previous_tag": None,
                "health_url": "",
            }
        ]

    def containers(self) -> list[dict]:
        return list(self._containers_list)

    def get_entry(self, name: str) -> dict | None:
        for e in self._containers_list:
            if e["name"] == name:
                return e
        return None

    def is_busy(self) -> bool:
        return self._active_job is not None

    def apply_update(self, name: str, *, bundle_rel: str | None = None,
                     tag: str | None = None) -> dict:
        from vanchor_supervisor.core import BusyError
        if self._active_job is not None:
            raise BusyError("busy")
        self._active_job = {"id": "j-test-1", "kind": "update", "phase": "verify",
                            "ok": None, "error": None}
        return dict(self._active_job)

    def rollback(self, name: str) -> dict:
        self._active_job = {"id": "j-rollback-1", "kind": "rollback", "phase": "recreate",
                            "ok": None, "error": None}
        return dict(self._active_job)

    def create_backup(self, name: str) -> dict:
        return {"id": "j-backup-1", "kind": "backup", "phase": "running",
                "ok": None, "error": None}

    def do_restore(self, name: str, backup_id: str) -> dict:
        return {"id": "j-restore-1", "kind": "restore", "phase": "running",
                "ok": None, "error": None}

    def get_job(self, job_id: str) -> dict | None:
        if self._active_job and self._active_job.get("id") == job_id:
            return dict(self._active_job)
        return None

    def get_last_job(self) -> dict | None:
        return self._last_job

    def list_backups(self) -> list[dict]:
        return []

    def prune(self) -> dict:
        return {"removed": [], "kept": [], "errors": []}

    def do_self_update(self, bundle_path) -> dict:
        return {"new_version": "0.2.0"}

    def list_jobs(self) -> list[dict]:
        return []


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture()
def token(tmp_path) -> str:
    return "deadbeef" * 4  # 32 hex chars


@pytest.fixture()
def api_server(tmp_path, token):
    settings = SupervisorSettings()
    settings.state_dir = str(tmp_path / "state")
    settings.listen_host = "127.0.0.1"
    settings.listen_port = 0  # OS picks a free port
    Path(settings.state_dir).mkdir(parents=True, exist_ok=True)
    # Write token file
    tok_path = Path(settings.state_dir) / "token"
    tok_path.write_text(token)

    backend = FakeDockerBackend(volume_root=tmp_path / "data")
    (tmp_path / "data").mkdir()
    # Create a minimal test bundle so /v1/update/inspect can read a manifest
    _make_test_bundle(
        tmp_path / "data" / "updates" / "test.bundle.tar",
        {
            "format": "vanchor-bundle",
            "kind": "app",
            "tag": "1.5.0a9",
            "name": "vanchor",
            "min_supervisor": "0.1.0",
        },
    )
    core = FakeCore(settings, backend)

    server = serve(core, settings)
    host, port = server.server_address
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://{host}:{port}", token
    server.shutdown()


def _get(url: str, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"X-Supervisor-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _post(url: str, body: dict, token: str) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"X-Supervisor-Token": token,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ------------------------------------------------------------------ #
# Auth
# ------------------------------------------------------------------ #

def test_no_token_returns_401(api_server):
    base, token = api_server
    req = urllib.request.Request(f"{base}/v1/status")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)
    assert exc_info.value.code == 401


def test_wrong_token_returns_401(api_server):
    base, token = api_server
    code, _ = _get(f"{base}/v1/status", "wrong" * 8)
    assert code == 401


def test_correct_token_returns_200(api_server):
    base, token = api_server
    code, data = _get(f"{base}/v1/status", token)
    assert code == 200


# ------------------------------------------------------------------ #
# Status shape
# ------------------------------------------------------------------ #

def test_status_shape(api_server):
    base, token = api_server
    code, data = _get(f"{base}/v1/status", token)
    assert code == 200
    assert "supervisor_version" in data
    assert "api_version" in data
    assert "containers" in data
    assert "disk" in data
    assert "backups" in data
    assert "job" in data
    assert "last_job" in data
    assert "warnings" in data


def test_status_containers_shape(api_server):
    base, token = api_server
    _, data = _get(f"{base}/v1/status", token)
    c = data["containers"][0]
    assert c["name"] == "vanchor"
    assert "tag" in c
    assert "state" in c


# ------------------------------------------------------------------ #
# Apply / job tracking
# ------------------------------------------------------------------ #

def test_apply_returns_job_id(api_server):
    base, token = api_server
    code, data = _post(f"{base}/v1/update/apply",
                       {"name": "vanchor", "source": "registry", "tag": "1.5.0a9"},
                       token)
    assert code == 200
    assert "job_id" in data


def test_apply_busy_returns_409(api_server):
    base, token = api_server
    # First apply starts a job
    _post(f"{base}/v1/update/apply",
          {"name": "vanchor", "source": "registry", "tag": "1.5.0a9"}, token)
    # Second apply should be refused
    code, data = _post(f"{base}/v1/update/apply",
                       {"name": "vanchor", "source": "registry", "tag": "1.5.0a9"},
                       token)
    assert code == 409
    assert "error" in data


def test_jobs_by_id(api_server):
    base, token = api_server
    _, apply_data = _post(f"{base}/v1/update/apply",
                          {"name": "vanchor", "source": "registry", "tag": "1.5.0a9"},
                          token)
    job_id = apply_data["job_id"]
    code, job = _get(f"{base}/v1/jobs/{job_id}", token)
    assert code == 200
    assert job["id"] == job_id


def test_jobs_last_empty(api_server):
    base, token = api_server
    code, data = _get(f"{base}/v1/jobs/last", token)
    assert code == 200
    # Either {} or a job dict
    assert isinstance(data, dict)


# ------------------------------------------------------------------ #
# 404 on unknown paths
# ------------------------------------------------------------------ #

def test_unknown_v1_path_404(api_server):
    base, token = api_server
    code, _ = _get(f"{base}/v1/nonexistent/route", token)
    assert code == 404


def test_v2_path_404(api_server):
    base, token = api_server
    code, _ = _get(f"{base}/v2/status", token)
    assert code == 404


# ------------------------------------------------------------------ #
# Inspect
# ------------------------------------------------------------------ #

def test_inspect_returns_compat(api_server):
    base, token = api_server
    code, data = _post(f"{base}/v1/update/inspect",
                       {"bundle": "updates/test.bundle.tar"}, token)
    assert code == 200
    assert "compatible" in data
    assert "manifest" in data
    assert "current_tag" in data


# ------------------------------------------------------------------ #
# Rollback
# ------------------------------------------------------------------ #

def test_rollback_returns_job_id(api_server):
    base, token = api_server
    code, data = _post(f"{base}/v1/rollback", {"name": "vanchor"}, token)
    assert code == 200
    assert "job_id" in data


# ------------------------------------------------------------------ #
# Backups list
# ------------------------------------------------------------------ #

def test_list_backups(api_server):
    base, token = api_server
    code, data = _get(f"{base}/v1/backups", token)
    assert code == 200
    assert "backups" in data
    assert isinstance(data["backups"], list)


# ------------------------------------------------------------------ #
# Body size cap
# ------------------------------------------------------------------ #

def test_body_size_cap(api_server):
    base, token = api_server
    # POST with a body larger than 1 MB
    big_body = json.dumps({"data": "x" * (1024 * 1024 + 1)}).encode()
    req = urllib.request.Request(
        f"{base}/v1/update/apply",
        data=big_body,
        method="POST",
        headers={"X-Supervisor-Token": token, "Content-Type": "application/json"},
    )
    # The server rejects an oversized body WITHOUT reading it (DoS-safe), so a
    # well-timed client sees a clean 413, but a client still mid-write of the
    # 1 MB+ body can instead have the connection reset (broken pipe / reset).
    # Both mean "capped" — accept either rather than flaking on the timing.
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("oversized body was not rejected")
    except urllib.error.HTTPError as exc:
        assert exc.code == 413
    except (urllib.error.URLError, ConnectionError, BrokenPipeError, OSError):
        pass  # connection reset by the cap — also a valid rejection


# ------------------------------------------------------------------ #
# A2: _self_update path traversal containment
# ------------------------------------------------------------------ #

def test_self_update_traversal_rejected(api_server):
    """A2: /v1/self-update must reject bundle paths that escape the volume."""
    base, token = api_server
    code, data = _post(
        f"{base}/v1/self-update",
        {"bundle": "../../etc/passwd"},
        token,
    )
    assert code == 400
    assert "invalid" in data.get("error", "")


# ------------------------------------------------------------------ #
# A3: backup download backup_id sanitization
# ------------------------------------------------------------------ #

def test_download_backup_rejects_wildcard(api_server):
    """A3: backup_id with glob metachar must return 400 not a directory listing."""
    base, token = api_server
    code, data = _get(f"{base}/v1/backups/*/download", token)
    assert code == 400
    assert "invalid" in data.get("error", "")


def test_download_backup_rejects_dotdot(api_server):
    """A3: backup_id with path traversal must return 400."""
    base, token = api_server
    code, data = _get(f"{base}/v1/backups/../token/download", token)
    # FastAPI may return 404 for the parameterised route or 400 from our guard.
    assert code in (400, 404)


# ------------------------------------------------------------------ #
# I1: provision_token
# ------------------------------------------------------------------ #

from vanchor_supervisor.api import provision_token
import os
import stat as _stat


def test_provision_token_creates_file(tmp_path):
    """I1: provision_token creates a non-empty token file."""
    state_dir = str(tmp_path / "state")
    token = provision_token(state_dir, volume_mp="/unused")
    tok_file = Path(state_dir) / "token"
    assert tok_file.exists()
    assert len(token) == 64  # secrets.token_hex(32) → 64 hex chars
    assert tok_file.read_text().strip() == token


def test_provision_token_idempotent(tmp_path):
    """I1: second call returns the same token (no overwrite)."""
    state_dir = str(tmp_path / "state")
    t1 = provision_token(state_dir, volume_mp="/unused")
    t2 = provision_token(state_dir, volume_mp="/unused")
    assert t1 == t2


def test_provision_token_perms_600(tmp_path):
    """I1: token file permissions are 0o600 (owner r/w only)."""
    state_dir = str(tmp_path / "state")
    provision_token(state_dir, volume_mp="/unused")
    tok_file = Path(state_dir) / "token"
    mode = tok_file.stat().st_mode & 0o777
    assert mode == 0o600, f"Expected 0o600, got 0o{mode:o}"


# ------------------------------------------------------------------ #
# I1: __main__ import must not crash (provision_token exists in api)
# ------------------------------------------------------------------ #

def test_main_module_imports_cleanly():
    """I1: importing __main__ must not raise (provision_token must exist in api)."""
    import importlib
    # The module does a from .api import provision_token at the top of main().
    # We verify the import completes without ImportError.
    mod = importlib.import_module("vanchor_supervisor.__main__")
    assert callable(getattr(mod, "main", None))
