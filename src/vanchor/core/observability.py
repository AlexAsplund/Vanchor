"""Logging setup, telemetry recording, and an event-bus wiretap.

Three small, independent observability helpers:

* :func:`setup_logging` configures the root logger consistently for the app
  and tests.
* :class:`TelemetryRecorder` keeps an in-memory ring of recent telemetry
  snapshots and (optionally) appends each one as a JSON line to a file, so a
  run can be replayed or inspected after the fact.
* :func:`wiretap` attaches a wildcard subscriber to the :class:`EventBus` that
  logs every ``(topic, payload)`` at DEBUG -- a cheap, central trace of the
  whole system's message flow.
* :class:`DecisionLog` is an optional ring buffer answering "why did the
  controller do that" by recording human-readable reasons with fields.

Everything here is deliberately defensive: recording must never crash the
control loop, so serialization failures and file errors are swallowed and
logged rather than propagated.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, TextIO

from .events import EventBus

logger = logging.getLogger("vanchor.observability")

DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class RingLogHandler(logging.Handler):
    """Keep the most recent log records in memory for the "View logs" UI.

    A bounded ring of decoded records (time/level/logger/message). Cheap and
    crash-safe; :func:`log_ring` returns the process-wide singleton so the buffer
    survives repeated :func:`setup_logging` calls."""

    def __init__(self, capacity: int = 800) -> None:
        super().__init__()
        self.records: Deque[dict] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append({
                "t": record.created,
                "level": record.levelname,
                "levelno": record.levelno,
                "name": record.name,
                "msg": record.getMessage(),
            })
        except Exception:  # pragma: no cover - logging must never raise
            pass

    def dump(self, min_levelno: int = 0, limit: int = 500,
             contains: str | None = None) -> list[dict]:
        """Newest-last records at/above ``min_levelno``, optionally text-filtered."""
        rows = [r for r in self.records if r["levelno"] >= min_levelno]
        if contains:
            needle = contains.lower()
            rows = [r for r in rows if needle in r["msg"].lower() or needle in r["name"].lower()]
        return rows[-limit:]


_RING: RingLogHandler | None = None


def log_ring() -> RingLogHandler:
    """The process-wide in-memory log ring (created on first use)."""
    global _RING
    if _RING is None:
        _RING = RingLogHandler()
        _RING.setLevel(logging.INFO)
    return _RING


def setup_logging(level: str = "INFO", fmt: str | None = None) -> None:
    """Configure the root logger with a single stream handler.

    Safe to call more than once: existing handlers are cleared first so we do
    not accumulate duplicate handlers (and duplicate log lines) across calls.

    ``level`` is a standard level name (``"DEBUG"``, ``"INFO"`` ...). Unknown
    names fall back to ``INFO``.
    """
    numeric = logging.getLevelName(level.upper())
    if not isinstance(numeric, int):
        numeric = logging.INFO

    root = logging.getLogger()
    # Drop any handlers a previous setup (or basicConfig) installed.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt or DEFAULT_LOG_FORMAT))
    handler.setLevel(numeric)                 # console stays at the requested level
    root.addHandler(handler)

    # In-memory ring for the "View logs" UI. Capture INFO+ regardless of the
    # console level (so the log view is useful even at --log-level warning); the
    # singleton persists its buffer across repeated setup_logging() calls.
    ring = log_ring()
    root.addHandler(ring)
    root.setLevel(min(numeric, logging.INFO))  # let INFO+ reach the handlers


class TelemetryRecorder:
    """Keep recent telemetry snapshots in memory and optionally on disk.

    Each snapshot is a plain ``dict`` (typically ``NavigationState.to_dict()``).
    Calling :meth:`record` always appends to an in-memory ring buffer of at most
    ``ring_size`` entries, and -- when a ``path`` is configured and the file is
    open -- also writes the snapshot as one JSON line (JSONL).

    The recorder is usable with ``path=None`` for memory-only operation, and is
    a no-op-safe context-manager-free design: call :meth:`start` to open the
    file, :meth:`stop`/:meth:`close` to flush and close it.
    """

    def __init__(self, path: str | Path | None = None, ring_size: int = 600) -> None:
        self.path: Path | None = Path(path) if path is not None else None
        self.ring_size = ring_size
        self._ring: Deque[dict] = deque(maxlen=ring_size)
        self._file: TextIO | None = None

    def start(self) -> None:
        """Open the backing file for appending (no-op when memory-only)."""
        if self.path is None or self._file is not None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("a", encoding="utf-8")
        except OSError:  # pragma: no cover - defensive
            logger.exception("could not open telemetry file %s", self.path)
            self._file = None

    def record(self, snapshot: dict) -> None:
        """Append ``snapshot`` to the ring buffer and, if open, to the file."""
        self._ring.append(snapshot)
        if self._file is None:
            return
        try:
            self._file.write(json.dumps(snapshot, default=str) + "\n")
            self._file.flush()
        except (OSError, TypeError, ValueError):  # pragma: no cover - defensive
            logger.exception("could not write telemetry snapshot")

    def recent(self, n: int = 50) -> list[dict]:
        """Return up to the last ``n`` recorded snapshots (oldest first)."""
        if n <= 0:
            return []
        items = list(self._ring)
        return items[-n:]

    def stop(self) -> None:
        """Flush and close the backing file (no-op when memory-only)."""
        self.close()

    def close(self) -> None:
        """Close the backing file; the ring buffer is left intact."""
        if self._file is None:
            return
        try:
            self._file.flush()
            self._file.close()
        except OSError:  # pragma: no cover - defensive
            logger.exception("could not close telemetry file")
        finally:
            self._file = None

    def __len__(self) -> int:
        return len(self._ring)


def wiretap(bus: EventBus, logger: logging.Logger | None = None) -> None:
    """Log every event published on ``bus`` at DEBUG level.

    Attaches a wildcard subscriber via :meth:`EventBus.subscribe_all`. Cheap
    when DEBUG is disabled because the handler short-circuits before formatting
    the payload.
    """
    log = logger or logging.getLogger("vanchor.wiretap")

    def _tap(topic: str, payload: Any) -> None:
        if log.isEnabledFor(logging.DEBUG):
            log.debug("event %s -> %r", topic, payload)

    bus.subscribe_all(_tap)


@dataclass
class _Decision:
    """One recorded decision: when, why, and any structured detail."""

    timestamp: float
    reason: str
    fields: dict[str, Any]


@dataclass
class DecisionLog:
    """A small ring buffer of controller decisions for after-the-fact debugging.

    The controller calls :meth:`record` with a short human-readable ``reason``
    and any structured ``fields``; :meth:`recent` returns the most recent
    entries (as dicts) for display in the UI or an API endpoint.
    """

    ring_size: int = 200
    _ring: Deque[_Decision] = field(default_factory=deque, init=False, repr=False)

    def __post_init__(self) -> None:
        self._ring = deque(maxlen=self.ring_size)

    def record(self, reason: str, **fields: Any) -> None:
        self._ring.append(_Decision(time.time(), reason, dict(fields)))

    def recent(self, n: int = 50) -> list[dict]:
        if n <= 0:
            return []
        items = list(self._ring)[-n:]
        return [
            {"timestamp": d.timestamp, "reason": d.reason, **d.fields} for d in items
        ]

    def __len__(self) -> int:
        return len(self._ring)
