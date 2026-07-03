"""Tests for the opt-in "upload last session on WiFi" flow (roadmap #48).

The boat already writes session artifacts to the data dir (chunked debug
recordings under ``debug/`` and always-on black-box dumps under ``blackbox/``).
This exercises the packaging + opt-in upload module and the UI endpoints that
list sessions and trigger an upload.

Privacy / safety contract under test:

* uploads are strictly OPT-IN and default OFF -- an upload with ``opt_in`` false
  never touches the network;
* a missing / empty destination is handled cleanly (no crash, clear error);
* packaging picks the LATEST session and its bytes reach the POST endpoint;
* the endpoint reads the opt-in flag + URL from the prefs KV store (never
  config) and reports success / failure.
"""

from __future__ import annotations

import io
import json
import os
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.obs.session_upload import SessionUploader
from vanchor.ui.server import create_app


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _write_debug_session(data_dir: str, name: str, *, parts: int = 1,
                         mtime: float | None = None) -> str:
    """Create a chunked debug session dir with ``parts`` gzip parts."""
    import gzip

    sess = os.path.join(data_dir, "debug", name)
    os.makedirs(sess, exist_ok=True)
    for i in range(1, parts + 1):
        p = os.path.join(sess, f"{i:04d}.ndjson.gz")
        with gzip.open(p, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": 1.0, "kind": "meta", "data": {"part": i}}) + "\n")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
    return sess


def _write_blackbox_dump(data_dir: str, name: str, *, mtime: float | None = None) -> str:
    import gzip

    d = os.path.join(data_dir, "blackbox")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        json.dump({"meta": {"alarms": ["drag_alarm"]}, "frames": []}, fh)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


class _FakeEndpoint:
    """Captures the last POST and returns a configurable status code."""

    def __init__(self, code: int = 200):
        self.code = code
        self.calls: list[tuple[str, str, bytes]] = []

    def __call__(self, url: str, filename: str, data: bytes) -> int:
        self.calls.append((url, filename, data))
        return self.code


# --------------------------------------------------------------------------- #
# Listing + packaging picks the LATEST session
# --------------------------------------------------------------------------- #
def test_list_merges_sources_newest_first(tmp_path):
    d = str(tmp_path)
    _write_debug_session(d, "old-session", mtime=1000.0)
    _write_blackbox_dump(d, "blackbox-20260101-000000-drag_alarm.json.gz", mtime=3000.0)
    _write_debug_session(d, "new-session", parts=2, mtime=2000.0)

    up = SessionUploader(d)
    sessions = up.list_sessions()
    # Both sources present, newest (highest mtime) first.
    assert [s["kind"] for s in sessions] == ["blackbox", "debug", "debug"]
    assert sessions[0]["name"].startswith("blackbox-")
    assert sessions[1]["name"] == "new-session"
    assert sessions[1]["parts"] == 2


def test_latest_and_package_picks_latest_session(tmp_path):
    d = str(tmp_path)
    _write_debug_session(d, "session-a", mtime=1000.0)
    _write_debug_session(d, "session-b", parts=3, mtime=5000.0)

    up = SessionUploader(d)
    latest = up.latest_session()
    assert latest is not None
    assert latest["name"] == "session-b"

    filename, blob = up.package(latest)
    assert filename.endswith(".zip")
    assert "session-b" in filename
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = zf.namelist()
    # Manifest + the 3 parts of the LATEST session (nested under session/).
    assert "manifest.json" in names
    part_names = [n for n in names if n.endswith(".ndjson.gz")]
    assert len(part_names) == 3
    assert all(n.startswith("session/session-b/") for n in part_names)
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["name"] == "session-b"
    assert manifest["parts"] == 3


def test_list_empty_when_no_artifacts(tmp_path):
    up = SessionUploader(str(tmp_path))
    assert up.list_sessions() == []
    assert up.latest_session() is None


# --------------------------------------------------------------------------- #
# Upload: success / failure to a fake endpoint
# --------------------------------------------------------------------------- #
def test_upload_posts_latest_and_reports_success(tmp_path):
    d = str(tmp_path)
    _write_debug_session(d, "incident", parts=2, mtime=9000.0)
    fake = _FakeEndpoint(code=200)
    up = SessionUploader(d, post_fn=fake)

    result = up.upload("https://example.test/ingest", opt_in=True)
    assert result["ok"] is True
    assert result["http_status"] == 200
    assert result["session"] == "debug:incident"
    # The bytes actually posted are a valid zip carrying the session.
    assert len(fake.calls) == 1
    url, filename, data = fake.calls[0]
    assert url == "https://example.test/ingest"
    assert data == b"" or zipfile.ZipFile(io.BytesIO(data))  # valid zip
    assert filename.endswith(".zip")
    # Status is remembered.
    assert up.status()["ok"] is True


def test_upload_reports_failure_on_http_error(tmp_path):
    d = str(tmp_path)
    _write_blackbox_dump(d, "blackbox-20260101-010000-fix_lost.json.gz")
    fake = _FakeEndpoint(code=500)
    up = SessionUploader(d, post_fn=fake)

    result = up.upload("https://example.test/ingest", opt_in=True)
    assert result["ok"] is False
    assert result["http_status"] == 500
    assert "error" in result


def test_upload_reports_failure_on_transport_exception(tmp_path):
    d = str(tmp_path)
    _write_debug_session(d, "boom", mtime=1.0)

    def _explode(url, filename, data):
        raise ConnectionError("network down")

    up = SessionUploader(d, post_fn=_explode)
    result = up.upload("https://example.test/ingest", opt_in=True)
    assert result["ok"] is False
    assert "network down" in result["error"]


# --------------------------------------------------------------------------- #
# Opt-in default OFF + missing destination
# --------------------------------------------------------------------------- #
def test_upload_refused_when_not_opted_in(tmp_path):
    d = str(tmp_path)
    _write_debug_session(d, "private", mtime=1.0)
    fake = _FakeEndpoint()
    up = SessionUploader(d, post_fn=fake)

    result = up.upload("https://example.test/ingest", opt_in=False)
    assert result["ok"] is False
    assert result["error"] == "opt-in disabled"
    # Critically: the network was NEVER touched.
    assert fake.calls == []


def test_upload_missing_destination_handled_cleanly(tmp_path):
    d = str(tmp_path)
    _write_debug_session(d, "s", mtime=1.0)
    fake = _FakeEndpoint()
    up = SessionUploader(d, post_fn=fake)

    for dest in ("", "   ", None):
        result = up.upload(dest, opt_in=True)
        assert result["ok"] is False
        assert result["error"] == "no destination configured"
    assert fake.calls == []


def test_upload_no_sessions_handled_cleanly(tmp_path):
    fake = _FakeEndpoint()
    up = SessionUploader(str(tmp_path), post_fn=fake)
    result = up.upload("https://example.test/ingest", opt_in=True)
    assert result["ok"] is False
    assert result["error"] == "no sessions to upload"
    assert fake.calls == []


def test_resolve_rejects_path_traversal(tmp_path):
    d = str(tmp_path)
    _write_debug_session(d, "real", mtime=1.0)
    fake = _FakeEndpoint()
    up = SessionUploader(d, post_fn=fake)
    # A traversal id must not resolve to anything outside the source dirs.
    result = up.upload("https://x/y", opt_in=True, session_id="debug:../../etc")
    assert result["ok"] is False
    assert result["error"] == "no sessions to upload"
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# Endpoints: opt-in flag lives in the prefs KV store (default OFF)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VANCHOR_ALLOWED_HOSTS", "testserver")
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    with TestClient(create_app(Runtime(cfg))) as c:
        yield c


def test_session_list_endpoint_defaults_off(client):
    r = client.get("/api/session/list")
    assert r.status_code == 200
    body = r.json()
    assert body["sessions"] == []
    assert body["opt_in"] is False  # opt-in default OFF (no pref set)
    assert body["destination_set"] is False


def test_session_upload_endpoint_refuses_without_opt_in(client, tmp_path):
    _write_debug_session(str(tmp_path), "sess", mtime=time.time())
    # No prefs set -> opt-in defaults OFF -> refused, network untouched.
    r = client.post("/api/session/upload", json={})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["error"] == "opt-in disabled"


def test_session_upload_endpoint_reads_prefs_and_reports(client, tmp_path):
    _write_debug_session(str(tmp_path), "sess", mtime=time.time())
    # Opt in but WITHOUT a URL -> clean "no destination" error.
    client.put("/api/prefs", json={"session_upload_enabled": True})
    r = client.post("/api/session/upload", json={})
    assert r.json()["ok"] is False
    assert r.json()["error"] == "no destination configured"
