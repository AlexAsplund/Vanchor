"""Bundle reading, validation, and payload extraction."""
from __future__ import annotations

import hashlib
import json
import os
import tarfile
import tempfile
from pathlib import Path
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _is_safe_member(name: str) -> bool:
    """Return True if the tar member name is safe (no absolute paths, no ..)."""
    if os.path.isabs(name):
        return False
    parts = Path(name).parts
    return ".." not in parts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_manifest(tar_path: str | Path) -> dict:
    """Open the outer bundle tar and parse manifest.json.

    Validates:
    - format == "vanchor-bundle"
    - No absolute or path-traversal member names

    Raises ValueError on any validation failure.
    """
    tar_path = Path(tar_path)
    with tarfile.open(str(tar_path), "r") as tf:
        # Safety check: reject any member with a dangerous name
        for member in tf.getmembers():
            if not _is_safe_member(member.name):
                raise ValueError(f"unsafe tar member: {member.name!r}")

        try:
            mf = tf.getmember("manifest.json")
        except KeyError:
            raise ValueError("Bundle does not contain manifest.json")

        fh = tf.extractfile(mf)
        if fh is None:
            raise ValueError("manifest.json is not a regular file in the bundle")
        try:
            manifest = json.loads(fh.read())
        except json.JSONDecodeError as exc:
            raise ValueError(f"manifest.json is not valid JSON: {exc}") from exc

    if manifest.get("format") != "vanchor-bundle":
        raise ValueError(
            f"unexpected bundle format: {manifest.get('format')!r} (expected 'vanchor-bundle')"
        )

    return manifest


def verify_payload(
    tar_path: str | Path,
    manifest: dict,
    progress_cb: Optional[Callable[[float], None]] = None,
) -> Path:
    """Extract the payload from a bundle, verify its sha256, and return its path.

    The payload is extracted into a temporary directory (caller is responsible
    for cleanup if needed).  For app bundles the payload is image.tar.gz; for
    supervisor bundles it is payload.tar.gz.

    progress_cb(pct) is called periodically with a float percentage 0.0–100.0.

    Returns the Path to the extracted payload file.
    Raises ValueError on sha256 mismatch.
    """
    tar_path = Path(tar_path)
    kind = manifest.get("kind")

    if kind == "app":
        payload_name = "image.tar.gz"
        expected_sha = manifest.get("image_sha256", "")
    elif kind == "supervisor":
        payload_name = "payload.tar.gz"
        expected_sha = manifest.get("payload_sha256", "")
    else:
        raise ValueError(f"Unknown bundle kind: {kind!r}")

    tmpdir = tempfile.mkdtemp(prefix="vanchor-bundle-")
    out_path = Path(tmpdir) / payload_name

    with tarfile.open(str(tar_path), "r") as tf:
        # Safety pass
        members = tf.getmembers()
        for member in members:
            if not _is_safe_member(member.name):
                raise ValueError(f"unsafe tar member: {member.name!r}")

        try:
            payload_member = tf.getmember(payload_name)
        except KeyError:
            raise ValueError(f"Bundle does not contain {payload_name!r}")

        fh = tf.extractfile(payload_member)
        if fh is None:
            raise ValueError(f"{payload_name!r} is not a regular file in the bundle")

        total = payload_member.size
        done = 0
        h = hashlib.sha256()
        chunk_size = 256 * 1024  # 256 KB

        with open(str(out_path), "wb") as out_fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                out_fh.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress_cb and total > 0:
                    progress_cb(round(done / total * 100.0, 1))

    actual_sha = h.hexdigest()
    if actual_sha != expected_sha:
        raise ValueError(
            f"sha256 mismatch for {payload_name}: "
            f"expected {expected_sha!r}, got {actual_sha!r}"
        )

    return out_path
