"""Opt-in "upload last session on WiFi" (roadmap #48).

Package the most recent on-boat session artifacts -- the debug recordings
(``<data_dir>/debug/*``) and the always-on black-box dumps
(``<data_dir>/blackbox/*``) -- into a single zip and POST it to a
user-configured destination, so a real-water incident becomes a replayable
test scenario back on the bench.

PRIVACY / SAFETY (non-negotiable):

* Strictly OPT-IN. Nothing uploads automatically. The opt-in flag and the
  destination URL live in the prefs KV store (``session_upload_enabled`` default
  OFF, ``session_upload_url`` default empty) -- NOT in config -- and an upload
  only ever happens as a deliberate user action (an explicit POST from the UI).
  :meth:`SessionUploader.upload` refuses unless ``opt_in`` is truthy.
* The upload runs OFF the control path. The network POST is synchronous and
  blocking, so callers hand :meth:`upload` to a thread / executor. Nothing here
  touches the event loop, the governor, or the motor deadman.
* ``upload`` never raises into the caller: any packaging / network failure is
  caught and reported as a status dict, so a flaky WiFi link can't take down
  the server.

The default HTTP transport is stdlib ``urllib`` (no new dependency); it is an
injectable ``post_fn`` seam so tests drive a fake endpoint.
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
import urllib.request
import zipfile

logger = logging.getLogger("vanchor.session_upload")

# Source subdirectories of the data dir and the file suffix each uses. A "debug"
# session may be a directory of parts (chunked) or a single legacy file.
_DEBUG_SUFFIX = ".ndjson.gz"
_BLACKBOX_SUFFIX = ".json.gz"

_UPLOAD_TIMEOUT_S = 30.0


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _http_post(url: str, filename: str, data: bytes) -> int:
    """Default transport: POST ``data`` (a zip) to ``url`` and return the HTTP
    status code. Blocking -- callers run it off the event loop. Raises on a
    transport error (the caller turns that into a failure status)."""
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/zip",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Vanchor-Session-Filename": filename,
        },
    )
    with urllib.request.urlopen(req, timeout=_UPLOAD_TIMEOUT_S) as resp:  # noqa: S310
        return int(getattr(resp, "status", 0) or resp.getcode() or 0)


class SessionUploader:
    """Lists, packages, and (opt-in) uploads the boat's session artifacts.

    Read-only with respect to the data dir: it only reads the debug / blackbox
    artifacts and never mutates or deletes them. ``post_fn`` is the transport
    seam (``(url, filename, data) -> status_code``); the default POSTs via
    ``urllib``.
    """

    def __init__(self, data_dir: str, *, post_fn=None, now_fn=time.time) -> None:
        self._data_dir = data_dir
        self._debug_dir = os.path.join(data_dir, "debug")
        self._blackbox_dir = os.path.join(data_dir, "blackbox")
        self._post_fn = post_fn or _http_post
        self._now_fn = now_fn
        self._lock = threading.Lock()
        self._uploading = False
        self._last_status: dict = {"state": "idle"}

    # ------------------------------------------------------------------ #
    # Listing
    # ------------------------------------------------------------------ #
    def list_sessions(self, limit: int = 25) -> list[dict]:
        """Recent sessions across BOTH sources, newest first.

        Each entry: ``{id, kind, name, bytes, mtime, parts}``. ``id`` is
        ``"<kind>:<name>"`` and round-trips through :meth:`_resolve`.
        """
        entries = self._debug_sessions() + self._blackbox_sessions()
        entries.sort(key=lambda e: e["mtime"], reverse=True)
        if limit is not None and limit >= 0:
            entries = entries[:limit]
        return entries

    def latest_session(self) -> dict | None:
        """The single most-recent session (by mtime), or ``None`` if none."""
        entries = self.list_sessions(limit=1)
        return entries[0] if entries else None

    def _debug_sessions(self) -> list[dict]:
        out: list[dict] = []
        try:
            names = os.listdir(self._debug_dir)
        except OSError:
            return out
        for name in names:
            full = os.path.join(self._debug_dir, name)
            if os.path.isdir(full):  # chunked session (a dir of parts)
                parts = [p for p in os.listdir(full) if p.endswith(_DEBUG_SUFFIX)]
                if not parts:
                    continue
                size = sum(_safe_size(os.path.join(full, p)) for p in parts)
                mtime = max(_safe_mtime(os.path.join(full, p)) for p in parts)
                out.append(self._entry("debug", name, size, mtime, len(parts)))
            elif name.endswith(_DEBUG_SUFFIX):  # legacy single-file session
                out.append(
                    self._entry("debug", name, _safe_size(full), _safe_mtime(full), 1)
                )
        return out

    def _blackbox_sessions(self) -> list[dict]:
        out: list[dict] = []
        try:
            names = os.listdir(self._blackbox_dir)
        except OSError:
            return out
        for name in names:
            if not name.endswith(_BLACKBOX_SUFFIX):
                continue
            full = os.path.join(self._blackbox_dir, name)
            out.append(
                self._entry("blackbox", name, _safe_size(full), _safe_mtime(full), 1)
            )
        return out

    @staticmethod
    def _entry(kind: str, name: str, size: int, mtime: float, parts: int) -> dict:
        return {
            "id": f"{kind}:{name}",
            "kind": kind,
            "name": name,
            "bytes": int(size),
            "mtime": round(float(mtime), 3),
            "parts": int(parts),
        }

    # ------------------------------------------------------------------ #
    # Resolve + package
    # ------------------------------------------------------------------ #
    def _resolve(self, session_id: str | None) -> dict | None:
        """Resolve an id (``"<kind>:<name>"``) to a session entry, or the latest
        when ``session_id`` is falsy. Guards against path traversal: only real
        artifacts inside the debug / blackbox dirs resolve."""
        if not session_id:
            return self.latest_session()
        kind, _, name = str(session_id).partition(":")
        if kind not in ("debug", "blackbox") or not name:
            return None
        # basename strips any directory component a malicious id might smuggle.
        safe = os.path.basename(name)
        if safe != name:
            return None
        for entry in self.list_sessions(limit=None):
            if entry["id"] == f"{kind}:{safe}":
                return entry
        return None

    def _session_files(self, entry: dict) -> list[tuple[str, str]]:
        """``(arcname, abspath)`` pairs for every file in a session."""
        if entry["kind"] == "debug":
            base = os.path.join(self._debug_dir, entry["name"])
            if os.path.isdir(base):
                parts = sorted(
                    p for p in os.listdir(base) if p.endswith(_DEBUG_SUFFIX)
                )
                return [(os.path.join(entry["name"], p), os.path.join(base, p))
                        for p in parts]
            return [(entry["name"], base)]  # legacy single file
        base = os.path.join(self._blackbox_dir, entry["name"])
        return [(entry["name"], base)]

    def package(self, entry: dict) -> tuple[str, bytes]:
        """Zip a resolved session (+ a manifest) into memory. Returns
        ``(filename, zip_bytes)``."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._now_fn()))
        stem = entry["name"]
        if stem.endswith(_DEBUG_SUFFIX):
            stem = stem[: -len(_DEBUG_SUFFIX)]
        elif stem.endswith(_BLACKBOX_SUFFIX):
            stem = stem[: -len(_BLACKBOX_SUFFIX)]
        filename = f"session-{entry['kind']}-{stem}-{stamp}.zip"

        files = self._session_files(entry)
        manifest = {
            "id": entry["id"],
            "kind": entry["kind"],
            "name": entry["name"],
            "parts": entry["parts"],
            "bytes": entry["bytes"],
            "mtime": entry["mtime"],
            "packaged_at": round(self._now_fn(), 3),
            "files": [arc for arc, _ in files],
        }
        # A fixed archive timestamp (zip can't represent pre-1980 dates, and the
        # artifacts are already gzip'd; their own headers carry the real time).
        zt = (2000, 1, 1, 0, 0, 0)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr(zipfile.ZipInfo("manifest.json", zt),
                        json.dumps(manifest, indent=2))
            for arcname, abspath in files:
                try:
                    with open(abspath, "rb") as fh:
                        payload = fh.read()
                except OSError:
                    logger.warning("session file vanished during packaging: %s",
                                   abspath)
                    continue
                info = zipfile.ZipInfo(os.path.join("session", arcname), zt)
                zf.writestr(info, payload)
        return filename, buf.getvalue()

    # ------------------------------------------------------------------ #
    # Upload (deliberate, opt-in)
    # ------------------------------------------------------------------ #
    def upload(self, dest_url: str, *, opt_in: bool,
               session_id: str | None = None) -> dict:
        """Package the chosen (or latest) session and POST it to ``dest_url``.

        Blocking -- run it OFF the event loop. Never raises; returns a status
        dict that is also stored as :meth:`status`.

        Refuses (without touching the network) when ``opt_in`` is false, when no
        destination is configured, or when there is no session to upload.
        """
        if not opt_in:
            return self._record({"ok": False, "state": "error",
                                 "error": "opt-in disabled"})
        dest = str(dest_url or "").strip()
        if not dest:
            return self._record({"ok": False, "state": "error",
                                 "error": "no destination configured"})

        with self._lock:
            if self._uploading:
                return dict(self._last_status, ok=False, error="upload in progress")
            self._uploading = True
        try:
            entry = self._resolve(session_id)
            if entry is None:
                return self._record({"ok": False, "state": "error",
                                     "error": "no sessions to upload"})
            filename, data = self.package(entry)
            started = self._now_fn()
            try:
                code = int(self._post_fn(dest, filename, data))
            except Exception as exc:  # noqa: BLE001 - a bad link must not raise
                logger.warning("session upload failed: %s", exc)
                return self._record({
                    "ok": False, "state": "error", "session": entry["id"],
                    "filename": filename, "bytes": len(data),
                    "error": f"{type(exc).__name__}: {exc}",
                })
            ok = 200 <= code < 300
            return self._record({
                "ok": ok,
                "state": "done" if ok else "error",
                "session": entry["id"],
                "filename": filename,
                "bytes": len(data),
                "http_status": code,
                "uploaded_at": round(self._now_fn(), 3),
                "duration_s": round(self._now_fn() - started, 3),
                **({} if ok else {"error": f"HTTP {code}"}),
            })
        finally:
            with self._lock:
                self._uploading = False

    def status(self) -> dict:
        """A copy of the last upload attempt's status (or ``{"state":"idle"}``)."""
        with self._lock:
            return dict(self._last_status)

    def _record(self, status: dict) -> dict:
        with self._lock:
            self._last_status = dict(status)
            return dict(self._last_status)
