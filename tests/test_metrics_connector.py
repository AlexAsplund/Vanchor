"""Tests for the MetricsConnector (Task 4: store-and-forward, offline-first).

TDD order:
1. Buffer writes: offline (no url) accumulates parts, bulk keys stripped.
2. Flush mechanics: gunzip -> valid NDJSON; delete on 2xx; keep on 500/exception.
3. Size cap: drop-oldest when buffer_max_mb exceeded (never drop in-progress).
4. Interval throttling: sampled at interval_s, not every publish.
5. Transport exception never propagates.
6. Survive restart: a second instance picks up existing completed parts.
7. debug() always returns a string, never raises.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import time
from pathlib import Path
from typing import Any

import pytest

from vanchor.connectors.context import ConnectorContext
from vanchor.connectors.metrics import (
    MANIFEST,
    MetricsConnector,
    _BULK_KEYS,
)
from vanchor.core.events import EventBus

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_FAKE_TELEM = {
    "mode": "anchor",
    "lat": 47.5,
    "lon": -122.3,
    "depth_points": [[1, 2, 3]],   # bulk key → stripped
    "waypoints": [{"lat": 1}],       # bulk key → stripped
    "safety_geometry": {"type": "FeatureCollection"},  # bulk key → stripped
    "track": {"recording": True, "points": [[0, 0]]},  # bulk key → stripped
    "sog_knots": 0.1,
}


def _make_ctx(bus: EventBus) -> ConnectorContext:
    return ConnectorContext(bus, MANIFEST, command_sink=lambda cmd: None)


def _read_ndjson(path: Path) -> list[dict]:
    """Gunzip a part file and parse every NDJSON line."""
    lines: list[dict] = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                lines.append(json.loads(line))
    return lines


def _all_parts(buf_dir: Path) -> list[Path]:
    """Return all .ndjson.gz parts sorted by name."""
    return sorted(buf_dir.glob("*.ndjson.gz"))


def _completed_parts(buf_dir: Path, current: Path | None) -> list[Path]:
    return [p for p in _all_parts(buf_dir) if p != current]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Manifest
# ─────────────────────────────────────────────────────────────────────────────

def test_manifest_fields() -> None:
    assert MANIFEST.name == "metrics"
    assert "telemetry" in MANIFEST.consumes
    assert MANIFEST.produces == ()
    assert MANIFEST.control is False
    assert len(MANIFEST.grant_lines) >= 1


def test_bulk_keys_set() -> None:
    assert "depth_points" in _BULK_KEYS
    assert "waypoints" in _BULK_KEYS
    assert "safety_geometry" in _BULK_KEYS
    assert "track" in _BULK_KEYS


# ─────────────────────────────────────────────────────────────────────────────
# 2. Offline buffering: samples accumulate, bulk keys stripped
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_offline_buffering_creates_buffer_dir(tmp_path: Path) -> None:
    clock = [0.0]
    conn = MetricsConnector(
        data_dir=tmp_path,
        interval_s=1.0,
        mono_fn=lambda: clock[0],
    )
    bus = EventBus()
    ctx = _make_ctx(bus)
    await conn.start(ctx)
    try:
        # Advance clock past interval, then publish
        clock[0] = 2.0
        await bus.publish("telemetry", _FAKE_TELEM)
    finally:
        await conn.stop()

    buf = tmp_path / "metrics_buffer"
    assert buf.is_dir()
    parts = list(buf.glob("*.ndjson.gz"))
    assert len(parts) >= 1


@pytest.mark.asyncio
async def test_offline_buffering_bulk_keys_stripped(tmp_path: Path) -> None:
    clock = [0.0]
    conn = MetricsConnector(
        data_dir=tmp_path,
        interval_s=0.0,  # accept every publish
        mono_fn=lambda: clock[0],
    )
    bus = EventBus()
    ctx = _make_ctx(bus)
    await conn.start(ctx)
    try:
        clock[0] = 1.0
        await bus.publish("telemetry", _FAKE_TELEM)
        clock[0] = 2.0
        await bus.publish("telemetry", _FAKE_TELEM)
    finally:
        await conn.stop()

    buf = tmp_path / "metrics_buffer"
    parts = sorted(buf.glob("*.ndjson.gz"))
    assert parts, "expected at least one part file"
    # Read all parts, collect all records
    records: list[dict] = []
    for p in parts:
        records.extend(_read_ndjson(p))
    assert records, "no records found"
    for rec in records:
        for bulk_key in _BULK_KEYS:
            assert bulk_key not in rec, f"bulk key {bulk_key!r} should be stripped"
    # Real keys present
    assert all("t" in r for r in records)
    assert all("mode" in r for r in records)


@pytest.mark.asyncio
async def test_offline_no_url_no_network_touch(tmp_path: Path) -> None:
    """With no url, flush does nothing (no network call)."""
    sends: list[Any] = []

    def spy_send(url: str, body: bytes, headers: dict) -> int:  # pragma: no cover
        sends.append(url)
        return 200

    clock = [0.0]
    conn = MetricsConnector(
        data_dir=tmp_path,
        url="",
        interval_s=0.0,
        mono_fn=lambda: clock[0],
        send=spy_send,
    )
    bus = EventBus()
    ctx = _make_ctx(bus)
    await conn.start(ctx)
    try:
        clock[0] = 1.0
        await bus.publish("telemetry", _FAKE_TELEM)
        await conn._rotate_current()  # force close/complete the part
        await conn._do_flush()
    finally:
        await conn.stop()

    assert sends == [], "no sends expected when url is empty"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Flush mechanics
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flush_deletes_on_200(tmp_path: Path) -> None:
    sent: list[tuple[str, bytes, dict]] = []

    def fake_send(url: str, body: bytes, headers: dict) -> int:
        sent.append((url, body, headers))
        return 200

    clock = [0.0]
    conn = MetricsConnector(
        data_dir=tmp_path,
        url="http://metrics.example.com/ingest",
        interval_s=0.0,
        mono_fn=lambda: clock[0],
        send=fake_send,
    )
    bus = EventBus()
    ctx = _make_ctx(bus)
    await conn.start(ctx)
    try:
        clock[0] = 1.0
        await bus.publish("telemetry", _FAKE_TELEM)
        clock[0] = 2.0
        await bus.publish("telemetry", _FAKE_TELEM)
        # Rotate to make the part completed
        await conn._rotate_current()
        await conn._do_flush()
        # Check before stop() so _current_part is not None
        buf = tmp_path / "metrics_buffer"
        completed = [p for p in buf.glob("*.ndjson.gz") if p != conn._current_part]
        assert completed == [], "completed part should be deleted after 200"
    finally:
        await conn.stop()

    assert len(sent) == 1, f"expected 1 POST, got {len(sent)}"
    _url, body, headers = sent[0]
    assert _url == "http://metrics.example.com/ingest"
    assert headers.get("Content-Encoding") == "gzip"
    assert headers.get("Content-Type") == "application/x-ndjson"

    # gunzip and parse NDJSON
    lines = gzip.decompress(body).decode("utf-8").strip().split("\n")
    records = [json.loads(l) for l in lines if l.strip()]
    assert len(records) == 2
    assert all("t" in r for r in records)
    assert all("mode" in r for r in records)
    assert all("depth_points" not in r for r in records)


@pytest.mark.asyncio
async def test_flush_keeps_on_500(tmp_path: Path) -> None:
    def fake_send(url: str, body: bytes, headers: dict) -> int:
        return 500

    clock = [0.0]
    conn = MetricsConnector(
        data_dir=tmp_path,
        url="http://metrics.example.com/ingest",
        interval_s=0.0,
        mono_fn=lambda: clock[0],
        send=fake_send,
    )
    bus = EventBus()
    ctx = _make_ctx(bus)
    await conn.start(ctx)
    try:
        clock[0] = 1.0
        await bus.publish("telemetry", _FAKE_TELEM)
        await conn._rotate_current()
        before = _all_parts(tmp_path / "metrics_buffer")
        await conn._do_flush()
        after = _all_parts(tmp_path / "metrics_buffer")
    finally:
        await conn.stop()

    assert before == after, "parts should be kept on 500 response"


@pytest.mark.asyncio
async def test_flush_keeps_on_exception(tmp_path: Path) -> None:
    def exploding_send(url: str, body: bytes, headers: dict) -> int:
        raise RuntimeError("network down")

    clock = [0.0]
    conn = MetricsConnector(
        data_dir=tmp_path,
        url="http://metrics.example.com/ingest",
        interval_s=0.0,
        mono_fn=lambda: clock[0],
        send=exploding_send,
    )
    bus = EventBus()
    ctx = _make_ctx(bus)
    await conn.start(ctx)
    try:
        clock[0] = 1.0
        await bus.publish("telemetry", _FAKE_TELEM)
        await conn._rotate_current()
        before = _all_parts(tmp_path / "metrics_buffer")
        # Must NOT raise
        await conn._do_flush()
        after = _all_parts(tmp_path / "metrics_buffer")
    finally:
        await conn.stop()

    assert before == after, "parts should be kept on send exception"


@pytest.mark.asyncio
async def test_flush_bearer_token_in_header(tmp_path: Path) -> None:
    headers_seen: list[dict] = []

    def fake_send(url: str, body: bytes, headers: dict) -> int:
        headers_seen.append(dict(headers))
        return 200

    clock = [0.0]
    conn = MetricsConnector(
        data_dir=tmp_path,
        url="http://metrics.example.com/ingest",
        token="my-secret-token",
        interval_s=0.0,
        mono_fn=lambda: clock[0],
        send=fake_send,
    )
    bus = EventBus()
    ctx = _make_ctx(bus)
    await conn.start(ctx)
    try:
        clock[0] = 1.0
        await bus.publish("telemetry", _FAKE_TELEM)
        await conn._rotate_current()
        await conn._do_flush()
    finally:
        await conn.stop()

    assert headers_seen, "expected at least one POST"
    assert headers_seen[0].get("Authorization") == "Bearer my-secret-token"


@pytest.mark.asyncio
async def test_inprogress_part_not_flushed(tmp_path: Path) -> None:
    """The currently-open part must NOT be posted during flush."""
    sent: list[Any] = []

    def fake_send(url: str, body: bytes, headers: dict) -> int:
        sent.append(url)
        return 200

    clock = [0.0]
    conn = MetricsConnector(
        data_dir=tmp_path,
        url="http://metrics.example.com/ingest",
        interval_s=0.0,
        mono_fn=lambda: clock[0],
        send=fake_send,
    )
    bus = EventBus()
    ctx = _make_ctx(bus)
    await conn.start(ctx)
    try:
        clock[0] = 1.0
        await bus.publish("telemetry", _FAKE_TELEM)
        # Do NOT rotate — the part is still in-progress
        await conn._do_flush()
    finally:
        await conn.stop()

    assert sent == [], "in-progress part must never be POSTed"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Size cap: drop oldest
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_size_cap_drops_oldest_completed(tmp_path: Path) -> None:
    """When the buffer exceeds buffer_max_mb, the OLDEST completed part is deleted."""
    # Use a very small cap so even a tiny part triggers it
    clock = [0.0]
    conn = MetricsConnector(
        data_dir=tmp_path,
        interval_s=0.0,
        buffer_max_mb=0.0,  # effectively zero → every rotation triggers cap
        mono_fn=lambda: clock[0],
    )
    bus = EventBus()
    ctx = _make_ctx(bus)
    await conn.start(ctx)
    try:
        # Write part 1
        clock[0] = 1.0
        await bus.publish("telemetry", _FAKE_TELEM)
        await conn._rotate_current()
        buf = tmp_path / "metrics_buffer"
        parts_after_first = _all_parts(buf)

        # Write part 2 — enforcing cap should drop part 1
        clock[0] = 2.0
        await bus.publish("telemetry", _FAKE_TELEM)
        await conn._rotate_current()

        parts_after_second = _all_parts(buf)
    finally:
        await conn.stop()

    # The oldest part (part 1) should be gone
    surviving = [p.name for p in parts_after_second if p != conn._current_part]
    oldest_name = parts_after_first[0].name if parts_after_first else None
    assert oldest_name not in surviving, (
        f"oldest part {oldest_name!r} should have been deleted by cap; survivors: {surviving}"
    )


@pytest.mark.asyncio
async def test_size_cap_never_drops_inprogress(tmp_path: Path) -> None:
    """The cap must never delete the currently-open part."""
    clock = [0.0]
    conn = MetricsConnector(
        data_dir=tmp_path,
        interval_s=0.0,
        buffer_max_mb=0.0,  # aggressively small cap
        mono_fn=lambda: clock[0],
    )
    bus = EventBus()
    ctx = _make_ctx(bus)
    await conn.start(ctx)
    try:
        clock[0] = 1.0
        await bus.publish("telemetry", _FAKE_TELEM)
        # Don't rotate — _current_part is the open file
        conn._enforce_cap()
    finally:
        await conn.stop()

    # In-progress part must still exist
    if conn._current_part is not None:
        assert conn._current_part.exists(), "in-progress part must not be deleted by cap"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Interval throttling
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_interval_throttle_skips_rapid_publishes(tmp_path: Path) -> None:
    """Rapid publishes within interval_s are discarded."""
    clock = [0.0]
    conn = MetricsConnector(
        data_dir=tmp_path,
        interval_s=1.0,
        mono_fn=lambda: clock[0],
    )
    bus = EventBus()
    ctx = _make_ctx(bus)
    await conn.start(ctx)
    try:
        # First publish at t=0.0 → accepted
        await bus.publish("telemetry", _FAKE_TELEM)
        # Second publish immediately → throttled
        await bus.publish("telemetry", _FAKE_TELEM)
        # Third publish → throttled
        await bus.publish("telemetry", _FAKE_TELEM)
    finally:
        await conn.stop()

    assert conn._sample_count == 1, f"expected 1 sample, got {conn._sample_count}"


@pytest.mark.asyncio
async def test_interval_throttle_accepts_after_interval(tmp_path: Path) -> None:
    """After interval_s has elapsed, the next publish is accepted."""
    clock = [0.0]
    conn = MetricsConnector(
        data_dir=tmp_path,
        interval_s=1.0,
        mono_fn=lambda: clock[0],
    )
    bus = EventBus()
    ctx = _make_ctx(bus)
    await conn.start(ctx)
    try:
        # t=0 → accepted
        await bus.publish("telemetry", _FAKE_TELEM)
        # t=0.5 → throttled (< 1.0 s elapsed)
        clock[0] = 0.5
        await bus.publish("telemetry", _FAKE_TELEM)
        # t=2.0 → accepted (>= 1.0 s elapsed)
        clock[0] = 2.0
        await bus.publish("telemetry", _FAKE_TELEM)
    finally:
        await conn.stop()

    assert conn._sample_count == 2, f"expected 2 samples, got {conn._sample_count}"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Survive restart
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_survives_restart_picks_up_existing_parts(tmp_path: Path) -> None:
    """A new connector instance finds completed parts from the previous run."""
    clock = [0.0]

    # First instance: write + rotate a part
    conn1 = MetricsConnector(
        data_dir=tmp_path,
        interval_s=0.0,
        mono_fn=lambda: clock[0],
    )
    bus1 = EventBus()
    ctx1 = _make_ctx(bus1)
    await conn1.start(ctx1)
    clock[0] = 1.0
    await bus1.publish("telemetry", _FAKE_TELEM)
    await conn1._rotate_current()
    completed_before = _completed_parts(tmp_path / "metrics_buffer", conn1._current_part)
    await conn1.stop()

    assert completed_before, "first instance should have completed parts"

    # Second instance: should see the completed parts
    conn2 = MetricsConnector(
        data_dir=tmp_path,
        interval_s=0.0,
        mono_fn=lambda: clock[0],
    )
    bus2 = EventBus()
    ctx2 = _make_ctx(bus2)
    await conn2.start(ctx2)
    try:
        parts_seen = _completed_parts(tmp_path / "metrics_buffer", conn2._current_part)
    finally:
        await conn2.stop()

    assert len(parts_seen) >= len(completed_before), (
        "second instance should see at least the parts from the first run"
    )


@pytest.mark.asyncio
async def test_second_instance_can_flush_first_instance_parts(tmp_path: Path) -> None:
    """Second instance flushes parts created by the first instance."""
    sent: list[bytes] = []

    def fake_send(url: str, body: bytes, headers: dict) -> int:
        sent.append(body)
        return 200

    clock = [0.0]

    # First instance: write + stop (rotate happens on stop? no — only explicit rotate)
    conn1 = MetricsConnector(
        data_dir=tmp_path,
        interval_s=0.0,
        mono_fn=lambda: clock[0],
    )
    bus1 = EventBus()
    await conn1.start(_make_ctx(bus1))
    clock[0] = 1.0
    await bus1.publish("telemetry", _FAKE_TELEM)
    await conn1._rotate_current()  # complete the part
    await conn1.stop()

    # Second instance with a url: should flush the completed parts
    conn2 = MetricsConnector(
        data_dir=tmp_path,
        url="http://metrics.example.com/ingest",
        interval_s=0.0,
        mono_fn=lambda: clock[0],
        send=fake_send,
    )
    bus2 = EventBus()
    await conn2.start(_make_ctx(bus2))
    await conn2._do_flush()
    await conn2.stop()

    assert len(sent) >= 1, "second instance should flush parts from the first"
    # Verify content
    lines = gzip.decompress(sent[0]).decode("utf-8").strip().split("\n")
    records = [json.loads(l) for l in lines if l.strip()]
    assert any("t" in r for r in records)


# ─────────────────────────────────────────────────────────────────────────────
# 7. debug() always safe
# ─────────────────────────────────────────────────────────────────────────────

def test_debug_never_raises_before_start(tmp_path: Path) -> None:
    conn = MetricsConnector(data_dir=tmp_path)
    result = conn.debug()
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_debug_never_raises_while_running(tmp_path: Path) -> None:
    clock = [0.0]
    conn = MetricsConnector(
        data_dir=tmp_path,
        interval_s=0.0,
        mono_fn=lambda: clock[0],
    )
    bus = EventBus()
    await conn.start(_make_ctx(bus))
    try:
        clock[0] = 1.0
        await bus.publish("telemetry", _FAKE_TELEM)
        result = conn.debug()
        assert isinstance(result, str)
        assert "sample" in result.lower() or "metrics" in result.lower()
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_debug_shows_url_set(tmp_path: Path) -> None:
    conn = MetricsConnector(
        data_dir=tmp_path,
        url="http://metrics.example.com/ingest",
    )
    result = conn.debug()
    assert "url" in result.lower() or "http" in result.lower()


# ─────────────────────────────────────────────────────────────────────────────
# 8. Registration
# ─────────────────────────────────────────────────────────────────────────────

def test_connector_is_registered() -> None:
    """Importing the module registers the metrics connector."""
    from vanchor.connectors import registry as reg
    # The import side-effect registers it
    import vanchor.connectors.metrics  # noqa: F401
    assert reg.has("metrics"), "metrics connector should be registered"


def test_build_from_settings(tmp_path: Path) -> None:
    from vanchor.connectors.registry import build
    import vanchor.connectors.metrics  # noqa: F401

    conn = build("metrics", {"data_dir": str(tmp_path), "url": "", "interval_s": 2.0})
    assert isinstance(conn, MetricsConnector)
