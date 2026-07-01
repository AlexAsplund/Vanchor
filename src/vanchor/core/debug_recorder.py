"""Debug session recording + replay.

Records EVERYTHING that happens during a session -- telemetry snapshots, raw
NMEA, commands, and **application log lines** -- to gzipped NDJSON on the server,
so a run can be downloaded and replayed frame-for-frame later.

Crash safety: a session is written as a series of gzip **chunks** (parts) in a
per-session directory (``debug/<name>/0001.ndjson.gz``, ``0002...``). A part is
rotated after ``CHUNK_SECONDS`` or ``CHUNK_BYTES`` and closed with a valid gzip
trailer (+ fsync), so every *completed* part survives a crash intact; the open
part is flushed to the OS at least every ``FLUSH_INTERVAL`` seconds, so an
application crash loses at most a couple of seconds of the current part (a
power-loss may lose the un-fsynced tail of the open part only). Replay/download
transparently concatenate the parts (gzip members concatenate into one stream).

Each NDJSON line is ``{"t": <unix seconds>, "kind": "telemetry|nmea|command|log|
meta", "data": ...}``. Log lines are captured by attaching a logging handler to
the root logger for the duration of the recording. ``write`` is guarded by a lock
because logs can arrive from worker threads (e.g. blocking serial reads).
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import threading

logger = logging.getLogger("vanchor.debug")

CHUNK_SECONDS = 300.0             # rotate a part after 5 minutes ...
CHUNK_BYTES = 1 * 1024 * 1024     # ... or 1 MB (compressed), whichever comes first
FLUSH_INTERVAL = 2.0             # flush the open part to disk at least this often
_SUFFIX = ".ndjson.gz"


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _part_paths(path: str) -> list[str]:
    """Ordered part files for a session ``path`` (a dir of parts, or a single
    legacy ``*.ndjson.gz`` file)."""
    if os.path.isdir(path):
        return [os.path.join(path, p) for p in sorted(os.listdir(path))
                if p.endswith(_SUFFIX)]
    return [path]


class _DebugLogHandler(logging.Handler):
    """Forwards every log record into the active recording as a ``log`` line."""

    def __init__(self, recorder: "DebugRecorder") -> None:
        super().__init__()
        self._rec = recorder

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._rec.write("log", {
                "level": record.levelname,
                "name": record.name,
                "msg": record.getMessage(),
            }, record.created)
        except Exception:  # pragma: no cover - logging must never raise
            pass


class DebugRecorder:
    def __init__(self, data_dir: str) -> None:
        self.dir = os.path.join(data_dir, "debug")
        self.active = False
        self.path: str | None = None   # the session DIRECTORY
        self.name: str | None = None
        self.counts: dict[str, int] = {}
        self._fh = None
        self._lock = threading.RLock()
        self._part = 0
        self._part_start = 0.0
        self._last_flush = 0.0
        self._log_handler: _DebugLogHandler | None = None

    # ---- lifecycle ------------------------------------------------------ #
    def start(self, name: str, now: float) -> dict:
        with self._lock:
            if self.active:
                return self.status()
            # Sanitize the caller-supplied name to prevent path traversal: a
            # payload like "../../evil" must not escape the recorder's base dir.
            # os.path.basename strips any directory component, and stripping
            # leading "./\\" removes residual single-component traversal tokens
            # like "." or "..".  Fall back to "session" when nothing is left.
            safe_name = os.path.basename(str(name).strip()).strip("./\\ ")
            if not safe_name:
                safe_name = "session"
            self.name = safe_name
            self.path = os.path.join(self.dir, safe_name)  # a directory of parts
            os.makedirs(self.path, exist_ok=True)
            self.counts = {}
            self._part = 0
            self.active = True
            self._open_part(now)
            # Log the lifecycle line BEFORE attaching the capture handler, so our
            # own "started" message isn't recorded into the session (its wall-clock
            # timestamp would also perturb chunk rotation, which is keyed on write
            # times). Ambient app logs after this ARE captured (INFO+ per the
            # running level), attached to root so every component is included.
            logger.info("debug recording started: %s", self.path)
            self._log_handler = _DebugLogHandler(self)
            logging.getLogger().addHandler(self._log_handler)
        return self.status()

    def _open_part(self, now: float) -> None:
        """Open the next chunk. Caller holds the lock."""
        self._part += 1
        part_path = os.path.join(self.path, f"{self._part:04d}{_SUFFIX}")
        self._fh = gzip.open(part_path, "wt", encoding="utf-8")
        self._part_start = now
        self._last_flush = now
        self._fh.write(json.dumps({"t": now, "kind": "meta", "data": {
            "name": self.name, "part": self._part, "started": now}}) + "\n")

    def _close_part(self) -> None:
        """Flush, fsync and close the current chunk. Caller holds the lock."""
        if self._fh is None:
            return
        try:
            self._fh.flush()
            try:
                os.fsync(self._fh.buffer.fileno())  # durable on close (power loss)
            except (OSError, AttributeError):  # pragma: no cover - platform-dependent
                pass
            self._fh.close()
        finally:
            self._fh = None

    def _current_part_size(self) -> int:
        return _safe_size(os.path.join(self.path, f"{self._part:04d}{_SUFFIX}"))

    def write(self, kind: str, data, now: float) -> None:
        with self._lock:
            if not self.active or self._fh is None:
                return
            try:
                self._fh.write(json.dumps({"t": now, "kind": kind, "data": data}) + "\n")
                self.counts[kind] = self.counts.get(kind, 0) + 1
            except (OSError, TypeError, ValueError):  # pragma: no cover - defensive
                return
            if now - self._last_flush >= FLUSH_INTERVAL:
                try:
                    self._fh.flush()
                except OSError:  # pragma: no cover - defensive
                    pass
                self._last_flush = now
            if (now - self._part_start >= CHUNK_SECONDS
                    or self._current_part_size() >= CHUNK_BYTES):
                self._close_part()
                self._open_part(now)

    def stop(self) -> dict:
        with self._lock:
            if not self.active:
                return {"recording": False}
            self.active = False
            if self._log_handler is not None:
                logging.getLogger().removeHandler(self._log_handler)
                self._log_handler = None
            self._close_part()
            counts = dict(self.counts)
            name = self.name
        logger.info("debug recording stopped: %s (%s)", self.path, counts)
        return {"recording": False, "name": name, "counts": counts}

    def status(self) -> dict:
        return {
            "recording": self.active,
            "name": self.name if self.active else None,
            "parts": self._part if self.active else 0,
            "counts": dict(self.counts) if self.active else {},
        }

    def sessions(self) -> list[dict]:
        if not os.path.isdir(self.dir):
            return []
        out = []
        for f in sorted(os.listdir(self.dir), reverse=True):
            full = os.path.join(self.dir, f)
            if os.path.isdir(full):  # a chunked session
                parts = [p for p in os.listdir(full) if p.endswith(_SUFFIX)]
                if not parts:
                    continue
                size = sum(_safe_size(os.path.join(full, p)) for p in parts)
                out.append({"name": f, "file": f, "bytes": size, "parts": len(parts)})
            elif f.endswith(_SUFFIX):  # a legacy single-file session
                out.append({"name": f[: -len(_SUFFIX)], "file": f,
                            "bytes": _safe_size(full), "parts": 1})
        return out

    def path_for(self, file_name: str) -> str | None:
        """Resolve a session (dir) or legacy file for download/replay, guarding
        against path traversal."""
        safe = os.path.basename(file_name)
        p = os.path.join(self.dir, safe)
        if os.path.isdir(p):
            return p
        if os.path.isfile(p) and p.endswith(_SUFFIX):
            return p
        return None


class ReplayPlayer:
    """Plays back recorded telemetry frames at their original cadence."""

    def __init__(self) -> None:
        self.active = False
        self.name: str | None = None
        self._frames: list[tuple[float, dict]] = []
        self._t0 = 0.0
        self._wall0 = 0.0
        self._idx = 0

    def load(self, path: str, now: float) -> bool:
        frames: list[tuple[float, dict]] = []
        for part in _part_paths(path):
            try:
                with gzip.open(part, "rt", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if rec.get("kind") == "telemetry" and isinstance(rec.get("data"), dict):
                            frames.append((float(rec.get("t", 0.0)), rec["data"]))
            except (OSError, EOFError):
                # A crash-truncated final part: keep everything recovered so far
                # (completed parts + this part's readable prefix) and stop.
                break
        if not frames:
            return False
        self._frames = frames
        self._t0 = frames[0][0]
        self._wall0 = now
        self._idx = 0
        self.active = True
        self.name = os.path.basename(path)
        return True

    def stop(self) -> None:
        self.active = False
        self._frames = []

    def current(self, now: float) -> dict | None:
        if not self.active or not self._frames:
            return None
        elapsed = now - self._wall0
        while self._idx + 1 < len(self._frames) and (self._frames[self._idx + 1][0] - self._t0) <= elapsed:
            self._idx += 1
        frame = dict(self._frames[self._idx][1])
        n = len(self._frames)
        frame["replay"] = {
            "active": True, "name": self.name,
            "index": self._idx + 1, "total": n, "progress": round((self._idx + 1) / n, 3),
        }
        # Auto-stop a second after the last frame.
        if self._idx >= n - 1 and elapsed > (self._frames[-1][0] - self._t0) + 1.0:
            self.active = False
        return frame
