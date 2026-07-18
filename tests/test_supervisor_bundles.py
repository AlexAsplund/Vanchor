"""Tests for vanchor_supervisor.bundles — manifest reading + payload verification."""
from __future__ import annotations
import hashlib
import json
import os
import sys
import tarfile
import io
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import make_bundle  # noqa: E402

from vanchor_supervisor.bundles import read_manifest, verify_payload, _is_safe_member


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _make_app_bundle(tmp_path: Path, *, flip_byte: bool = False) -> Path:
    """Build a minimal app bundle for testing."""
    image_gz = tmp_path / "image.tar.gz"
    image_gz.write_bytes(b"\x1f\x8b" + b"x" * 200)
    out = tmp_path / "test-app.bundle.tar"
    make_bundle.make_app_bundle(
        image_tar_gz=image_gz,
        image="ghcr.io/alexasplund/vanchor",
        tag="1.5.0a9",
        min_supervisor="0.1.0",
        arch="arm64",
        out=out,
    )
    if flip_byte:
        # Corrupt the image payload inside the bundle
        with tarfile.open(out, "r:") as tf:
            manifest = json.loads(tf.extractfile("manifest.json").read())
            img_bytes = bytearray(tf.extractfile("image.tar.gz").read())
        img_bytes[10] ^= 0xFF  # flip one byte
        # Rebuild with the corrupted image
        bad = tmp_path / "bad.bundle.tar"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:") as tf_out:
            m_bytes = json.dumps(manifest).encode()
            ti = tarfile.TarInfo("manifest.json")
            ti.size = len(m_bytes)
            tf_out.addfile(ti, io.BytesIO(m_bytes))
            ti2 = tarfile.TarInfo("image.tar.gz")
            ti2.size = len(img_bytes)
            tf_out.addfile(ti2, io.BytesIO(bytes(img_bytes)))
        bad.write_bytes(buf.getvalue())
        return bad
    return out


def _make_bad_bundle(tmp_path: Path, *, kind: str) -> Path:
    """Build a bundle with a specific defect."""
    out = tmp_path / f"bad-{kind}.bundle.tar"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        if kind == "wrong_format":
            manifest = {"format": "wrong", "schema_version": 1}
            m_bytes = json.dumps(manifest).encode()
            ti = tarfile.TarInfo("manifest.json")
            ti.size = len(m_bytes)
            tf.addfile(ti, io.BytesIO(m_bytes))
        elif kind == "no_manifest":
            dummy = b"nothing"
            ti = tarfile.TarInfo("other.txt")
            ti.size = len(dummy)
            tf.addfile(ti, io.BytesIO(dummy))
        elif kind == "dotdot_member":
            manifest = {"format": "vanchor-bundle", "schema_version": 1, "kind": "app",
                        "name": "vanchor", "image_sha256": "abc", "tag": "1.0.0"}
            m_bytes = json.dumps(manifest).encode()
            ti = tarfile.TarInfo("manifest.json")
            ti.size = len(m_bytes)
            tf.addfile(ti, io.BytesIO(m_bytes))
            evil = b"evil"
            ti2 = tarfile.TarInfo("../evil.py")
            ti2.size = len(evil)
            tf.addfile(ti2, io.BytesIO(evil))
        elif kind == "absolute_member":
            manifest = {"format": "vanchor-bundle", "schema_version": 1, "kind": "app",
                        "name": "vanchor", "image_sha256": "abc", "tag": "1.0.0"}
            m_bytes = json.dumps(manifest).encode()
            ti = tarfile.TarInfo("manifest.json")
            ti.size = len(m_bytes)
            tf.addfile(ti, io.BytesIO(m_bytes))
            evil = b"evil"
            ti2 = tarfile.TarInfo("/etc/cron.d/evil")
            ti2.size = len(evil)
            tf.addfile(ti2, io.BytesIO(evil))
    out.write_bytes(buf.getvalue())
    return out


# ------------------------------------------------------------------ #
# _is_safe_member
# ------------------------------------------------------------------ #

def test_safe_member_normal():
    assert _is_safe_member("image.tar.gz")
    assert _is_safe_member("manifest.json")
    assert _is_safe_member("vanchor_supervisor/__init__.py")


def test_safe_member_dotdot():
    assert not _is_safe_member("../evil")
    assert not _is_safe_member("foo/../../../etc/passwd")


def test_safe_member_absolute():
    assert not _is_safe_member("/etc/passwd")
    assert not _is_safe_member("/absolute/path")


# ------------------------------------------------------------------ #
# read_manifest
# ------------------------------------------------------------------ #

def test_read_manifest_ok(tmp_path):
    bundle = _make_app_bundle(tmp_path)
    manifest = read_manifest(bundle)
    assert manifest["format"] == "vanchor-bundle"
    assert manifest["kind"] == "app"
    assert manifest["tag"] == "1.5.0a9"


def test_read_manifest_wrong_format(tmp_path):
    bundle = _make_bad_bundle(tmp_path, kind="wrong_format")
    with pytest.raises(ValueError, match="format"):
        read_manifest(bundle)


def test_read_manifest_no_manifest(tmp_path):
    bundle = _make_bad_bundle(tmp_path, kind="no_manifest")
    with pytest.raises((ValueError, KeyError)):
        read_manifest(bundle)


def test_read_manifest_dotdot_member(tmp_path):
    """Bundle with ../evil member must be rejected (path traversal)."""
    bundle = _make_bad_bundle(tmp_path, kind="dotdot_member")
    with pytest.raises(ValueError, match="unsafe"):
        read_manifest(bundle)


def test_read_manifest_absolute_member(tmp_path):
    bundle = _make_bad_bundle(tmp_path, kind="absolute_member")
    with pytest.raises(ValueError, match="unsafe"):
        read_manifest(bundle)


# ------------------------------------------------------------------ #
# verify_payload
# ------------------------------------------------------------------ #

def test_verify_payload_ok(tmp_path):
    bundle = _make_app_bundle(tmp_path)
    manifest = read_manifest(bundle)
    calls = []
    payload_path = verify_payload(bundle, manifest, progress_cb=calls.append)
    assert payload_path.exists()
    assert payload_path.name == "image.tar.gz"
    assert len(calls) > 0  # progress_cb was called


def test_verify_payload_sha_mismatch(tmp_path):
    bundle = _make_app_bundle(tmp_path, flip_byte=True)
    manifest = read_manifest(bundle)
    with pytest.raises(ValueError, match="sha256"):
        verify_payload(bundle, manifest)


def test_verify_payload_progress_cb_called(tmp_path):
    bundle = _make_app_bundle(tmp_path)
    manifest = read_manifest(bundle)
    progress_values = []
    verify_payload(bundle, manifest, progress_cb=progress_values.append)
    assert len(progress_values) > 0
    assert all(0 <= v <= 100 for v in progress_values)
