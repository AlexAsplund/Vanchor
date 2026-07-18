"""HTTP API for the vanchor supervisor daemon.

Binds to localhost only (127.0.0.1 by default).  All routes require the
``X-Supervisor-Token`` header.  Body reads capped at 1 MB (large uploads
come through the app's /api/supervisor/upload, not here).

Routes per task-5-brief.md §5.7:

  GET  /v1/status
  GET  /v1/jobs/<id>
  GET  /v1/jobs/last
  POST /v1/update/inspect    {bundle}
  POST /v1/update/apply      {name, source, bundle?/tag?}
  POST /v1/rollback          {name}
  POST /v1/backup            {}
  GET  /v1/backups
  GET  /v1/backups/<id>/download
  POST /v1/restore           {backup_id}
  POST /v1/prune             {}
  POST /v1/self-update       {bundle}
  GET  /v1/devices/check     ?name=<container>

Unknown /v1/* → 404 JSON; /v2/* → 404.
"""
from __future__ import annotations

import http.server
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

_MAX_BODY = 1 * 1024 * 1024  # 1 MB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _supervisor_version() -> str:
    from . import SUPERVISOR_VERSION
    return SUPERVISOR_VERSION


def _api_version() -> int:
    from . import API_VERSION
    return API_VERSION


def _read_token(state_dir: str) -> str:
    p = Path(state_dir) / "token"
    return p.read_text().strip() if p.exists() else ""


def _json(handler: http.server.BaseHTTPRequestHandler, status: int, data) -> None:
    body = json.dumps(data, indent=2).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _bytes(handler: http.server.BaseHTTPRequestHandler, status: int,
           data: bytes, media_type: str = "application/octet-stream",
           filename: str | None = None) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", media_type)
    handler.send_header("Content-Length", str(len(data)))
    if filename:
        handler.send_header("Content-Disposition",
                            f'attachment; filename="{filename}"')
    handler.end_headers()
    handler.wfile.write(data)


def _read_body(handler: http.server.BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    if length > _MAX_BODY:
        raise OverflowError("Request body too large (limit 1 MB)")
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def serve(core, settings) -> http.server.ThreadingHTTPServer:
    """Create and return a ThreadingHTTPServer bound to settings.listen_host:port.

    Does NOT start the server — the caller should invoke serve_forever() in a
    thread.  Binds to port 0 (OS-assigned) when settings.listen_port == 0.
    """
    token = _read_token(settings.state_dir)

    class _Handler(http.server.BaseHTTPRequestHandler):

        def log_message(self, fmt, *args):
            log.debug("API %s — " + fmt, self.address_string(), *args)

        def _auth(self) -> bool:
            return self.headers.get("X-Supervisor-Token", "") == token

        def _deny(self) -> None:
            _json(self, 401, {"error": "unauthorized"})

        def do_GET(self):
            if not self._auth():
                self._deny()
                return
            self._dispatch("GET")

        def do_POST(self):
            if not self._auth():
                self._deny()
                return
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            raw_path = self.path.split("?")[0].rstrip("/")
            qs = self.path.partition("?")[2]
            params: dict[str, str] = {}
            if qs:
                for pair in qs.split("&"):
                    k, _, v = pair.partition("=")
                    params[k] = v

            # ---- exact routes ----------------------------------------
            if method == "GET" and raw_path == "/v1/status":
                self._status()
            elif method == "GET" and raw_path == "/v1/jobs/last":
                self._jobs_last()
            elif method == "GET" and raw_path == "/v1/backups":
                self._list_backups()
            elif method == "POST" and raw_path == "/v1/update/inspect":
                self._update_inspect()
            elif method == "POST" and raw_path == "/v1/update/apply":
                self._update_apply()
            elif method == "POST" and raw_path == "/v1/rollback":
                self._rollback()
            elif method == "POST" and raw_path == "/v1/backup":
                self._backup()
            elif method == "POST" and raw_path == "/v1/restore":
                self._restore()
            elif method == "POST" and raw_path == "/v1/prune":
                self._prune()
            elif method == "POST" and raw_path == "/v1/self-update":
                self._self_update()
            elif method == "GET" and raw_path == "/v1/devices/check":
                self._devices_check(params)
            # ---- parameterised routes --------------------------------
            elif (m := re.fullmatch(r"/v1/jobs/([^/]+)", raw_path)) and method == "GET":
                self._get_job(m.group(1))
            elif (m := re.fullmatch(r"/v1/backups/([^/]+)/download", raw_path)) and method == "GET":
                self._download_backup(m.group(1))
            # ---- unknown /v1 or /v2 ----------------------------------
            elif raw_path.startswith("/v1/") or raw_path.startswith("/v2/"):
                _json(self, 404, {"error": "not_found"})
            else:
                _json(self, 404, {"error": "not_found"})

        # ---- handlers --------------------------------------------------

        def _status(self) -> None:
            from . import disk as _disk
            containers_status = []
            for entry in core.containers():
                try:
                    ps = core.backend.ps(entry["name"])
                    state = ps.get("status", "unknown")
                    health = "healthy" if ps.get("running") else "unhealthy"
                except Exception:
                    state, health = "unknown", "unknown"
                containers_status.append({
                    "name": entry["name"],
                    "image": entry.get("image", ""),
                    "tag": entry.get("tag", ""),
                    "previous_tag": entry.get("previous_tag"),
                    "state": state,
                    "health": health,
                })

            try:
                volume_mp = core.backend.volume_mountpoint(settings.data_volume)
                disk_snap = _disk.snapshot(Path(volume_mp), core.backend, settings)
            except Exception as exc:
                disk_snap = {"error": str(exc), "warn": False, "crit": False}

            warnings = []
            if disk_snap.get("crit"):
                warnings.append("disk_crit")
            elif disk_snap.get("warn"):
                warnings.append("disk_warn")

            backups = core.list_backups()
            backups_summary = {
                "count": len(backups),
                "latest": backups[-1]["created_at"] if backups else None,
            }

            active_job = None
            last_job = core.get_last_job()
            if core.is_busy() and last_job and last_job.get("phase") not in ("done", "failed"):
                active_job = last_job
                last_job = None

            _json(self, 200, {
                "supervisor_version": _supervisor_version(),
                "api_version": _api_version(),
                "containers": containers_status,
                "disk": disk_snap,
                "backups": backups_summary,
                "job": active_job,
                "last_job": last_job,
                "warnings": warnings,
            })

        def _get_job(self, job_id: str) -> None:
            job = core.get_job(job_id)
            if job is None:
                _json(self, 404, {"error": "not_found"})
                return
            _json(self, 200, job)

        def _jobs_last(self) -> None:
            job = core.get_last_job()
            _json(self, 200, job if job is not None else {})

        def _update_inspect(self) -> None:
            try:
                body = _read_body(self)
            except OverflowError:
                _json(self, 413, {"error": "body_too_large"})
                return
            except Exception as exc:
                _json(self, 400, {"error": str(exc)})
                return
            bundle_rel = body.get("bundle")
            if not bundle_rel:
                _json(self, 400, {"error": "bundle required"})
                return
            try:
                volume_mp = core.backend.volume_mountpoint(settings.data_volume)
                bundle_path = (Path(volume_mp) / bundle_rel).resolve()
                mp_resolved = Path(volume_mp).resolve()
                try:
                    bundle_path.relative_to(mp_resolved)
                except ValueError:
                    _json(self, 400, {"error": "invalid bundle path"})
                    return
                from .bundles import read_manifest
                from . import SUPERVISOR_VERSION
                from .versionspec import is_at_least
                manifest = read_manifest(bundle_path)
                min_sup = manifest.get("min_supervisor", "0.0.0")
                compatible = True
                reason = None
                try:
                    if not is_at_least(SUPERVISOR_VERSION, min_sup):
                        compatible = False
                        reason = f"supervisor_too_old: need >= {min_sup}, have {SUPERVISOR_VERSION}"
                except ValueError:
                    compatible = False
                    reason = f"supervisor_too_old: unparseable min_supervisor {min_sup!r}"

                # Find current tag
                entry = core.get_entry(manifest.get("name", "vanchor"))
                current_tag = entry["tag"] if entry else None

                _json(self, 200, {
                    "manifest": manifest,
                    "compatible": compatible,
                    "reason": reason,
                    "current_tag": current_tag,
                })
            except Exception as exc:
                _json(self, 500, {"error": str(exc)})

        def _update_apply(self) -> None:
            try:
                body = _read_body(self)
            except OverflowError:
                _json(self, 413, {"error": "body_too_large"})
                return
            except Exception as exc:
                _json(self, 400, {"error": str(exc)})
                return
            name = body.get("name", "vanchor")
            source = body.get("source", "bundle")
            bundle_rel = body.get("bundle")
            tag = body.get("tag")
            try:
                from .core import BusyError
                job = core.apply_update(name, bundle_rel=bundle_rel, tag=tag)
                _json(self, 200, {"job_id": job["id"]})
            except BusyError:
                _json(self, 409, {"error": "busy"})
            except ValueError as exc:
                _json(self, 400, {"error": str(exc)})
            except Exception as exc:
                log.exception("apply_update error")
                _json(self, 500, {"error": str(exc)})

        def _rollback(self) -> None:
            try:
                body = _read_body(self)
            except OverflowError:
                _json(self, 413, {"error": "body_too_large"})
                return
            except Exception as exc:
                _json(self, 400, {"error": str(exc)})
                return
            name = body.get("name", "vanchor")
            try:
                from .core import BusyError
                job = core.rollback(name)
                _json(self, 200, {"job_id": job["id"]})
            except BusyError:
                _json(self, 409, {"error": "busy"})
            except ValueError as exc:
                _json(self, 400, {"error": str(exc)})
            except Exception as exc:
                log.exception("rollback error")
                _json(self, 500, {"error": str(exc)})

        def _backup(self) -> None:
            try:
                _read_body(self)
            except OverflowError:
                _json(self, 413, {"error": "body_too_large"})
                return
            except Exception:
                pass
            try:
                from .core import BusyError
                job = core.create_backup("vanchor")
                _json(self, 200, {"job_id": job["id"]})
            except BusyError:
                _json(self, 409, {"error": "busy"})
            except Exception as exc:
                _json(self, 500, {"error": str(exc)})

        def _list_backups(self) -> None:
            backups = core.list_backups()
            _json(self, 200, {"backups": backups})

        def _download_backup(self, backup_id: str) -> None:
            state_dir = Path(settings.state_dir)
            backups_dir = state_dir / "backups"
            # Find by name (with or without .tar.gz extension)
            candidates = list(backups_dir.glob(f"{backup_id}*"))
            if not candidates:
                _json(self, 404, {"error": "not_found"})
                return
            tar_path = candidates[0]
            try:
                data = tar_path.read_bytes()
                _bytes(self, 200, data, media_type="application/gzip",
                       filename=tar_path.name)
            except OSError as exc:
                _json(self, 500, {"error": str(exc)})

        def _restore(self) -> None:
            try:
                body = _read_body(self)
            except OverflowError:
                _json(self, 413, {"error": "body_too_large"})
                return
            except Exception as exc:
                _json(self, 400, {"error": str(exc)})
                return
            backup_id = body.get("backup_id")
            if not backup_id:
                _json(self, 400, {"error": "backup_id required"})
                return
            try:
                from .core import BusyError
                job = core.do_restore("vanchor", backup_id)
                _json(self, 200, {"job_id": job["id"]})
            except BusyError:
                _json(self, 409, {"error": "busy"})
            except Exception as exc:
                _json(self, 500, {"error": str(exc)})

        def _prune(self) -> None:
            try:
                _read_body(self)
            except OverflowError:
                _json(self, 413, {"error": "body_too_large"})
                return
            except Exception:
                pass
            try:
                from .core import BusyError
                job = core.prune_job()
                _json(self, 200, {"job_id": job["id"]})
            except BusyError:
                _json(self, 409, {"error": "busy"})
            except AttributeError:
                # Fallback: synchronous prune
                result = core.prune()
                _json(self, 200, {"ok": True, "result": result})
            except Exception as exc:
                _json(self, 500, {"error": str(exc)})

        def _self_update(self) -> None:
            try:
                body = _read_body(self)
            except OverflowError:
                _json(self, 413, {"error": "body_too_large"})
                return
            except Exception as exc:
                _json(self, 400, {"error": str(exc)})
                return
            bundle_rel = body.get("bundle")
            if not bundle_rel:
                _json(self, 400, {"error": "bundle required"})
                return
            try:
                volume_mp = core.backend.volume_mountpoint(settings.data_volume)
                bundle_path = (Path(volume_mp) / bundle_rel).resolve()
                result = core.do_self_update(bundle_path)
                _json(self, 200, result)
            except Exception as exc:
                _json(self, 500, {"error": str(exc)})

        def _devices_check(self, params: dict) -> None:
            name = params.get("name", "vanchor")
            entry = core.get_entry(name)
            if entry is None:
                _json(self, 404, {"error": "not_found"})
                return
            try:
                from . import devicepolicy
                volume_mp = core.backend.volume_mountpoint(settings.data_volume)
                result = devicepolicy.check(entry, Path(volume_mp), core.backend)
                _json(self, 200, result)
            except Exception as exc:
                _json(self, 500, {"error": str(exc)})

    server = http.server.ThreadingHTTPServer(
        (settings.listen_host, settings.listen_port), _Handler
    )
    return server
