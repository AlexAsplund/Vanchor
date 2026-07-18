"""Tests for vanchor_supervisor.selfupdate + guard.py."""
from __future__ import annotations
import json
import os
import sys
import tarfile
import io
import hashlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import make_bundle  # noqa: E402

from vanchor_supervisor.selfupdate import install, clear_pending, read_pending


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _make_supervisor_bundle(tmp_path: Path, version: str = "0.1.1") -> Path:
    """Build a minimal supervisor bundle for testing.

    The supervisor package dir must be named 'vanchor_supervisor' so
    selfupdate._sanity_check can import it at <target_dir>/vanchor_supervisor.
    """
    sup_dir = tmp_path / "vanchor_supervisor"
    sup_dir.mkdir(exist_ok=True)
    (sup_dir / "__init__.py").write_text(
        f'SUPERVISOR_VERSION = "{version}"\nAPI_VERSION = 1\n'
    )
    guard = tmp_path / "guard.py"
    guard.write_text("# guard\n")
    out = tmp_path / f"vanchor-supervisor-{version}.bundle.tar"
    make_bundle.make_supervisor_bundle(
        supervisor_dir=sup_dir,
        guard_path=guard,
        version=version,
        out=out,
    )
    return out


# ------------------------------------------------------------------ #
# install()
# ------------------------------------------------------------------ #

def test_install_extracts_to_versions_dir(tmp_path):
    bundle = _make_supervisor_bundle(tmp_path, version="0.1.1")
    install_root = tmp_path / "supervisor"
    install_root.mkdir()
    (install_root / "versions").mkdir()
    # Create a "current" symlink pointing to an old version
    old_ver = install_root / "versions" / "0.1.0"
    old_ver.mkdir()
    (old_ver / "__init__.py").write_text('SUPERVISOR_VERSION = "0.1.0"\n')
    current = install_root / "current"
    os.symlink(str(old_ver), str(current))

    new_ver = install(bundle, install_root)
    assert new_ver == "0.1.1"

    version_dir = install_root / "versions" / "0.1.1"
    assert version_dir.exists()
    init_file = version_dir / "vanchor_supervisor" / "__init__.py"
    assert init_file.exists()


def test_install_writes_pending_json(tmp_path):
    bundle = _make_supervisor_bundle(tmp_path, version="0.1.1")
    install_root = tmp_path / "supervisor"
    install_root.mkdir()
    (install_root / "versions").mkdir()
    old_ver = install_root / "versions" / "0.1.0"
    old_ver.mkdir()
    (old_ver / "__init__.py").write_text('SUPERVISOR_VERSION = "0.1.0"\n')
    os.symlink(str(old_ver), str(install_root / "current"))

    install(bundle, install_root)

    pending = json.loads((install_root / "pending.json").read_text())
    assert pending["target"] == "0.1.1"
    assert pending["previous"] == "0.1.0"
    assert pending["boots"] == 0


def test_install_flips_symlink(tmp_path):
    bundle = _make_supervisor_bundle(tmp_path, version="0.1.1")
    install_root = tmp_path / "supervisor"
    install_root.mkdir()
    (install_root / "versions").mkdir()
    old_ver = install_root / "versions" / "0.1.0"
    old_ver.mkdir()
    (old_ver / "__init__.py").write_text('SUPERVISOR_VERSION = "0.1.0"\n')
    os.symlink(str(old_ver), str(install_root / "current"))

    install(bundle, install_root)

    current_target = os.readlink(str(install_root / "current"))
    assert "0.1.1" in current_target


# ------------------------------------------------------------------ #
# clear_pending / read_pending
# ------------------------------------------------------------------ #

def test_clear_pending_returns_data(tmp_path):
    install_root = tmp_path / "supervisor"
    install_root.mkdir()
    pending = {"target": "0.1.1", "previous": "0.1.0", "boots": 1}
    (install_root / "pending.json").write_text(json.dumps(pending))

    result = clear_pending(install_root)
    assert result is not None
    assert result["target"] == "0.1.1"
    assert not (install_root / "pending.json").exists()


def test_clear_pending_no_file_returns_none(tmp_path):
    install_root = tmp_path / "supervisor"
    install_root.mkdir()
    result = clear_pending(install_root)
    assert result is None


def test_read_pending_returns_data(tmp_path):
    install_root = tmp_path / "supervisor"
    install_root.mkdir()
    pending = {"target": "0.1.1", "previous": "0.1.0", "boots": 0}
    (install_root / "pending.json").write_text(json.dumps(pending))

    result = read_pending(install_root)
    assert result == pending


# ------------------------------------------------------------------ #
# guard.py
# ------------------------------------------------------------------ #

def test_guard_increments_boots(tmp_path, monkeypatch):
    """Guard increments boots and exits 0 on first/second boot."""
    guard_path = Path(__file__).parent.parent / "supervisor" / "guard.py"
    install_root = tmp_path / "supervisor"
    install_root.mkdir()
    (install_root / "versions").mkdir()
    pending = {"target": "0.1.1", "previous": "0.1.0", "boots": 0}
    (install_root / "pending.json").write_text(json.dumps(pending))

    # Set SUPERVISOR_INSTALL_ROOT so guard knows where to look
    monkeypatch.setenv("SUPERVISOR_INSTALL_ROOT", str(install_root))

    # Run guard
    import runpy
    import types
    # We need to run guard.py as __main__ in our process — use exec
    guard_ns: dict = {}
    exec(guard_path.read_text(), guard_ns)
    # Call main() manually
    guard_ns["INSTALL_ROOT"] = install_root
    guard_ns["main"]()

    data = json.loads((install_root / "pending.json").read_text())
    assert data["boots"] == 1


def test_guard_reverts_on_third_boot(tmp_path, monkeypatch):
    """Guard reverts symlink and clears pending on boots >= 3."""
    guard_path = Path(__file__).parent.parent / "supervisor" / "guard.py"
    install_root = tmp_path / "supervisor"
    install_root.mkdir()
    (install_root / "versions").mkdir()

    # Set up previous version dir
    old_ver = install_root / "versions" / "0.1.0"
    old_ver.mkdir()
    new_ver = install_root / "versions" / "0.1.1"
    new_ver.mkdir()
    # Current points to new version
    os.symlink(str(new_ver), str(install_root / "current"))

    # pending.json shows 2 boots already
    pending = {"target": "0.1.1", "previous": "0.1.0", "boots": 2}
    (install_root / "pending.json").write_text(json.dumps(pending))

    monkeypatch.setenv("SUPERVISOR_INSTALL_ROOT", str(install_root))

    guard_ns: dict = {}
    exec(guard_path.read_text(), guard_ns)
    guard_ns["INSTALL_ROOT"] = install_root
    guard_ns["main"]()

    # pending.json should be deleted
    assert not (install_root / "pending.json").exists()
    # current symlink should now point to old version
    target = os.readlink(str(install_root / "current"))
    assert "0.1.0" in target


def test_guard_exits_0_on_corrupt_pending(tmp_path, monkeypatch):
    """Guard must exit 0 even when pending.json is corrupt."""
    guard_path = Path(__file__).parent.parent / "supervisor" / "guard.py"
    install_root = tmp_path / "supervisor"
    install_root.mkdir()
    (install_root / "pending.json").write_text("not-valid-json{{{")

    monkeypatch.setenv("SUPERVISOR_INSTALL_ROOT", str(install_root))

    guard_ns: dict = {}
    exec(guard_path.read_text(), guard_ns)
    guard_ns["INSTALL_ROOT"] = install_root
    # Should not raise
    guard_ns["main"]()
