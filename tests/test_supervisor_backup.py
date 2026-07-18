"""Tests for vanchor_supervisor.backup — volume snapshot create/restore."""
from __future__ import annotations
import json
import os
import tarfile
from pathlib import Path

import pytest

from vanchor_supervisor.backup import create, enforce_retention, restore
from supervisor_fakes import FakeDockerBackend, FakeHealth


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _seed_volume(vol: Path) -> None:
    """Write representative files in a fake data volume."""
    (vol / "boats.json").write_text(json.dumps({"active_id": "x"}))
    (vol / "devices.json").write_text(json.dumps({"hardware": {}}))
    (vol / "trips").mkdir(exist_ok=True)
    (vol / "trips" / "t1.json").write_text(json.dumps({"id": "t1"}))
    (vol / "updates").mkdir(exist_ok=True)
    (vol / "updates" / "bundle.tar").write_bytes(b"bundle")
    (vol / "water_cache").mkdir(exist_ok=True)
    (vol / "water_cache" / "big.wkb").write_bytes(b"\x00" * 500)


# ------------------------------------------------------------------ #
# create
# ------------------------------------------------------------------ #

def test_create_returns_path(tmp_path):
    vol = tmp_path / "vol"
    vol.mkdir()
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    _seed_volume(vol)

    path = create(vol, out_dir, created_at="2026-07-18T00:00:00Z")
    assert path.exists()
    assert path.suffix == ".gz"
    assert "vanchor-data" in path.name


def test_create_produces_valid_tar_gz(tmp_path):
    vol = tmp_path / "vol"
    vol.mkdir()
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    _seed_volume(vol)

    path = create(vol, out_dir, created_at="2026-07-18T00:00:00Z")
    assert tarfile.is_tarfile(path)


def test_create_excludes_updates_dir(tmp_path):
    vol = tmp_path / "vol"
    vol.mkdir()
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    _seed_volume(vol)

    path = create(vol, out_dir, created_at="2026-07-18T00:00:00Z")
    with tarfile.open(path, "r:gz") as tf:
        names = tf.getnames()
    # updates/ should not be in the archive
    assert not any("updates" in n for n in names), (
        f"'updates' dir should be excluded, but found in: {names}"
    )


def test_create_includes_data_files(tmp_path):
    vol = tmp_path / "vol"
    vol.mkdir()
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    _seed_volume(vol)

    path = create(vol, out_dir, created_at="2026-07-18T00:00:00Z")
    with tarfile.open(path, "r:gz") as tf:
        names = tf.getnames()
    # boats.json and trips should be present
    assert any("boats.json" in n for n in names)
    assert any("trips" in n for n in names)


# ------------------------------------------------------------------ #
# enforce_retention
# ------------------------------------------------------------------ #

def test_retention_keeps_newest_n(tmp_path):
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    # Create 7 fake backup files with different timestamps
    files = []
    for i in range(7):
        p = out_dir / f"vanchor-data-2026071{i}T000000.tar.gz"
        p.write_bytes(b"x" * (i + 1))
        # Adjust mtime for ordering
        os.utime(p, (i * 1000, i * 1000))
        files.append(p)

    deleted = enforce_retention(out_dir, keep=5)
    remaining = list(out_dir.glob("*.tar.gz"))
    assert len(remaining) == 5
    assert len(deleted) == 2
    # The OLDEST files should be gone
    for p in deleted:
        assert not p.exists()


def test_retention_noop_when_under_limit(tmp_path):
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    for i in range(3):
        p = out_dir / f"vanchor-data-20260{i}.tar.gz"
        p.write_bytes(b"x")
    deleted = enforce_retention(out_dir, keep=5)
    assert deleted == []
    assert len(list(out_dir.glob("*.tar.gz"))) == 3


# ------------------------------------------------------------------ #
# Safety check: unsafe member in restore
# ------------------------------------------------------------------ #

def test_restore_rejects_unsafe_member(tmp_path):
    import io
    vol = tmp_path / "vol"
    vol.mkdir()
    out_dir = tmp_path / "backups"
    out_dir.mkdir()

    # Build a tar.gz with a path-traversal member
    bad_path = out_dir / "evil.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        evil = b"evil content"
        ti = tarfile.TarInfo("../../etc/cron.d/evil")
        ti.size = len(evil)
        tf.addfile(ti, io.BytesIO(evil))
    bad_path.write_bytes(buf.getvalue())

    backend = FakeDockerBackend(volume_root=vol)
    backend.containers["vanchor"] = {"name": "vanchor", "state": "running"}

    with pytest.raises(ValueError, match="unsafe|traversal"):
        restore(
            volume_root=vol,
            tar_path=bad_path,
            backend=backend,
            container_name="vanchor",
            health_url="http://127.0.0.1:8000/api/state",
            settings=None,  # not needed for this check
        )


# ------------------------------------------------------------------ #
# Round-trip: create + restore
# ------------------------------------------------------------------ #

def test_create_restore_roundtrip(tmp_path):
    """Create a backup, then restore it into a fresh volume — content matches."""
    from vanchor_supervisor.config import SupervisorSettings
    settings = SupervisorSettings()
    settings.health_gate_s = 1.0
    settings.health_ok_count = 1
    settings.health_poll_s = 0.01

    vol_src = tmp_path / "vol_src"
    vol_src.mkdir()
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    _seed_volume(vol_src)

    tar_path = create(vol_src, out_dir, created_at="2026-07-18T00:00:00Z")

    vol_dst = tmp_path / "vol_dst"
    vol_dst.mkdir()

    health = FakeHealth([200] * 10)
    backend = FakeDockerBackend(volume_root=vol_dst)
    backend.containers["vanchor"] = {"name": "vanchor", "state": "running"}

    restore(
        volume_root=vol_dst,
        tar_path=tar_path,
        backend=backend,
        container_name="vanchor",
        health_url="http://127.0.0.1:8000/api/state",
        settings=settings,
        health_fetch=health,
        sleep=lambda _: None,
    )

    # boats.json should be restored
    assert (vol_dst / "boats.json").exists()
    src_data = json.loads((vol_src / "boats.json").read_text())
    dst_data = json.loads((vol_dst / "boats.json").read_text())
    assert src_data == dst_data
