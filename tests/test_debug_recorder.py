"""Tests for the debug session recorder + replay.

A session is a directory of gzip *chunks* (parts). Completed parts are closed
with a valid trailer (crash-safe); the open part is flushed periodically.
"""

import gzip
import json
import logging
import time

import vanchor.core.debug_recorder as dr
from vanchor.core.debug_recorder import DebugRecorder, ReplayPlayer


def _detach_log_capture(rec):
    """Stop capturing ambient (real-timestamp) logs, so a test can drive record
    time manually without those log writes perturbing chunk rotation."""
    if rec._log_handler is not None:
        logging.getLogger().removeHandler(rec._log_handler)
        rec._log_handler = None


def _read_all(session_path):
    """All decoded NDJSON records across a session's parts (tolerating a
    crash-truncated final part)."""
    out = []
    for part in dr._part_paths(session_path):
        try:
            with gzip.open(part, "rt") as fh:
                for line in fh:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        except (OSError, EOFError):
            break
    return out


def test_records_gzipped_ndjson(tmp_path):
    rec = DebugRecorder(str(tmp_path))
    rec.start("sess1", now=100.0)
    rec.write("nmea", "$GPRMC,...", now=100.1)
    rec.write("telemetry", {"mode": "manual", "heading_deg": 5}, now=100.2)
    rec.write("command", {"type": "stop"}, now=100.3)
    st = rec.stop()
    assert st["counts"]["telemetry"] == 1 and st["counts"]["nmea"] == 1

    path = rec.path_for("sess1")  # the session directory
    assert path is not None
    # ignore any captured "log" lines (ambient app logs) — assert our writes:
    kinds = [r["kind"] for r in _read_all(path) if r["kind"] != "log"]
    assert kinds == ["meta", "nmea", "telemetry", "command"]
    assert any(s["name"] == "sess1" for s in rec.sessions())


def test_path_for_blocks_traversal(tmp_path):
    rec = DebugRecorder(str(tmp_path))
    assert rec.path_for("../../etc/passwd") is None


def test_replay_plays_telemetry_frames(tmp_path):
    rec = DebugRecorder(str(tmp_path))
    rec.start("s", now=0.0)
    for i in range(5):
        rec.write("telemetry", {"heading_deg": i * 10}, now=float(i))  # 1 s apart
    rec.stop()

    rp = ReplayPlayer()
    assert rp.load(rec.path_for("s"), now=1000.0)
    f0 = rp.current(1000.0)
    assert f0["heading_deg"] == 0 and f0["replay"]["total"] == 5
    f2 = rp.current(1002.5)
    assert f2["heading_deg"] == 20 and f2["replay"]["index"] == 3
    rp.stop()
    assert rp.current(1003.0) is None


def test_rotates_into_multiple_valid_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "CHUNK_SECONDS", 10.0)  # rotate every 10 s of record time
    rec = DebugRecorder(str(tmp_path))
    rec.start("multi", now=0.0)
    _detach_log_capture(rec)  # this test drives `now` manually
    for i in range(25):
        rec.write("telemetry", {"i": i}, now=float(i))  # 0..24 -> parts at 10, 20
    rec.stop()

    session = rec.path_for("multi")
    parts = dr._part_paths(session)
    assert len(parts) >= 3
    for p in parts:                      # every part is a complete, valid gzip
        with gzip.open(p, "rt") as fh:
            assert fh.read()
    frames = [r["data"]["i"] for r in _read_all(session) if r["kind"] == "telemetry"]
    assert frames == list(range(25))     # all recovered, in order, across parts

    s = next(x for x in rec.sessions() if x["name"] == "multi")
    assert s["parts"] == len(parts) and s["bytes"] > 0


def test_completed_parts_survive_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "CHUNK_SECONDS", 10.0)
    rec = DebugRecorder(str(tmp_path))
    rec.start("crash", now=0.0)
    _detach_log_capture(rec)  # this test drives `now` manually
    for i in range(15):
        rec.write("telemetry", {"i": i}, now=float(i))  # rotates at 10: part1 closed
    # SIMULATE A CRASH: never call stop() -> part 2 is left open (no trailer).
    session = rec.path_for("crash")
    assert len(dr._part_paths(session)) >= 2

    player = ReplayPlayer()
    assert player.load(session, now=0.0)          # recovers from the closed part(s)
    recovered = [f["i"] for _, f in player._frames]
    assert recovered[:10] == list(range(10))      # the completed part is fully intact


def test_captures_app_logs(tmp_path):
    rec = DebugRecorder(str(tmp_path))
    rec.start("logs", now=time.time())
    logging.getLogger("vanchor.test").warning("hello-debug-capture")
    rec.stop()
    logs = [r for r in _read_all(rec.path_for("logs")) if r["kind"] == "log"]
    assert any("hello-debug-capture" in r["data"]["msg"] for r in logs)
    # and it isn't left attached to the root logger after stop
    assert rec._log_handler is None
