"""Tests for the debug session recorder + replay."""

import gzip
import json

from vanchor.core.debug_recorder import DebugRecorder, ReplayPlayer


def test_records_gzipped_ndjson(tmp_path):
    rec = DebugRecorder(str(tmp_path))
    rec.start("sess1", now=100.0)
    rec.write("nmea", "$GPRMC,...", now=100.1)
    rec.write("telemetry", {"mode": "manual", "heading_deg": 5}, now=100.2)
    rec.write("command", {"type": "stop"}, now=100.3)
    st = rec.stop()
    assert st["counts"]["telemetry"] == 1 and st["counts"]["nmea"] == 1

    path = rec.path_for("sess1.ndjson.gz")
    assert path is not None
    with gzip.open(path, "rt") as fh:
        kinds = [json.loads(line)["kind"] for line in fh]
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
    assert rp.load(rec.path_for("s.ndjson.gz"), now=1000.0)
    # at t0 -> first frame
    f0 = rp.current(1000.0)
    assert f0["heading_deg"] == 0 and f0["replay"]["total"] == 5
    # 2.5 s later -> third frame (index advances by recorded time)
    f2 = rp.current(1002.5)
    assert f2["heading_deg"] == 20 and f2["replay"]["index"] == 3
    rp.stop()
    assert rp.current(1003.0) is None
