"""Persistent alert log for the server side of the alert history panel.

Mirrors the client-side localStorage log in ``static/alerts.js``. The server
copy survives a page reload / new client so the operator sees the full history
even after a browser refresh or a second device connects.

Design goals:
- Thread-safe (guarded by a single lock; never called from the hot path).
- Corrupt-file tolerant (a bad JSON file is treated as empty on load).
- Bounded (max_entries cap; oldest entries are dropped when full).
- Debounced writes (one write per ``_DEBOUNCE_S`` seconds at most, plus a
  flush on ``clear()``).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger("vanchor.alertlog")

_DEBOUNCE_S: float = 5.0
_FILE_NAME: str = "alerts.json"


class AlertLog:
    """Thread-safe, JSON-backed alert log.

    Parameters
    ----------
    path:
        Directory for the ``alerts.json`` persistence file.  Pass ``None``
        to run in-memory only (useful in tests or read-only environments).
    max_entries:
        Maximum number of entries to retain; oldest are dropped first.
    """

    def __init__(self, path: Path | None, max_entries: int = 100) -> None:
        self._path = (Path(path) / _FILE_NAME) if path is not None else None
        self._max = max_entries
        self._entries: list[dict] = []
        self._lock = threading.Lock()
        self._dirty = False
        self._last_write: float = 0.0
        self._load()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def record(
        self,
        severity: str,
        message: str,
        kind: str = "",
        lat: float | None = None,
        lon: float | None = None,
    ) -> dict:
        """Append a new alert entry and return it.

        Parameters
        ----------
        severity:
            One of ``"info"``, ``"warn"``, ``"alarm"``.
        message:
            Human-readable alert text.
        kind:
            Optional machine-readable category (e.g. ``"depth"``, ``"anchor"``).
        lat, lon:
            Optional GPS coordinates where the event occurred.
        """
        entry: dict = {
            "ts": int(time.time() * 1000),  # epoch ms, matching the JS side
            "severity": _norm_severity(severity),
            "message": str(message).strip(),
        }
        if kind:
            entry["kind"] = str(kind)
        if lat is not None and lon is not None:
            entry["lat"] = float(lat)
            entry["lon"] = float(lon)
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self._max:
                self._entries = self._entries[-self._max:]
            self._maybe_write()
        return entry

    def snapshot(self) -> list[dict]:
        """Return a shallow copy of all entries (oldest first)."""
        with self._lock:
            return list(self._entries)

    def clear(self) -> None:
        """Remove all entries and flush to disk immediately."""
        with self._lock:
            self._entries = []
            self._last_write = 0.0  # force immediate flush
            self._flush()

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            entries = data.get("alerts") if isinstance(data, dict) else None
            if isinstance(entries, list):
                valid = [
                    e for e in entries
                    if isinstance(e, dict) and isinstance(e.get("message"), str)
                ]
                self._entries = valid[-self._max:]
        except Exception:  # noqa: BLE001 - corrupt file → start fresh
            logger.warning("alertlog: corrupt alerts.json — starting fresh")
            self._entries = []

    def _maybe_write(self) -> None:
        """Write if enough time has passed since the last flush (debounce)."""
        now = time.monotonic()
        if now - self._last_write >= _DEBOUNCE_S:
            self._flush()

    def _flush(self) -> None:
        """Write the current entries to disk (must be called under self._lock)."""
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"alerts": self._entries}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self._path)
            self._last_write = time.monotonic()
        except Exception:  # noqa: BLE001 - disk full / permission error
            logger.warning("alertlog: failed to write alerts.json", exc_info=True)


def _norm_severity(s: str) -> str:
    s = str(s or "info").lower()
    return s if s in {"info", "warn", "alarm"} else "info"
