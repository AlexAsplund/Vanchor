"""Volume snapshot backup for the supervisor.

Creates/restores/prunes .tar.gz snapshots of the vanchor data volume.
This is the supervisor's backup module (separate from the app's own backup.py).
"""
from __future__ import annotations

import datetime
import logging
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from .config import SupervisorSettings

log = logging.getLogger(__name__)

_STAMP_FMT = "%Y%m%dT%H%M%SZ"
_PREFIX = "vanchor-data-"
_SUFFIX = ".tar.gz"

# Directories excluded from backups by default (volatile / restorable data)
_DEFAULT_EXCLUDE = ("updates", "water_cache", "debug")


def _stamp(created_at: str | None = None) -> str:
    if created_at:
        return created_at
    return datetime.datetime.now(datetime.timezone.utc).strftime(_STAMP_FMT)


def create(
    volume_root: Path,
    out_dir: Path,
    *,
    exclude: tuple[str, ...] = _DEFAULT_EXCLUDE,
    created_at: str | None = None,
) -> Path:
    """Create a .tar.gz snapshot of volume_root into out_dir.

    Files and directories named in *exclude* (top-level names relative to
    volume_root) are omitted from the archive.

    Returns the Path to the created archive.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp(created_at)
    out_path = out_dir / f"{_PREFIX}{stamp}{_SUFFIX}"

    exclude_set = set(exclude)

    def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        name = tarinfo.name
        # arcname="." makes entries look like "./boats.json" or "./updates"
        if name.startswith("./"):
            name = name[2:]
        # Get the first path component (top-level dir/file name)
        parts = Path(name).parts if name else ()
        if parts and parts[0] in exclude_set:
            return None
        return tarinfo

    with tarfile.open(str(out_path), "w:gz") as tf:
        tf.add(str(volume_root), arcname=".", filter=_filter)

    log.info("Backup created: %s (%d bytes)", out_path, out_path.stat().st_size)
    return out_path


def enforce_retention(out_dir: Path, keep: int = 5) -> list[Path]:
    """Delete oldest backups beyond *keep*, return list of deleted paths."""
    backups = sorted(
        out_dir.glob(f"{_PREFIX}*{_SUFFIX}"),
        key=lambda p: p.stat().st_mtime,
    )
    to_delete = backups[: max(0, len(backups) - keep)]
    deleted = []
    for p in to_delete:
        try:
            p.unlink()
            deleted.append(p)
            log.info("Pruned backup: %s", p)
        except OSError as exc:
            log.warning("Failed to prune backup %s: %s", p, exc)
    return deleted


def restore(
    volume_root: Path,
    tar_path: Path,
    backend,
    container_name: str,
    health_url: str,
    settings: "SupervisorSettings | None" = None,
    health_fetch: Optional[Callable[[str], int]] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> None:
    """Restore a backup into volume_root.

    Steps:
    1. Validate tar members for safety (raises ValueError if unsafe).
    2. Stop the container.
    3. Rename existing volume contents to <name>.pre-restore-<stamp>.
    4. Extract tar_path into volume_root.
    5. Start the container.
    6. Health-gate (simple poll — mirrors core.py logic but standalone).

    Args:
        health_fetch: Optional callable(url) -> int status code. If None,
            uses urllib.request.urlopen.
        sleep: Optional callable(seconds). If None, uses time.sleep.

    Raises RuntimeError if the health gate fails after restore.
    """
    import time
    import urllib.request

    if health_fetch is None:
        def health_fetch(url: str) -> int:
            try:
                return urllib.request.urlopen(url, timeout=4).status
            except Exception:
                return 0

    if sleep is None:
        sleep = time.sleep

    # --- Step 1: Safety validation (before any destructive ops) ---
    with tarfile.open(str(tar_path), "r:gz") as tf:
        for member in tf.getmembers():
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(
                    f"unsafe tar member in backup — traversal detected: {name!r}"
                )

    log.info("Stopping container %s for restore", container_name)
    backend.stop(container_name)
    backend.rm(container_name)

    # Move existing contents aside
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime(_STAMP_FMT)
    pre_restore = volume_root.parent / f"{volume_root.name}.pre-restore-{stamp}"
    if volume_root.exists():
        volume_root.rename(pre_restore)
        log.info("Existing volume contents moved to %s", pre_restore)

    volume_root.mkdir(parents=True, exist_ok=True)

    log.info("Extracting %s into %s", tar_path, volume_root)
    with tarfile.open(str(tar_path), "r:gz") as tf:
        tf.extractall(str(volume_root))

    log.info("Restore extraction complete, starting container %s", container_name)
    backend.run({"name": container_name, "image": "", "tag": "", "restart": "unless-stopped"})

    # Health gate — only runs if settings provided
    if settings is None or not health_url:
        log.info("Skipping health gate (no settings or health_url)")
        return

    deadline = time.monotonic() + settings.health_gate_s
    ok_count = 0
    while time.monotonic() < deadline:
        status = health_fetch(health_url)
        if status == 200:
            ok_count += 1
            if ok_count >= settings.health_ok_count:
                log.info("Health gate passed after restore")
                return
        else:
            ok_count = 0
        sleep(settings.health_poll_s)

    raise RuntimeError(
        f"Health gate failed after restore: {health_url} did not return 200 "
        f"({settings.health_ok_count}x) within {settings.health_gate_s}s"
    )
