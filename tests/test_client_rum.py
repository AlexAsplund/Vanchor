"""Client-RUM ingestion: /api/client-log entries -> logs + debug recordings."""
import gzip
import json
import logging
import os

from vanchor.app import Runtime
from vanchor.core.config import load


def _rt(tmp_path) -> Runtime:
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    return Runtime(cfg)


def test_entries_land_in_the_client_logger(tmp_path):
    rt = _rt(tmp_path)
    records: list = []
    h = logging.Handler()
    h.emit = records.append  # type: ignore[assignment]
    logging.getLogger("vanchor.client").addHandler(h)
    try:
        n = rt.client_log([
            {"level": "error", "event": "js_error", "msg": "boom @app.js:12"},
            {"level": "info", "event": "visibility", "msg": "hidden"},
        ], session="abc123")
    finally:
        logging.getLogger("vanchor.client").removeHandler(h)
    assert n == 2
    texts = [r.getMessage() for r in records]
    assert any("js_error" in t and "boom" in t and "abc123" in t for t in texts)
    levels = {r.levelno for r in records}
    assert logging.ERROR in levels and logging.INFO in levels


def test_bounded_and_junk_tolerant(tmp_path):
    rt = _rt(tmp_path)
    n = rt.client_log([{"event": "e", "msg": "x" * 2000}] * 80 + ["junk", None], session="s")
    assert n == 50                                   # hard cap per call
    assert rt.client_log([], session="s") == 0
    assert rt.client_log(["nope", 4], session="s") == 0


def test_entries_recorded_into_active_debug_session(tmp_path):
    rt = _rt(tmp_path)
    rt.debug.start("rumtest", 1000.0)
    rt.client_log([{"level": "warn", "event": "geo_gap", "msg": "7.5s between fixes"}],
                  session="phone1")
    info = rt.debug.stop()
    assert info["counts"].get("client") == 1
    # the structured entry survives in the recording parts
    sess_dir = os.path.join(str(tmp_path), "debug", "rumtest")
    payload = b"".join(
        gzip.decompress(open(os.path.join(sess_dir, f), "rb").read())
        for f in sorted(os.listdir(sess_dir)) if f.endswith(".ndjson.gz")
    ).decode()
    lines = [json.loads(line) for line in payload.splitlines() if line.strip()]
    client = [ln for ln in lines if ln.get("kind") == "client"]
    assert client and client[0]["data"]["event"] == "geo_gap"
    assert client[0]["data"]["session"] == "phone1"
