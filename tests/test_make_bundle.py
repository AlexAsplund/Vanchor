"""Tests for scripts/make_bundle.py — bundle creation + round-trip."""
from __future__ import annotations
import hashlib
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

# Add scripts/ to the path so we can import make_bundle directly.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import make_bundle  # noqa: E402


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture()
def fake_image_tar_gz(tmp_path) -> Path:
    """A minimal fake image.tar.gz (any bytes; the fake backend won't parse it)."""
    p = tmp_path / "image.tar.gz"
    p.write_bytes(b"\x1f\x8b" + b"fake-docker-image-content" * 100)
    return p


# ------------------------------------------------------------------ #
# App bundle
# ------------------------------------------------------------------ #

def test_app_bundle_members(tmp_path, fake_image_tar_gz):
    out = tmp_path / "test.bundle.tar"
    result = make_bundle.make_app_bundle(
        image_tar_gz=fake_image_tar_gz,
        image="ghcr.io/alexasplund/vanchor",
        tag="1.5.0a9",
        min_supervisor="0.1.0",
        arch="arm64",
        out=out,
    )
    assert result == out
    assert out.exists()
    with tarfile.open(out, "r:") as tf:
        names = tf.getnames()
    assert "manifest.json" in names
    assert "image.tar.gz" in names
    assert len(names) == 2, f"Unexpected extra members: {names}"


def test_app_bundle_manifest_fields(tmp_path, fake_image_tar_gz):
    out = tmp_path / "test.bundle.tar"
    make_bundle.make_app_bundle(
        image_tar_gz=fake_image_tar_gz,
        image="ghcr.io/alexasplund/vanchor",
        tag="1.5.0a9",
        min_supervisor="0.1.0",
        arch="arm64",
        out=out,
    )
    with tarfile.open(out, "r:") as tf:
        manifest = json.loads(tf.extractfile("manifest.json").read())

    assert manifest["format"] == "vanchor-bundle"
    assert manifest["schema_version"] == 1
    assert manifest["kind"] == "app"
    assert manifest["name"] == "vanchor"
    assert manifest["app_version"] == "1.5.0a9"
    assert manifest["image"] == "ghcr.io/alexasplund/vanchor"
    assert manifest["tag"] == "1.5.0a9"
    assert manifest["arch"] == "arm64"
    assert manifest["min_supervisor"] == "0.1.0"
    assert "image_sha256" in manifest
    assert "created_at" in manifest


def test_app_bundle_sha256_matches_payload(tmp_path, fake_image_tar_gz):
    out = tmp_path / "test.bundle.tar"
    make_bundle.make_app_bundle(
        image_tar_gz=fake_image_tar_gz,
        image="ghcr.io/alexasplund/vanchor",
        tag="1.5.0a9",
        min_supervisor="0.1.0",
        arch="arm64",
        out=out,
    )
    with tarfile.open(out, "r:") as tf:
        manifest = json.loads(tf.extractfile("manifest.json").read())
        image_bytes = tf.extractfile("image.tar.gz").read()

    assert hashlib.sha256(image_bytes).hexdigest() == manifest["image_sha256"]


def test_app_bundle_manifest_first(tmp_path, fake_image_tar_gz):
    out = tmp_path / "test.bundle.tar"
    make_bundle.make_app_bundle(
        image_tar_gz=fake_image_tar_gz,
        image="ghcr.io/alexasplund/vanchor",
        tag="1.5.0a9",
        min_supervisor="0.1.0",
        arch="arm64",
        out=out,
    )
    with tarfile.open(out, "r:") as tf:
        names = tf.getnames()
    assert names[0] == "manifest.json", "manifest.json must be first member"


def test_app_bundle_deterministic(tmp_path, fake_image_tar_gz):
    out1 = tmp_path / "a.bundle.tar"
    out2 = tmp_path / "b.bundle.tar"
    kwargs = dict(
        image_tar_gz=fake_image_tar_gz,
        image="ghcr.io/alexasplund/vanchor",
        tag="1.5.0a9",
        min_supervisor="0.1.0",
        arch="arm64",
    )
    make_bundle.make_app_bundle(**kwargs, out=out1)
    make_bundle.make_app_bundle(**kwargs, out=out2)
    # Sizes should match (content is deterministic except timestamps)
    with tarfile.open(out1, "r:") as tf1, tarfile.open(out2, "r:") as tf2:
        m1 = json.loads(tf1.extractfile("manifest.json").read())
        m2 = json.loads(tf2.extractfile("manifest.json").read())
    # sha256 of the payload must be the same across runs
    assert m1["image_sha256"] == m2["image_sha256"]


# ------------------------------------------------------------------ #
# Supervisor bundle
# ------------------------------------------------------------------ #

@pytest.fixture()
def supervisor_dir(tmp_path) -> Path:
    """A minimal fake supervisor package directory."""
    d = tmp_path / "vanchor_supervisor"
    d.mkdir()
    (d / "__init__.py").write_text('SUPERVISOR_VERSION = "0.1.0"\nAPI_VERSION = 1\n')
    (d / "config.py").write_text("# config\n")
    return d


@pytest.fixture()
def guard_path(tmp_path) -> Path:
    p = tmp_path / "guard.py"
    p.write_text("# guard\n")
    return p


def test_supervisor_bundle_members(tmp_path, supervisor_dir, guard_path):
    out = tmp_path / "sup.bundle.tar"
    make_bundle.make_supervisor_bundle(
        supervisor_dir=supervisor_dir,
        guard_path=guard_path,
        version="0.1.0",
        out=out,
    )
    assert out.exists()
    with tarfile.open(out, "r:") as tf:
        names = tf.getnames()
    assert "manifest.json" in names
    assert "payload.tar.gz" in names
    assert len(names) == 2, f"Unexpected extra members: {names}"


def test_supervisor_bundle_manifest_fields(tmp_path, supervisor_dir, guard_path):
    out = tmp_path / "sup.bundle.tar"
    make_bundle.make_supervisor_bundle(
        supervisor_dir=supervisor_dir,
        guard_path=guard_path,
        version="0.1.0",
        out=out,
    )
    with tarfile.open(out, "r:") as tf:
        manifest = json.loads(tf.extractfile("manifest.json").read())

    assert manifest["format"] == "vanchor-bundle"
    assert manifest["schema_version"] == 1
    assert manifest["kind"] == "supervisor"
    assert manifest["supervisor_version"] == "0.1.0"
    assert "payload_sha256" in manifest
    assert "created_at" in manifest


def test_supervisor_bundle_sha256_matches(tmp_path, supervisor_dir, guard_path):
    out = tmp_path / "sup.bundle.tar"
    make_bundle.make_supervisor_bundle(
        supervisor_dir=supervisor_dir,
        guard_path=guard_path,
        version="0.1.0",
        out=out,
    )
    with tarfile.open(out, "r:") as tf:
        manifest = json.loads(tf.extractfile("manifest.json").read())
        payload_bytes = tf.extractfile("payload.tar.gz").read()

    assert hashlib.sha256(payload_bytes).hexdigest() == manifest["payload_sha256"]


def test_supervisor_bundle_manifest_first(tmp_path, supervisor_dir, guard_path):
    out = tmp_path / "sup.bundle.tar"
    make_bundle.make_supervisor_bundle(
        supervisor_dir=supervisor_dir,
        guard_path=guard_path,
        version="0.1.0",
        out=out,
    )
    with tarfile.open(out, "r:") as tf:
        names = tf.getnames()
    assert names[0] == "manifest.json"
