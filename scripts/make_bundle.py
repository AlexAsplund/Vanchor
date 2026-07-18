#!/usr/bin/env python3
"""Build Vanchor distribution bundles (stdlib-only).

App bundle:
    python scripts/make_bundle.py app \\
        --image ghcr.io/alexasplund/vanchor --tag 1.5.0a9 \\
        --min-supervisor 0.1.0 --arch arm64 \\
        --image-tar image.tar.gz --out vanchor-app-1.5.0a9-arm64.bundle.tar

Supervisor bundle:
    python scripts/make_bundle.py supervisor \\
        --supervisor-dir supervisor/vanchor_supervisor \\
        --guard supervisor/guard.py \\
        --version 0.1.0 \\
        --out vanchor-supervisor-0.1.0.bundle.tar
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def make_app_bundle(
    image_tar_gz: Path,
    image: str,
    tag: str,
    min_supervisor: str,
    arch: str,
    out: Path,
) -> Path:
    """Create an app bundle tar and return its path.

    The outer tar is uncompressed and contains:
      1. manifest.json  (first member)
      2. image.tar.gz   (the docker-save output, already gzipped)
    """
    image_tar_gz = Path(image_tar_gz)
    out = Path(out)

    image_bytes = image_tar_gz.read_bytes()
    image_sha256 = hashlib.sha256(image_bytes).hexdigest()

    manifest = {
        "format": "vanchor-bundle",
        "schema_version": 1,
        "kind": "app",
        "name": "vanchor",
        "app_version": tag,
        "image": image,
        "tag": tag,
        "arch": arch,
        "min_supervisor": min_supervisor,
        "image_sha256": image_sha256,
        "created_at": _now_utc(),
    }
    manifest_bytes = json.dumps(manifest, indent=2).encode()

    with tarfile.open(str(out), "w") as tf:
        _add_bytes(tf, "manifest.json", manifest_bytes)
        _add_bytes(tf, "image.tar.gz", image_bytes)

    return out


def make_supervisor_bundle(
    supervisor_dir: Path,
    guard_path: Path,
    version: str,
    out: Path,
) -> Path:
    """Create a supervisor bundle tar and return its path.

    The outer tar is uncompressed and contains:
      1. manifest.json   (first member)
      2. payload.tar.gz  (compressed tree: supervisor package + guard.py)
    """
    supervisor_dir = Path(supervisor_dir)
    guard_path = Path(guard_path)
    out = Path(out)

    payload_bytes = _build_payload_gz(supervisor_dir, guard_path)
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()

    manifest = {
        "format": "vanchor-bundle",
        "schema_version": 1,
        "kind": "supervisor",
        "supervisor_version": version,
        "payload_sha256": payload_sha256,
        "created_at": _now_utc(),
    }
    manifest_bytes = json.dumps(manifest, indent=2).encode()

    with tarfile.open(str(out), "w") as tf:
        _add_bytes(tf, "manifest.json", manifest_bytes)
        _add_bytes(tf, "payload.tar.gz", payload_bytes)

    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_bytes(tf: tarfile.TarFile, name: str, data: bytes) -> None:
    """Add an in-memory bytes blob to a TarFile as a regular file."""
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))


def _build_payload_gz(supervisor_dir: Path, guard_path: Path) -> bytes:
    """Build a gzipped tar of the supervisor directory + guard.py in memory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        # Add the supervisor package directory as vanchor_supervisor/
        pkg_name = supervisor_dir.name
        for root, dirs, files in os.walk(str(supervisor_dir)):
            dirs.sort()  # deterministic order
            for fname in sorted(files):
                full = Path(root) / fname
                rel = full.relative_to(supervisor_dir.parent)
                tf.add(str(full), arcname=str(rel))
        # Add guard.py at the top level
        tf.add(str(guard_path), arcname=guard_path.name)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_app(args: argparse.Namespace) -> None:
    out = make_app_bundle(
        image_tar_gz=Path(args.image_tar),
        image=args.image,
        tag=args.tag,
        min_supervisor=args.min_supervisor,
        arch=args.arch,
        out=Path(args.out),
    )
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"Created {out}  ({out.stat().st_size} bytes)")
    print(f"sha256: {sha}")


def _cmd_supervisor(args: argparse.Namespace) -> None:
    out = make_supervisor_bundle(
        supervisor_dir=Path(args.supervisor_dir),
        guard_path=Path(args.guard),
        version=args.version,
        out=Path(args.out),
    )
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"Created {out}  ({out.stat().st_size} bytes)")
    print(f"sha256: {sha}")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Build Vanchor distribution bundles.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # app sub-command
    ap = sub.add_parser("app", help="Build an app bundle from a docker-save tarball.")
    ap.add_argument("--image", required=True, help="Image name (e.g. ghcr.io/alexasplund/vanchor)")
    ap.add_argument("--tag", required=True, help="Image tag (e.g. 1.5.0a9)")
    ap.add_argument("--min-supervisor", required=True, help="Minimum supervisor version required")
    ap.add_argument("--arch", required=True, help="Target architecture (e.g. arm64)")
    ap.add_argument("--image-tar", required=True, help="Path to docker-save .tar.gz file")
    ap.add_argument("--out", required=True, help="Output bundle .tar path")
    ap.set_defaults(func=_cmd_app)

    # supervisor sub-command
    sp = sub.add_parser("supervisor", help="Build a supervisor bundle.")
    sp.add_argument("--supervisor-dir", required=True, help="Path to vanchor_supervisor package directory")
    sp.add_argument("--guard", required=True, help="Path to guard.py")
    sp.add_argument("--version", required=True, help="Supervisor version string")
    sp.add_argument("--out", required=True, help="Output bundle .tar path")
    sp.set_defaults(func=_cmd_supervisor)

    return p


def main() -> None:
    p = _build_parser()
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
