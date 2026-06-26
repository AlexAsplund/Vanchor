"""Tests for the versioned backup / restore of persistent state (core.backup).

Covers: a create -> restore round-trip into a fresh data_dir (data files +
client.json match, boats/depth actually restored), the manifest shape
(schema_version + app_version + format), a from-the-future schema warning that
still restores ok, a non-vanchor / corrupt zip raising ValueError (-> 400), and
that a zip-slip entry is ignored. Plus the HTTP surface (POST /api/backup and
POST /api/restore)."""

import io
import json
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.core import backup
from vanchor.core.config import AppConfig
from vanchor.ui.server import create_app


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _seed_data_dir(d: str) -> None:
    """Write a representative set of data_dir files (+ excluded caches)."""
    os.makedirs(os.path.join(d, "trips"), exist_ok=True)
    os.makedirs(os.path.join(d, "water_cache"), exist_ok=True)
    os.makedirs(os.path.join(d, "debug"), exist_ok=True)
    with open(os.path.join(d, "boats.json"), "w") as fh:
        json.dump({"active_id": "x", "profiles": {"x": {"name": "X", "specs": {}}}}, fh)
    with open(os.path.join(d, "depthmap.json"), "w") as fh:
        json.dump({"points": [[59.0, 13.0, 4.2]]}, fh)
    with open(os.path.join(d, "devices.json"), "w") as fh:
        json.dump({"hardware": {}, "nmea_tcp": {}}, fh)
    with open(os.path.join(d, "trips", "trip-1.json"), "w") as fh:
        json.dump({"id": "trip-1", "distance_m": 100.0}, fh)
    # Excluded, regenerable caches -- must NOT end up in the archive.
    with open(os.path.join(d, "water_cache", "big.wkb"), "wb") as fh:
        fh.write(b"\x00" * 1000)
    with open(os.path.join(d, "debug", "session.ndjson.gz"), "wb") as fh:
        fh.write(b"\x1f\x8b" + b"\x00" * 100)


# ---------------------------------------------------------------------- #
# Manifest + contents
# ---------------------------------------------------------------------- #
def test_manifest_shape(tmp_path):
    _seed_data_dir(str(tmp_path))
    data = backup.create_backup(str(tmp_path), client={"vanchor-theme": "dark"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["format"] == "vanchor-backup"
    assert manifest["schema_version"] == backup.SCHEMA_VERSION == 1
    assert manifest["app_version"]  # non-empty
    assert "created_at" in manifest
    assert "boats.json" in manifest["contents"]
    assert "trips/trip-1.json" in manifest["contents"]


def test_created_at_is_passed_in_not_clock(tmp_path):
    _seed_data_dir(str(tmp_path))
    data = backup.create_backup(str(tmp_path), created_at="2020-01-02T03:04:05Z")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["created_at"] == "2020-01-02T03:04:05Z"


def test_excludes_caches(tmp_path):
    _seed_data_dir(str(tmp_path))
    data = backup.create_backup(str(tmp_path))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
    assert not any(n.startswith("water_cache") for n in names)
    assert not any(n.startswith("debug") for n in names)


def test_client_defaults_to_empty_object(tmp_path):
    _seed_data_dir(str(tmp_path))
    data = backup.create_backup(str(tmp_path), client=None)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert json.loads(zf.read("client.json")) == {}


# ---------------------------------------------------------------------- #
# Round-trip
# ---------------------------------------------------------------------- #
def test_round_trip_into_fresh_dir(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    _seed_data_dir(str(src))
    client = {"vanchor-layout": "{}", "vanchor-theme": "night"}
    data = backup.create_backup(str(src), client=client)

    result = backup.restore_backup(str(dst), data)
    assert result["ok"] is True
    assert result["client"] == client
    assert result["warnings"] == []
    assert "boats.json" in result["restored"]
    assert "trips/trip-1.json" in result["restored"]

    # Files were actually written and match the originals.
    for name in ("boats.json", "depthmap.json", "devices.json"):
        assert (dst / name).read_bytes() == (src / name).read_bytes()
    assert (dst / "trips" / "trip-1.json").read_bytes() == (
        src / "trips" / "trip-1.json"
    ).read_bytes()
    # Depth soundings round-tripped.
    assert json.loads((dst / "depthmap.json").read_text())["points"] == [[59.0, 13.0, 4.2]]


# ---------------------------------------------------------------------- #
# Schema-from-the-future
# ---------------------------------------------------------------------- #
def test_future_schema_warns_but_restores(tmp_path):
    _seed_data_dir(str(tmp_path))
    data = backup.create_backup(str(tmp_path))
    # Rewrite the manifest to a future schema and add an unknown entry.
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(buf, "w") as out:
        for info in src.infolist():
            raw = src.read(info.filename)
            if info.filename == "manifest.json":
                m = json.loads(raw)
                m["schema_version"] = backup.SCHEMA_VERSION + 5
                raw = json.dumps(m).encode()
            out.writestr(info.filename, raw)
        out.writestr("future_only.json", b"{}")

    dst = tmp_path / "dst"
    dst.mkdir()
    result = backup.restore_backup(str(dst), buf.getvalue())
    assert result["ok"] is True
    assert result["schema_version"] == backup.SCHEMA_VERSION + 5
    assert any("newer backup" in w for w in result["warnings"])
    # Unknown future entry is ignored, known ones still restored.
    assert "boats.json" in result["restored"]
    assert not (dst / "future_only.json").exists()


# ---------------------------------------------------------------------- #
# Bad input
# ---------------------------------------------------------------------- #
def test_corrupt_zip_raises(tmp_path):
    with pytest.raises(ValueError):
        backup.restore_backup(str(tmp_path), b"this is not a zip")


def test_non_vanchor_zip_raises(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format": "something-else"}))
    with pytest.raises(ValueError):
        backup.restore_backup(str(tmp_path), buf.getvalue())


def test_missing_manifest_raises(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("boats.json", "{}")
    with pytest.raises(ValueError):
        backup.restore_backup(str(tmp_path), buf.getvalue())


# ---------------------------------------------------------------------- #
# Zip-slip
# ---------------------------------------------------------------------- #
def test_zip_slip_entry_ignored(tmp_path):
    dst = tmp_path / "dst"
    dst.mkdir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format": "vanchor-backup",
                    "schema_version": 1,
                    "app_version": "0.1.0",
                    "created_at": "2020-01-01T00:00:00Z",
                    "contents": ["boats.json"],
                }
            ),
        )
        zf.writestr("boats.json", "{}")
        # Malicious traversal entries.
        zf.writestr("../escape.json", "pwned")
        zf.writestr("/abs/escape.json", "pwned")
        zf.writestr("trips/../../escape2.json", "pwned")

    result = backup.restore_backup(str(dst), buf.getvalue())
    assert "boats.json" in result["restored"]
    assert any("unsafe" in w for w in result["warnings"])
    # Nothing escaped the destination dir.
    assert not (tmp_path / "escape.json").exists()
    assert not (tmp_path / "escape2.json").exists()
    assert not os.path.exists("/abs/escape.json")


# ---------------------------------------------------------------------- #
# Runtime wiring
# ---------------------------------------------------------------------- #
def test_runtime_create_and_restore(tmp_path):
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path / "rt")
    os.makedirs(cfg.data_dir, exist_ok=True)
    rt = Runtime(cfg)
    # Runtime seeds boats.json on construction; back it up.
    data = rt.create_backup(client={"vanchor-x": "1"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert "boats.json" in zf.namelist()

    # Restore into the same runtime (synchronous path -> restart_required True
    # because there is no running event loop to reload devices live).
    result = rt.restore_backup(data)
    assert result["ok"] is True
    assert "restart_required" in result
    assert result["client"] == {"vanchor-x": "1"}


# ---------------------------------------------------------------------- #
# HTTP surface
# ---------------------------------------------------------------------- #
@pytest.fixture()
def client(tmp_path):
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path / "api")
    os.makedirs(cfg.data_dir, exist_ok=True)
    app = create_app(Runtime(cfg))
    with TestClient(app) as c:
        yield c


def test_api_backup_then_restore(client):
    r = client.post("/api/backup", json={"client": {"vanchor-theme": "dark"}})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    assert "vanchor-backup-" in r.headers["content-disposition"]
    zip_bytes = r.content
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        assert "manifest.json" in zf.namelist()

    r2 = client.post(
        "/api/restore",
        files={"file": ("backup.zip", zip_bytes, "application/zip")},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["ok"] is True
    assert body["client"] == {"vanchor-theme": "dark"}


def test_api_backup_no_body(client):
    r = client.post("/api/backup")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert json.loads(zf.read("client.json")) == {}


def test_api_restore_bad_zip_is_400(client):
    r = client.post(
        "/api/restore",
        files={"file": ("bad.zip", b"not a zip at all", "application/zip")},
    )
    assert r.status_code == 400
    assert r.json()["ok"] is False
