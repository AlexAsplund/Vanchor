"""Self-update installer for the supervisor package.

Installs a new supervisor version from a supervisor bundle, with atomic
symlink flip and boot-count rollback support via pending.json.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tarfile
from pathlib import Path

from .bundles import read_manifest, verify_payload

log = logging.getLogger(__name__)

_PENDING = "pending.json"
_CURRENT = "current"


def install(bundle_path: Path, install_root: Path) -> str:
    """Install a new supervisor version from a supervisor bundle.

    Steps:
    1. Read manifest (must be kind=supervisor), verify payload sha256.
    2. Extract payload.tar.gz to <install_root>/versions/<ver>/.
    3. Sanity-import check via subprocess.
    4. Write pending.json.
    5. Atomically flip the 'current' symlink.

    Returns the new version string.
    Raises ValueError / RuntimeError on any failure.
    """
    bundle_path = Path(bundle_path)
    install_root = Path(install_root)

    manifest = read_manifest(bundle_path)
    if manifest.get("kind") != "supervisor":
        raise ValueError(
            f"Expected supervisor bundle, got kind={manifest.get('kind')!r}"
        )

    new_version = manifest["supervisor_version"]
    log.info("Installing supervisor version %s from %s", new_version, bundle_path)

    payload_path = verify_payload(bundle_path, manifest)

    # Extract payload into versions/<ver>/
    versions_dir = install_root / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    target_dir = versions_dir / new_version

    if target_dir.exists():
        log.warning("Version directory %s already exists — overwriting", target_dir)
        import shutil
        shutil.rmtree(str(target_dir))

    target_dir.mkdir(parents=True)

    log.info("Extracting payload to %s", target_dir)
    with tarfile.open(str(payload_path), "r:gz") as tf:
        for member in tf.getmembers():
            from .bundles import _is_safe_member
            if not _is_safe_member(member.name):
                raise ValueError(f"Unsafe member in payload: {member.name!r}")
        tf.extractall(str(target_dir))

    # Sanity-import check
    _sanity_check(target_dir)

    # Determine previous version from current symlink
    current_link = install_root / _CURRENT
    previous_version: str | None = None
    if current_link.is_symlink():
        try:
            previous_target = Path(os.readlink(str(current_link)))
            previous_version = previous_target.name
        except Exception:
            pass

    # Write pending.json — guard.py reads "target" and "previous" keys
    pending = {
        "target": new_version,
        "previous": previous_version,
        "boots": 0,
    }
    pending_path = install_root / _PENDING
    pending_path.write_text(json.dumps(pending, indent=2))
    log.info("Wrote pending.json: %s", pending)

    # Atomic symlink flip: current -> target_dir
    _atomic_symlink(current_link, target_dir)
    log.info("Supervisor current symlink → %s", target_dir)

    return new_version


def clear_pending(install_root: Path) -> dict | None:
    """Read and delete pending.json.  Returns its content or None if absent."""
    install_root = Path(install_root)
    pending_path = install_root / _PENDING
    if not pending_path.exists():
        return None
    try:
        data = json.loads(pending_path.read_text())
    except Exception as exc:
        log.warning("Failed to read pending.json: %s", exc)
        data = None
    try:
        pending_path.unlink(missing_ok=True)
    except Exception as exc:
        log.warning("Failed to delete pending.json: %s", exc)
    return data


def read_pending(install_root: Path) -> dict | None:
    """Return the content of pending.json without deleting it, or None."""
    install_root = Path(install_root)
    pending_path = install_root / _PENDING
    if not pending_path.exists():
        return None
    try:
        return json.loads(pending_path.read_text())
    except Exception as exc:
        log.warning("Failed to read pending.json: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sanity_check(target_dir: Path) -> None:
    """Import-check the installed package in a subprocess.

    Raises RuntimeError if the check fails.
    The package is expected to be at target_dir/vanchor_supervisor/__init__.py,
    so sys.path is set to target_dir itself (not its parent).
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, r'"
            + str(target_dir)
            + "'); import vanchor_supervisor; print(vanchor_supervisor.SUPERVISOR_VERSION)",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Sanity import check failed for {target_dir}:\n{result.stderr}"
        )
    log.info("Sanity check passed: %s", result.stdout.strip())


def _atomic_symlink(link_path: Path, target: Path) -> None:
    """Atomically replace link_path with a symlink to target."""
    tmp = link_path.parent / (link_path.name + ".tmp")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    os.symlink(str(target), str(tmp))
    os.replace(str(tmp), str(link_path))
