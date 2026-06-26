"""Tests for vanchor.core.observability."""

from __future__ import annotations

import json
import logging

from vanchor.core.events import EventBus
from vanchor.core.observability import (
    DecisionLog,
    TelemetryRecorder,
    setup_logging,
    wiretap,
)


def test_recorder_writes_jsonl_and_recent(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    rec = TelemetryRecorder(path=path, ring_size=100)
    rec.start()
    try:
        for i in range(10):
            rec.record({"i": i, "v": i * 2})
    finally:
        rec.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 10
    parsed = [json.loads(line) for line in lines]
    assert parsed[0] == {"i": 0, "v": 0}
    assert parsed[-1] == {"i": 9, "v": 18}

    last3 = rec.recent(3)
    assert [s["i"] for s in last3] == [7, 8, 9]
    assert len(rec) == 10


def test_recorder_ring_eviction():
    rec = TelemetryRecorder(path=None, ring_size=5)
    for i in range(20):
        rec.record({"i": i})
    assert len(rec) == 5
    assert [s["i"] for s in rec.recent(100)] == [15, 16, 17, 18, 19]
    assert rec.recent(0) == []


def test_recorder_memory_only_no_file(tmp_path):
    rec = TelemetryRecorder(path=None)
    rec.start()  # must be safe
    rec.record({"a": 1})
    rec.record({"a": 2})
    # No file should be created anywhere.
    assert list(tmp_path.iterdir()) == []
    assert rec.recent(1) == [{"a": 2}]
    rec.stop()  # safe no-op


def test_recorder_serializes_non_json_values(tmp_path):
    path = tmp_path / "t.jsonl"
    rec = TelemetryRecorder(path=path)
    rec.start()

    class Weird:
        def __str__(self) -> str:
            return "weird"

    rec.record({"obj": Weird()})
    rec.close()
    line = json.loads(path.read_text(encoding="utf-8").strip())
    assert line == {"obj": "weird"}


async def test_wiretap_captures_published_events(caplog):
    bus = EventBus()
    log = logging.getLogger("vanchor.wiretap.test")
    log.setLevel(logging.DEBUG)
    wiretap(bus, logger=log)

    with caplog.at_level(logging.DEBUG, logger="vanchor.wiretap.test"):
        await bus.publish("nav.fix", {"lat": 1.0})
        await bus.publish("motor.command", {"thrust": 0.5})

    messages = [r.getMessage() for r in caplog.records]
    assert any("nav.fix" in m for m in messages)
    assert any("motor.command" in m for m in messages)


async def test_wiretap_custom_handler():
    bus = EventBus()
    captured: list[tuple[str, object]] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append((record.getMessage(), record.levelno))

    log = logging.getLogger("vanchor.wiretap.custom")
    log.setLevel(logging.DEBUG)
    log.addHandler(Capture())
    try:
        wiretap(bus, logger=log)
        await bus.publish("telemetry", {"x": 1})
    finally:
        log.handlers.clear()

    assert any("telemetry" in m and lvl == logging.DEBUG for m, lvl in captured)


def test_setup_logging_idempotent():
    setup_logging("DEBUG")
    root = logging.getLogger()
    first = list(root.handlers)
    assert len(first) == 1
    assert root.level == logging.DEBUG

    setup_logging("INFO")
    assert len(root.handlers) == 1  # no duplicate handler
    assert root.level == logging.INFO

    setup_logging("NOT_A_LEVEL")
    assert root.level == logging.INFO  # falls back


def test_decision_log():
    dl = DecisionLog(ring_size=3)
    dl.record("anchor set", distance=2.5, mode="ANCHOR_HOLD")
    dl.record("steering", err=10.0)
    dl.record("a")
    dl.record("b")
    recent = dl.recent(10)
    assert len(recent) == 3
    assert recent[0]["reason"] == "steering"
    assert recent[-1]["reason"] == "b"
    assert recent[0]["err"] == 10.0
    assert "timestamp" in recent[0]
    assert dl.recent(0) == []
