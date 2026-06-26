"""Debug session recorder + replay.

Records EVERYTHING that happens during a session -- telemetry snapshots, raw
NMEA, commands, and log lines -- to a gzipped NDJSON file on the server, so a
real-boat session can be downloaded and replayed to see exactly what happened.

Each NDJSON line is ``{"t": <unix seconds>, "kind": "telemetry|nmea|command|log",
"data": ...}``. Replay feeds the recorded telemetry frames back through the live
telemetry channel at their original cadence, so the existing UI just plays it.
"""

from __future__ import annotations

import gzip
import json
import logging
import os

logger = logging.getLogger("vanchor.debug")


class DebugRecorder:
    def __init__(self, data_dir: str) -> None:
        self.dir = os.path.join(data_dir, "debug")
        self.active = False
        self.path: str | None = None
        self.name: str | None = None
        self._fh = None
        self.counts: dict[str, int] = {}

    def start(self, name: str, now: float) -> dict:
        if self.active:
            return self.status()
        os.makedirs(self.dir, exist_ok=True)
        self.name = name
        self.path = os.path.join(self.dir, name + ".ndjson.gz")
        self._fh = gzip.open(self.path, "wt", encoding="utf-8")
        self.counts = {}
        self.active = True
        self.write("meta", {"name": name, "started": now}, now)
        logger.info("debug recording started: %s", self.path)
        return self.status()

    def write(self, kind: str, data, now: float) -> None:
        if not self.active or self._fh is None:
            return
        try:
            self._fh.write(json.dumps({"t": now, "kind": kind, "data": data}) + "\n")
            self.counts[kind] = self.counts.get(kind, 0) + 1
        except (OSError, TypeError, ValueError):  # pragma: no cover - defensive
            pass

    def stop(self) -> dict:
        if not self.active:
            return {"recording": False}
        self.active = False
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
        logger.info("debug recording stopped: %s (%s)", self.path, self.counts)
        return {"recording": False, "name": self.name, "counts": dict(self.counts)}

    def status(self) -> dict:
        return {
            "recording": self.active,
            "name": self.name if self.active else None,
            "counts": dict(self.counts) if self.active else {},
        }

    def sessions(self) -> list[dict]:
        if not os.path.isdir(self.dir):
            return []
        out = []
        for f in sorted(os.listdir(self.dir), reverse=True):
            if not f.endswith(".ndjson.gz"):
                continue
            try:
                size = os.path.getsize(os.path.join(self.dir, f))
            except OSError:
                size = 0
            out.append({"name": f[: -len(".ndjson.gz")], "file": f, "bytes": size})
        return out

    def path_for(self, file_name: str) -> str | None:
        """Resolve a download path, guarding against traversal."""
        safe = os.path.basename(file_name)
        p = os.path.join(self.dir, safe)
        return p if os.path.isfile(p) else None


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
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("kind") == "telemetry" and isinstance(rec.get("data"), dict):
                        frames.append((float(rec.get("t", 0.0)), rec["data"]))
        except OSError:
            return False
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
