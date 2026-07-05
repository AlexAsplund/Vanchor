"""Metrics connector — store-and-forward, offline-first telemetry export.

Subscribes to the ``telemetry`` bus topic, samples at ``interval_s`` (default
1.0 s) to throttle the ~5 Hz bus rate, and buffers each sample as one NDJSON
line in gzipped part files under ``<data_dir>/metrics_buffer/``.

**Settings dict keys**

=====================  ========  ================================================
Key                    Default   Meaning
=====================  ========  ================================================
``data_dir``           *none*    Root data directory; buffer goes in
                                 ``<data_dir>/metrics_buffer/``.
                                 Falls back to the current working directory when
                                 absent from settings (not recommended for
                                 production — always pass an explicit path).
``url``                ``""``    POST endpoint.  When empty, data is buffered but
                                 never sent (pure offline mode, useful while
                                 testing or when no server is reachable).
``token``              ``""``    Optional Bearer token added to ``Authorization``
                                 headers.
``interval_s``         ``1.0``   Minimum seconds between recorded samples.
``flush_interval_s``   ``30``    How often the flush loop runs (seconds).
``buffer_max_mb``      ``50``    Maximum total buffer size in MiB.  When exceeded
                                 the OLDEST completed part is deleted.
=====================  ========  ================================================

**Wire protocol**

Each POST carries a completed gzip part file as the request body with headers
``Content-Encoding: gzip`` and ``Content-Type: application/x-ndjson``.  Every
line in the decompressed body is a JSON object::

    {"t": <unix_seconds_float>, "mode": "anchor", "lat": 47.5, ...}

The bulky array fields ``depth_points``, ``waypoints``, ``safety_geometry`` and
``track`` (same set as ``server.py``'s ``_BULK_KEYS``/``shape_frame``) are
stripped before writing to keep part sizes reasonable.

**Offline-first guarantee**

* Parts persist across restart — a new instance picks up any existing completed
  parts and sends them on the next flush cycle.
* A flush failure (network error, non-2xx) keeps the part intact for retry.
* Exceptions from the transport callable never propagate out of the connector.

**Transport seam**

The ``send`` parameter is a callable ``(url: str, body: bytes, headers: dict)
-> int`` (returning the HTTP status code).  The default implementation uses
``httpx``.  Tests inject a fake.

**Clock seam**

``mono_fn`` (default ``time.monotonic``) drives all internal timing so tests
can advance time without sleeping.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import time
from pathlib import Path
from typing import IO, Any, Callable

from .base import Connector, ConnectorManifest
from .context import ConnectorContext
from .registry import register_connector

logger = logging.getLogger("vanchor.connectors.metrics")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

#: Rotate the current part file once it reaches this many compressed bytes.
_ROTATION_BYTES: int = 512 * 1024  # 512 KB

#: Part filename suffix.
_SUFFIX = ".ndjson.gz"

#: Bulk keys stripped from every telemetry sample (mirrors ``server.py``).
_BULK_KEYS: frozenset[str] = frozenset(
    {"depth_points", "waypoints", "safety_geometry", "track"}
)

#: Minimum seconds between throttled "flush failed" log lines.
_LOG_THROTTLE = 60.0

# ─────────────────────────────────────────────────────────────────────────────
# Manifest
# ─────────────────────────────────────────────────────────────────────────────

MANIFEST = ConnectorManifest(
    name="metrics",
    label="Metrics Export",
    description=(
        "Samples live telemetry at ~1 Hz, buffers it to disk as gzipped NDJSON "
        "part files, and ships them to a configured endpoint whenever the network "
        "is reachable.  Operates entirely offline when no endpoint is configured."
    ),
    consumes=("telemetry",),
    produces=(),
    control=False,
    grant_lines=(
        "Read live telemetry",
        "Store it on disk and send it to your configured server whenever the "
        "network is reachable",
    ),
)

# ─────────────────────────────────────────────────────────────────────────────
# Default send implementation (httpx)
# ─────────────────────────────────────────────────────────────────────────────


def _httpx_send(url: str, body: bytes, headers: dict) -> int:
    """Default transport: a blocking httpx POST.  Re-raises transport errors;
    the caller (_do_flush) catches and keeps the part for retry."""
    try:
        import httpx  # optional; available in the project venv

        resp = httpx.post(url, content=body, headers=headers, timeout=30.0)
        return resp.status_code
    except Exception as exc:  # noqa: BLE001 — transport failures are handled by caller
        raise exc  # re-raise so the caller can log and decide


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _safe_size(path: Path) -> int:
    """Return the file size of ``path``, or 0 if inaccessible."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _seq_from_name(name: str) -> int:
    """Extract the sequence integer from a part filename like ``00000003.ndjson.gz``."""
    stem = name[: -len(_SUFFIX)] if name.endswith(_SUFFIX) else name
    try:
        return int(stem)
    except ValueError:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# MetricsConnector
# ─────────────────────────────────────────────────────────────────────────────


class MetricsConnector(Connector):
    """Store-and-forward telemetry exporter.

    See module docstring for full settings reference.
    """

    manifest = MANIFEST

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        interval_s: float = 1.0,
        flush_interval_s: float = 30.0,
        buffer_max_mb: float = 50.0,
        url: str = "",
        token: str = "",
        send: Callable[[str, bytes, dict], int] | None = None,
        mono_fn: Callable[[], float] = time.monotonic,
        wall_fn: Callable[[], float] = time.time,
    ) -> None:
        # Settings
        self._buf_dir: Path = (
            Path(data_dir) if data_dir else Path(".")
        ) / "metrics_buffer"
        self._interval_s = interval_s
        self._flush_interval_s = flush_interval_s
        self._buffer_max_bytes = int(buffer_max_mb * 1024 * 1024)
        self._url = url
        self._token = token
        self._send = send if send is not None else _httpx_send
        self._mono = mono_fn
        self._wall = wall_fn

        # Buffer state
        self._seq: int = 0                # next part sequence number
        self._current_part: Path | None = None
        self._fh: IO[str] | None = None

        # Metrics
        self._sample_count: int = 0
        self._last_sample_mono: float = -999999.0

        # Flush state
        self._last_flush_wall: float | None = None
        self._last_flush_result: str = "never"
        self._last_flush_log_mono: float = -999999.0

        # Rotation state
        self._rotation_pending: bool = False

        # Task
        self._flush_task: asyncio.Task[None] | None = None

    # ─── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self, ctx: ConnectorContext) -> None:
        """Subscribe to telemetry and start the flush loop."""
        # Guard against double-start: cancel any existing flush task
        if self._flush_task is not None:
            self._flush_task.cancel()
        self._buf_dir.mkdir(parents=True, exist_ok=True)
        self._seq = self._next_seq()
        self._open_new_part()
        ctx.subscribe("telemetry", self._on_telemetry)
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("metrics connector started; buffer=%s url=%s", self._buf_dir, self._url or "(none)")

    async def stop(self) -> None:
        """Stop the flush loop and close the current part."""
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        self._close_current_part()
        logger.info("metrics connector stopped")

    # ─── Part file management ───────────────────────────────────────────────

    def _next_seq(self) -> int:
        """Find the highest existing sequence number and return the next one."""
        existing = list(self._buf_dir.glob(f"*{_SUFFIX}"))
        if not existing:
            return 1
        return max(_seq_from_name(p.name) for p in existing) + 1

    def _part_path(self, seq: int) -> Path:
        return self._buf_dir / f"{seq:08d}{_SUFFIX}"

    def _open_new_part(self) -> None:
        """Open a new gzip part file for writing."""
        self._close_current_part()
        path = self._part_path(self._seq)
        self._current_part = path
        self._fh = gzip.open(str(path), "wt", encoding="utf-8")
        self._seq += 1

    def _close_current_part(self) -> None:
        """Flush, fsync, and close the in-progress part (if any)."""
        if self._fh is None:
            return
        try:
            self._fh.flush()
            try:
                # Try to fsync through the underlying buffer (best-effort)
                underlying = getattr(self._fh, "fileobj", None) or getattr(self._fh, "_fp", None)
                if underlying is not None:
                    try:
                        os.fsync(underlying.fileno())
                    except (OSError, AttributeError):
                        pass
            except Exception:  # noqa: BLE001
                pass
            self._fh.close()
        except OSError:
            pass
        finally:
            self._fh = None
            self._current_part = None

    async def _rotate_current(self) -> None:
        """Close the current part (completing it) and open a new one."""
        try:
            self._close_current_part()
            self._enforce_cap()
            self._open_new_part()
        finally:
            self._rotation_pending = False

    def _completed_parts(self) -> list[Path]:
        """Return all part files EXCEPT the currently-open one, oldest first."""
        return sorted(
            [
                p
                for p in self._buf_dir.glob(f"*{_SUFFIX}")
                if p != self._current_part
            ]
        )

    def _total_buffer_bytes(self) -> int:
        """Total size of ALL buffer files (including in-progress)."""
        return sum(_safe_size(p) for p in self._buf_dir.glob(f"*{_SUFFIX}"))

    def _enforce_cap(self) -> None:
        """Delete the oldest completed part(s) until total buffer ≤ cap.

        Never deletes the in-progress part.
        """
        while self._total_buffer_bytes() > self._buffer_max_bytes:
            completed = self._completed_parts()
            if not completed:
                break  # only in-progress part remains; cannot shrink further
            oldest = completed[0]
            try:
                oldest.unlink(missing_ok=True)
                logger.debug("metrics: dropped oldest part %s (cap enforcement)", oldest.name)
            except OSError as exc:
                logger.warning("metrics: could not delete %s: %s", oldest, exc)
                break  # avoid infinite loop if deletion fails

    # ─── Telemetry sampling ─────────────────────────────────────────────────

    def _on_telemetry(self, payload: dict) -> None:
        """Called by the event bus for every telemetry publish (~5 Hz)."""
        now = self._mono()
        if now - self._last_sample_mono < self._interval_s:
            return  # throttled
        self._last_sample_mono = now
        self._write_sample(payload)

    def _write_sample(self, payload: dict) -> None:
        """Strip bulk keys and append one NDJSON line to the current part."""
        if self._fh is None:
            return
        record = {"t": self._wall()}
        for k, v in payload.items():
            if k not in _BULK_KEYS:
                record[k] = v
        try:
            self._fh.write(json.dumps(record) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("metrics: failed to write sample: %s", exc)
            return
        self._sample_count += 1
        # Check if rotation is needed; schedule async rotation from sync context
        # Guard with _rotation_pending to prevent multiple rotations in the same loop turn
        if not self._rotation_pending and _safe_size(self._current_part) >= _ROTATION_BYTES:  # type: ignore[arg-type]
            self._rotation_pending = True
            try:
                asyncio.get_running_loop().create_task(self._rotate_current())
            except RuntimeError:
                self._rotation_pending = False  # reset if no running loop
                pass  # rotation deferred to next flush

    # ─── Flush loop ─────────────────────────────────────────────────────────

    async def _flush_loop(self) -> None:
        """Periodic flush task — driven by the monotonic clock so tests control timing."""
        last_flush = self._mono()
        while True:
            await asyncio.sleep(min(self._flush_interval_s, 5.0))
            now = self._mono()
            if now - last_flush >= self._flush_interval_s:
                last_flush = now
                await self._do_flush()

    async def _do_flush(self) -> None:
        """Flush all completed parts to the configured endpoint.

        Safe to call directly from tests.  Never raises.
        """
        if not self._url:
            return

        parts = self._completed_parts()
        if not parts:
            self._last_flush_result = "no completed parts"
            return

        headers: dict[str, str] = {
            "Content-Encoding": "gzip",
            "Content-Type": "application/x-ndjson",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        sent_count = 0
        error_count = 0

        for part in parts:
            try:
                body = part.read_bytes()
            except OSError as exc:
                logger.warning("metrics: could not read part %s: %s", part.name, exc)
                error_count += 1
                continue

            # Skip empty parts: if size is <= bare gzip header (30 bytes), delete instead of posting
            if len(body) < 30:
                try:
                    part.unlink(missing_ok=True)
                    logger.debug("metrics: deleted empty part %s (size %d)", part.name, len(body))
                    sent_count += 1
                except OSError as exc:
                    logger.warning("metrics: could not delete empty part %s: %s", part.name, exc)
                continue

            try:
                status = self._send(self._url, body, headers)
            except Exception as exc:  # noqa: BLE001
                now = self._mono()
                if now - self._last_flush_log_mono >= _LOG_THROTTLE:
                    self._last_flush_log_mono = now
                    logger.warning("metrics: send failed for %s: %s", part.name, exc)
                self._last_flush_result = f"error: {exc}"
                self._last_flush_wall = self._wall()
                error_count += 1
                continue

            if 200 <= status < 300:
                try:
                    part.unlink(missing_ok=True)
                    sent_count += 1
                except OSError as exc:
                    logger.warning("metrics: could not delete sent part %s: %s", part.name, exc)
            else:
                now = self._mono()
                if now - self._last_flush_log_mono >= _LOG_THROTTLE:
                    self._last_flush_log_mono = now
                    logger.warning(
                        "metrics: server returned %s for %s; keeping for retry",
                        status,
                        part.name,
                    )
                self._last_flush_result = f"http {status}"
                self._last_flush_wall = self._wall()
                error_count += 1

        self._last_flush_wall = self._wall()
        if error_count == 0 and sent_count > 0:
            self._last_flush_result = f"ok ({sent_count} parts sent)"
        elif sent_count > 0:
            self._last_flush_result = f"partial ({sent_count} sent, {error_count} failed)"
        elif error_count > 0 and sent_count == 0:
            pass  # result already set above per-error

    # ─── Status + debug ─────────────────────────────────────────────────────

    def status(self) -> dict:
        parts = self._completed_parts()
        total_bytes = self._total_buffer_bytes()
        return {
            "sample_count": self._sample_count,
            "completed_parts": len(parts),
            "total_buffer_bytes": total_bytes,
            "url_set": bool(self._url),
        }

    def debug(self) -> str:
        """Human-readable debug string.  Never raises."""
        try:
            parts = self._completed_parts()
            total_bytes = self._total_buffer_bytes()
            in_progress = self._current_part.name if self._current_part else "none"
            in_prog_size = _safe_size(self._current_part) if self._current_part else 0
            flush_info = (
                f"{self._last_flush_result} @ "
                f"{time.strftime('%H:%M:%S', time.localtime(self._last_flush_wall))}"
                if self._last_flush_wall is not None
                else "never"
            )
            return (
                f"MetricsConnector\n"
                f"  samples     : {self._sample_count}\n"
                f"  buffer_dir  : {self._buf_dir}\n"
                f"  in_progress : {in_progress} ({in_prog_size} bytes)\n"
                f"  completed   : {len(parts)} parts, {total_bytes} bytes total\n"
                f"  url_set     : {bool(self._url)}\n"
                f"  last_flush  : {flush_info}\n"
            )
        except Exception as exc:  # noqa: BLE001 — debug must never raise
            return f"MetricsConnector: debug error: {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# Factory + registration
# ─────────────────────────────────────────────────────────────────────────────


def _build(settings: dict) -> Connector:
    """Build a :class:`MetricsConnector` from persisted settings."""
    data_dir = settings.get("data_dir") or "."
    return MetricsConnector(
        data_dir=data_dir,
        interval_s=float(settings.get("interval_s", 1.0)),
        flush_interval_s=float(settings.get("flush_interval_s", 30.0)),
        buffer_max_mb=float(settings.get("buffer_max_mb", 50.0)),
        url=str(settings.get("url", "")),
        token=str(settings.get("token", "")),
    )


register_connector(
    "metrics",
    _build,
    label="Metrics Export",
)
