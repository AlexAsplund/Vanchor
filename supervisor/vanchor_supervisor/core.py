"""SupervisorCore: container lifecycle, job state machine, update/rollback.

All long-running operations execute on a single worker thread.  The API
returns a job_id immediately; callers poll GET /v1/jobs/<id> for status.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional

from . import backup as _backup
from . import disk as _disk
from . import selfupdate as _selfupdate
from . import SUPERVISOR_VERSION
from .bundles import read_manifest, verify_payload
from .config import SupervisorSettings
from .versionspec import is_at_least

log = logging.getLogger(__name__)

_MAX_JOBS = 20

# ---------------------------------------------------------------------------
# Default containers.json entry #0
# ---------------------------------------------------------------------------

_DEFAULT_CONTAINERS = [
    {
        "name": "vanchor",
        "image": "ghcr.io/alexasplund/vanchor",
        "tag": "1.5.0a8",
        "previous_tag": None,
        "network": "host",
        "env": {"VANCHOR_HOST": "0.0.0.0", "VANCHOR_DATA_DIR": "/data"},
        "volumes": [
            {"volume": "vanchor_data", "target": "/data"},
            {"host": "/dev", "target": "/dev", "ro": True},
        ],
        "device_cgroup_rules": [
            "c 166:* rmw",
            "c 188:* rmw",
            "c 204:* rmw",
            "c 89:* rmw",
        ],
        "devices": ["/dev/gpiochip0"],
        "restart": "unless-stopped",
        # Bounded container logs (SD-card wear): local driver, 2 x 5 MB.
        # Per-entry tunable; backends.run() applies the same bounds when the
        # field is absent so add-on entries get safe defaults too.
        "logging": {"driver": "local", "options": {"max-size": "5m", "max-file": "2"}},
        "health_url": "http://127.0.0.1:8000/api/state",
        "update_policy": {"channel": "release"},
        "required_devices_from": "devices.json",
    }
]


# ---------------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_job(kind: str, name: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "name": name,
        "phase": "queued",
        "ok": None,
        "error": None,
        "rolled_back": False,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# SupervisorCore
# ---------------------------------------------------------------------------


class SupervisorCore:
    """Main supervisor state object.  Thread-safe for single worker thread."""

    def __init__(
        self,
        settings: SupervisorSettings,
        backend,
        health_fetch: Optional[Callable[[str], int]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.backend = backend
        self.clock = clock
        self.sleep = sleep

        # health_fetch(url) -> HTTP status code, or 0 on connection error
        if health_fetch is None:
            self._health_fetch = _default_health_fetch
        else:
            self._health_fetch = health_fetch

        self._lock = threading.Lock()
        self._busy = False
        self._jobs: dict[str, dict] = {}  # id -> job dict

        self._state_dir = Path(settings.state_dir)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        (self._state_dir / "jobs").mkdir(exist_ok=True)

        self._containers: list[dict] = self._load_containers()
        self._load_recent_jobs()

    # ------------------------------------------------------------------
    # Containers
    # ------------------------------------------------------------------

    def _containers_path(self) -> Path:
        return self._state_dir / "containers.json"

    def _load_containers(self) -> list[dict]:
        p = self._containers_path()
        if not p.exists():
            log.info("containers.json not found — bootstrapping defaults")
            data = list(_DEFAULT_CONTAINERS)
            p.write_text(json.dumps(data, indent=2))
            return data
        try:
            return json.loads(p.read_text())
        except Exception as exc:
            log.error("Failed to load containers.json: %s — using defaults", exc)
            return list(_DEFAULT_CONTAINERS)

    def _save_containers(self) -> None:
        p = self._containers_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._containers, indent=2))
        os.replace(str(tmp), str(p))

    def get_entry(self, name: str) -> dict | None:
        for entry in self._containers:
            if entry["name"] == name:
                return entry
        return None

    def containers(self) -> list[dict]:
        return list(self._containers)

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def _jobs_dir(self) -> Path:
        return self._state_dir / "jobs"

    def _load_recent_jobs(self) -> None:
        jobs_dir = self._jobs_dir()
        jobs = []
        for p in jobs_dir.glob("*.json"):
            if p.name == "last.json":
                continue
            try:
                jobs.append((p.stat().st_mtime, json.loads(p.read_text())))
            except Exception:
                pass
        jobs.sort(key=lambda x: x[0])
        for _, job in jobs[-_MAX_JOBS:]:
            self._jobs[job["id"]] = job

    def _persist_job(self, job: dict) -> None:
        job["updated_at"] = _now_iso()
        jobs_dir = self._jobs_dir()
        p = jobs_dir / f"{job['id']}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(job, indent=2))
        os.replace(str(tmp), str(p))
        # Update last.json
        last_path = jobs_dir / "last.json"
        tmp2 = jobs_dir / "last.tmp"
        tmp2.write_text(json.dumps(job, indent=2))
        os.replace(str(tmp2), str(last_path))
        # Prune oldest beyond MAX_JOBS
        self._trim_jobs()

    def _trim_jobs(self) -> None:
        jobs_dir = self._jobs_dir()
        files = [p for p in jobs_dir.glob("*.json") if p.name != "last.json"]
        if len(files) <= _MAX_JOBS:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files[: len(files) - _MAX_JOBS]:
            job_id = p.stem
            self._jobs.pop(job_id, None)
            try:
                p.unlink()
            except OSError:
                pass

    def get_job(self, job_id: str) -> dict | None:
        return self._jobs.get(job_id)

    def get_last_job(self) -> dict | None:
        """Return the most recently updated job, or None."""
        last_path = self._jobs_dir() / "last.json"
        if last_path.exists():
            try:
                return json.loads(last_path.read_text())
            except Exception:
                pass
        if not self._jobs:
            return None
        return max(self._jobs.values(), key=lambda j: j.get("updated_at", ""))

    def list_jobs(self) -> list[dict]:
        return sorted(self._jobs.values(), key=lambda j: j.get("created_at", ""))

    def _acquire_worker(self) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            return True

    def _release_worker(self) -> None:
        with self._lock:
            self._busy = False

    def is_busy(self) -> bool:
        with self._lock:
            return self._busy

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply_update(self, name: str, *, bundle_rel: str | None = None, tag: str | None = None) -> dict:
        """Schedule an update job.  Returns the job dict (409 if busy)."""
        if not self._acquire_worker():
            raise BusyError("A job is already running")
        entry = self.get_entry(name)
        if entry is None:
            self._release_worker()
            raise ValueError(f"Unknown container: {name!r}")

        job = _new_job("update", name)
        job["bundle_rel"] = bundle_rel
        job["new_tag"] = tag
        self._jobs[job["id"]] = job
        self._persist_job(job)

        t = threading.Thread(
            target=self._run_update,
            args=(job, entry, bundle_rel, tag),
            daemon=True,
            name=f"update-{job['id'][:8]}",
        )
        t.start()
        return job

    def rollback(self, name: str) -> dict:
        """Schedule a rollback job.

        Returns the job dict.  Fails the job with error="no_previous" rather than
        raising when previous_tag is absent so the caller (and UI) gets a normal
        job-failure response.
        """
        if not self._acquire_worker():
            raise BusyError("A job is already running")
        entry = self.get_entry(name)
        if entry is None:
            self._release_worker()
            raise ValueError(f"Unknown container: {name!r}")

        job = _new_job("rollback", name)
        self._jobs[job["id"]] = job
        self._persist_job(job)

        if not entry.get("previous_tag"):
            self._fail_job(job, "no_previous")
            return job

        t = threading.Thread(
            target=self._run_rollback,
            args=(job, entry),
            daemon=True,
            name=f"rollback-{job['id'][:8]}",
        )
        t.start()
        return job

    def create_backup(self, name: str) -> dict:
        """Schedule a backup job."""
        if not self._acquire_worker():
            raise BusyError("A job is already running")
        entry = self.get_entry(name)
        if entry is None:
            self._release_worker()
            raise ValueError(f"Unknown container: {name!r}")

        job = _new_job("backup", name)
        self._jobs[job["id"]] = job
        self._persist_job(job)

        t = threading.Thread(
            target=self._run_backup,
            args=(job, entry),
            daemon=True,
            name=f"backup-{job['id'][:8]}",
        )
        t.start()
        return job

    def prune(self) -> dict:
        """Prune old images and return a summary dict."""
        result = _disk.prune(self.backend, self._containers)
        log.info("Prune: %s", result)
        return result

    def do_self_update(self, bundle_path: Path) -> dict:
        """Install a new supervisor version from a bundle.

        Installs synchronously, persists the job as ``done`` with
        ``detail="restarting supervisor"``, then schedules ``os._exit(0)``
        in a 1-second daemon thread so the HTTP response is delivered to
        the client before systemd ``Restart=always`` brings up the new code.

        Returns the job dict; the API handler extracts ``job["id"]``.
        """
        install_root = Path(self.settings.install_root)

        job = _new_job("self_update", "supervisor")
        self._jobs[job["id"]] = job
        self._persist_job(job)

        try:
            self._set_phase(job, "verify")
            new_version = _selfupdate.install(bundle_path, install_root)
            log.info("Self-update complete: supervisor %s installed", new_version)

            job["detail"] = "restarting supervisor"
            job["ok"] = True
            job["phase"] = "done"
            job["finished_at"] = _now_iso()
            # Persist BEFORE exiting so /v1/jobs/last is visible after restart.
            self._persist_job(job)
            log.info("[job %s] Self-update persisted; scheduling os._exit(0) in 1 s",
                     job["id"][:8])

            # Spawn a 1-second delay daemon thread so the current HTTP
            # response is delivered to the client first.  systemd
            # Restart=always will boot the newly installed version.
            def _delayed_exit() -> None:
                self.sleep(1)
                log.info("Supervisor self-update: calling os._exit(0)")
                os._exit(0)  # noqa: SLF001

            t = threading.Thread(
                target=_delayed_exit,
                daemon=True,
                name="self-update-exit",
            )
            t.start()

        except Exception as exc:
            job["ok"] = False
            job["error"] = str(exc)
            job["phase"] = "failed"
            job["finished_at"] = _now_iso()
            self._persist_job(job)
            log.error("[job %s] self-update FAILED: %s", job["id"][:8], exc)
            raise

        return job

    def do_restore(self, name: str, backup_id: str) -> dict:
        """Schedule a volume restore job."""
        if not self._acquire_worker():
            raise BusyError("A job is already running")
        entry = self.get_entry(name)
        if entry is None:
            self._release_worker()
            raise ValueError(f"Unknown container: {name!r}")

        job = _new_job("restore", name)
        job["backup_id"] = backup_id
        self._jobs[job["id"]] = job
        self._persist_job(job)

        t = threading.Thread(
            target=self._run_restore,
            args=(job, entry, backup_id),
            daemon=True,
            name=f"restore-{job['id'][:8]}",
        )
        t.start()
        return job

    # ------------------------------------------------------------------
    # Worker implementations
    # ------------------------------------------------------------------

    def _set_phase(self, job: dict, phase: str) -> None:
        job["phase"] = phase
        self._persist_job(job)
        log.info("[job %s] phase → %s", job["id"][:8], phase)

    def _fail_job(self, job: dict, error: str) -> None:
        job["ok"] = False
        job["error"] = error
        job["phase"] = "failed"
        self._persist_job(job)
        log.error("[job %s] FAILED: %s", job["id"][:8], error)
        self._release_worker()

    def _complete_job(self, job: dict) -> None:
        job["ok"] = True
        job["phase"] = "done"
        self._persist_job(job)
        log.info("[job %s] done", job["id"][:8])
        self._release_worker()

    def _run_update(
        self,
        job: dict,
        entry: dict,
        bundle_rel: str | None,
        new_tag: str | None,
    ) -> None:
        name = entry["name"]
        old_tag = entry["tag"]
        image = entry["image"]

        try:
            # Phase: verify
            self._set_phase(job, "verify")
            if bundle_rel is not None:
                # Path-traversal containment check
                volume_mp = self.backend.volume_mountpoint(self.settings.data_volume)
                bundle_path = (Path(volume_mp) / bundle_rel).resolve()
                mp_resolved = Path(volume_mp).resolve()
                try:
                    bundle_path.relative_to(mp_resolved)
                except ValueError:
                    raise ValueError(
                        "invalid bundle path: traversal outside volume mountpoint"
                    )
                if not bundle_path.exists():
                    raise FileNotFoundError(f"Bundle not found: {bundle_path}")

                manifest = read_manifest(bundle_path)
                if manifest.get("kind") != "app":
                    raise ValueError(f"Expected app bundle, got kind={manifest.get('kind')!r}")

                # min_supervisor compatibility check
                min_sup = manifest.get("min_supervisor", "0.0.0")
                try:
                    if not is_at_least(SUPERVISOR_VERSION, min_sup):
                        raise ValueError(
                            f"supervisor_too_old: need >= {min_sup}, have {SUPERVISOR_VERSION}"
                        )
                except ValueError as ve:
                    if str(ve).startswith("supervisor_too_old"):
                        raise
                    # Unparseable min_supervisor is treated as incompatible
                    raise ValueError(
                        f"supervisor_too_old: unparseable min_supervisor {min_sup!r}"
                    )

                bundle_tag = manifest.get("tag")
                if bundle_tag:
                    new_tag = bundle_tag
            else:
                bundle_path = None

            if not new_tag:
                raise ValueError("No tag supplied and no tag in bundle manifest")

            # Phase: backup
            self._set_phase(job, "backup")
            self._do_backup_for_update(entry)

            # Phase: load_or_pull
            self._set_phase(job, "load_or_pull")
            if bundle_path is not None:
                payload_path = verify_payload(bundle_path, manifest)
                self.backend.load(str(payload_path))
            else:
                self.backend.pull(image, new_tag)

            # Phase: recreate
            self._set_phase(job, "recreate")
            self.backend.stop(name)
            self.backend.rm(name)

            # Update entry with new tag
            new_entry = dict(entry)
            new_entry["previous_tag"] = old_tag
            new_entry["tag"] = new_tag

            # Update in-memory and persist before starting
            self._update_entry(name, new_entry)
            self.backend.run(new_entry)

            # Phase: health_gate
            self._set_phase(job, "health_gate")
            health_url = entry.get("health_url", "")
            if health_url:
                gate_ok = self._health_gate(health_url)
            else:
                gate_ok = True

            if gate_ok:
                job["to_tag"] = new_tag
                job["from_tag"] = old_tag
                self._complete_job(job)
                # Phase done: delete the consumed bundle
                if bundle_path is not None:
                    try:
                        bundle_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                # Prune old images
                try:
                    _disk.prune(self.backend, self._containers)
                except Exception as exc:
                    log.warning("Post-update prune failed: %s", exc)
                return

            # Gate failed — rollback
            log.warning("[job %s] health gate failed — rolling back to %s", job["id"][:8], old_tag)
            self._set_phase(job, "rollback")
            self.backend.stop(name)
            self.backend.rm(name)
            rollback_entry = dict(new_entry)
            rollback_entry["tag"] = old_tag
            rollback_entry["previous_tag"] = None
            self._update_entry(name, rollback_entry)
            self.backend.run(rollback_entry)

            # Single rollback health gate
            if health_url:
                rb_ok = self._health_gate(health_url)
            else:
                rb_ok = True

            job["rolled_back"] = True
            if rb_ok:
                job["ok"] = False
                job["error"] = "health_gate_failed_rolled_back"
                job["phase"] = "done"
                self._persist_job(job)
            else:
                job["ok"] = False
                job["error"] = "rollback_unhealthy"
                job["phase"] = "failed"
                self._persist_job(job)

            log.error("[job %s] rolled back; rollback_ok=%s", job["id"][:8], rb_ok)

        except Exception as exc:
            self._fail_job(job, str(exc))
            log.exception("[job %s] update raised", job["id"][:8])
            return

        self._release_worker()

    def _run_rollback(self, job: dict, entry: dict) -> None:
        name = entry["name"]
        prev_tag = entry.get("previous_tag")
        current_tag = entry["tag"]
        health_url = entry.get("health_url", "")

        try:
            self._set_phase(job, "recreate")
            self.backend.stop(name)
            self.backend.rm(name)

            rb_entry = dict(entry)
            rb_entry["tag"] = prev_tag
            rb_entry["previous_tag"] = current_tag
            self._update_entry(name, rb_entry)
            self.backend.run(rb_entry)

            self._set_phase(job, "health_gate")
            gate_ok = self._health_gate(health_url) if health_url else True

            if gate_ok:
                job["rolled_back"] = True
                self._complete_job(job)
            else:
                job["rolled_back"] = True
                self._fail_job(job, "rollback_unhealthy")

        except Exception as exc:
            self._fail_job(job, str(exc))
            log.exception("[job %s] rollback raised", job["id"][:8])

    def _run_backup(self, job: dict, entry: dict) -> None:
        try:
            self._set_phase(job, "running")
            volume_mp = self.backend.volume_mountpoint(self.settings.data_volume)
            volume_root = Path(volume_mp)
            out_dir = self._state_dir / "backups"
            out_dir.mkdir(parents=True, exist_ok=True)

            out_path = _backup.create(volume_root, out_dir)
            _backup.enforce_retention(out_dir, keep=self.settings.backup_retention)

            job["backup_path"] = str(out_path)
            self._complete_job(job)

        except Exception as exc:
            self._fail_job(job, str(exc))
            log.exception("[job %s] backup raised", job["id"][:8])

    def _run_restore(self, job: dict, entry: dict, backup_id: str) -> None:
        try:
            self._set_phase(job, "running")
            out_dir = self._state_dir / "backups"
            # Find the backup file
            backup_path = self._find_backup(out_dir, backup_id)
            if backup_path is None:
                raise ValueError(f"Backup not found: {backup_id!r}")

            volume_mp = self.backend.volume_mountpoint(self.settings.data_volume)
            volume_root = Path(volume_mp)

            _backup.restore(
                volume_root=volume_root,
                tar_path=backup_path,
                backend=self.backend,
                container_name=entry["name"],
                health_url=entry.get("health_url", ""),
                settings=self.settings,
            )
            self._complete_job(job)

        except Exception as exc:
            self._fail_job(job, str(exc))
            log.exception("[job %s] restore raised", job["id"][:8])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _health_gate(self, url: str) -> bool:
        """Poll url until health_ok_count consecutive 200s or timeout.

        Returns True on success, False on timeout.
        """
        deadline = self.clock() + self.settings.health_gate_s
        ok_count = 0
        while self.clock() < deadline:
            status = self._health_fetch(url)
            if status == 200:
                ok_count += 1
                if ok_count >= self.settings.health_ok_count:
                    return True
            else:
                ok_count = 0
            self.sleep(self.settings.health_poll_s)
        return False

    def _update_entry(self, name: str, new_entry: dict) -> None:
        for i, e in enumerate(self._containers):
            if e["name"] == name:
                self._containers[i] = new_entry
                self._save_containers()
                return
        log.warning("_update_entry: container %r not found", name)

    def _do_backup_for_update(self, entry: dict) -> None:
        """Create a backup before applying an update."""
        try:
            volume_mp = self.backend.volume_mountpoint(self.settings.data_volume)
            volume_root = Path(volume_mp)
            out_dir = self._state_dir / "backups"
            out_dir.mkdir(parents=True, exist_ok=True)
            _backup.create(volume_root, out_dir)
            _backup.enforce_retention(out_dir, keep=self.settings.backup_retention)
        except Exception as exc:
            log.warning("Pre-update backup failed (continuing): %s", exc)

    def _find_backup(self, out_dir: Path, backup_id: str) -> Path | None:
        """Find a backup by id (filename stem or full filename)."""
        # Try exact filename match
        exact = out_dir / backup_id
        if exact.exists():
            return exact
        # Try matching by stem
        for p in out_dir.glob("*.tar.gz"):
            if p.stem == backup_id or p.name == backup_id:
                return p
        return None

    def list_backups(self) -> list[dict]:
        """Return a list of available backups as dicts."""
        out_dir = self._state_dir / "backups"
        if not out_dir.exists():
            return []
        result = []
        for p in sorted(out_dir.glob("vanchor-data-*.tar.gz")):
            result.append({
                "id": p.name,
                "path": str(p),
                "size_bytes": p.stat().st_size,
                "created_at": datetime.datetime.fromtimestamp(
                    p.stat().st_mtime, tz=datetime.timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
        return result


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class BusyError(Exception):
    """Raised when a job is requested but the worker is already busy."""


# ---------------------------------------------------------------------------
# Default health fetcher
# ---------------------------------------------------------------------------


def _default_health_fetch(url: str) -> int:
    """Return HTTP status code from url, or 0 on any connection error."""
    try:
        resp = urllib.request.urlopen(url, timeout=4)
        return resp.status
    except Exception:
        return 0
