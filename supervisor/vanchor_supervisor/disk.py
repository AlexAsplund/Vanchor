"""Disk usage monitoring and image pruning for the supervisor."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import SupervisorSettings

log = logging.getLogger(__name__)


def snapshot(volume_root: Path, backend, settings: "SupervisorSettings") -> dict:
    """Return a disk usage snapshot dict for the API /v1/status response.

    Uses shutil.disk_usage for the data volume filesystem and
    backend.system_df() for docker layer accounting.

    Returns a dict with keys:
      data_total_bytes, data_free_bytes, data_used_pct,
      docker_images_bytes, docker_reclaimable_bytes, warn, crit
    """
    try:
        usage = shutil.disk_usage(str(volume_root))
        total = usage.total
        used = usage.used
        free = usage.free
        pct = (used / total * 100.0) if total > 0 else 0.0
    except OSError as exc:
        log.warning("disk_usage failed for %s: %s", volume_root, exc)
        total = used = free = 0
        pct = 0.0

    warn = pct >= settings.disk_warn_pct
    crit = pct >= settings.disk_crit_pct

    images_bytes = 0
    reclaimable_bytes = 0
    try:
        df = backend.system_df()
        images_bytes = df.get("images_bytes", 0)
        reclaimable_bytes = df.get("reclaimable_bytes", 0)
    except Exception as exc:
        log.warning("system_df failed: %s", exc)

    return {
        "data_total_bytes": total,
        "data_free_bytes": free,
        "data_used_pct": round(pct, 1),
        "docker_images_bytes": images_bytes,
        "docker_reclaimable_bytes": reclaimable_bytes,
        "warn": warn,
        "crit": crit,
    }


def prune(backend, containers: list[dict]) -> dict:
    """Prune old image tags for each container, then prune dangling images.

    For each container entry, keeps only current tag and previous_tag; removes
    all other tags of the same image repository.

    Returns a summary dict with keys:
      removed (list of image refs removed), kept (list of refs kept),
      errors (list of error strings)
    """
    removed = []
    kept = []
    errors = []

    for entry in containers:
        repository = entry.get("image")
        current_tag = entry.get("tag")
        previous_tag = entry.get("previous_tag")
        if not repository or not current_tag:
            continue

        keep_tags = {t for t in (current_tag, previous_tag) if t}

        try:
            all_tags = backend.list_repo_tags(repository)
        except AttributeError:
            # Fallback: try images() method (CliDockerBackend style)
            try:
                imgs = backend.images(repository)
                all_tags = [img.get("tag", "") for img in imgs if img.get("tag")]
            except Exception as exc:
                errors.append(f"list_repo_tags({repository}): {exc}")
                continue
        except Exception as exc:
            errors.append(f"list_repo_tags({repository}): {exc}")
            continue

        for tag in all_tags:
            ref = f"{repository}:{tag}"
            if tag in keep_tags or tag in ("<none>", ""):
                kept.append(ref)
                continue
            try:
                backend.rmi(repository, tag)
                removed.append(ref)
                log.info("Pruned image: %s", ref)
            except Exception as exc:
                errors.append(f"rmi({ref}): {exc}")

    # Prune dangling layers
    try:
        backend.prune_dangling()
    except Exception as exc:
        errors.append(f"prune_dangling: {exc}")

    return {"removed": removed, "kept": kept, "errors": errors}
