"""Always-on black-box flight recorder (roadmap #20).

A lightweight, bounded ring buffer that samples a low-rate snapshot of the
control loop and, on ANY alarm transition, dumps its pre-trigger history plus a
short post-trigger tail to a timestamped gzip file -- so incidents are captured
even when the opt-in debug recorder isn't running.

Design constraints (this rides the SAFETY-critical control tick):

* The per-tick hot path is O(1) and allocation-light: it computes an integer
  alarm bitmask (no allocation) and compares it to the previous tick's mask to
  detect a *rising* edge. A snapshot dict is built and appended to the ring only
  when a low-rate sample is due, or when an alarm trips (and during its tail).
* Every dump is written OFF the event loop (``asyncio.to_thread``) so gzip
  compression never blocks the loop. With no running loop (unit tests / off the
  live path) the write happens inline so a dump still lands on disk.
* ``observe`` NEVER raises into the caller: the whole body is guarded so a
  black-box bug can't take down the control loop.

Each dump file is ``<data_dir>/blackbox/blackbox-<YYYYmmdd-HHMMSS>-<reason>.json.gz``
containing ``{"meta": {...}, "frames": [...]}`` (frames oldest-first).

The recorder captures BOTH the command the controller asked for (``desired``) and
what the safety governor actually applied (``applied``) -- the two are visible
together only at the governor boundary, which is where :meth:`observe` is wired.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import time
from collections import deque

logger = logging.getLogger("vanchor.blackbox")

_SUFFIX = ".json.gz"

# The alarm flags on ``SafetyStatus`` that count as an INCIDENT worth dumping.
# Routine, high-frequency governor actions (thrust/steer slew-limiting, the
# reverse-interlock cooldown) are deliberately EXCLUDED so a normal reversal or
# a slew clamp never spams a dump -- those are still recorded inside each frame
# (see ``limited``) for diagnostics, they just don't *trigger* one.
_STATUS_ALARMS: tuple[str, ...] = (
    "drag_alarm",
    "fix_lost",
    "shallow_stop",
    "nogo_stop",
    "heading_stale",
)
# Extra alarm sources the caller passes in as booleans (they don't live on the
# governor's per-tick status): a controller-loop fault and the lost-link
# failsafe. Appended AFTER the status alarms so bit positions stay stable.
_EXTRA_ALARMS: tuple[str, ...] = ("controller_fault", "link_failsafe")

_ALL_ALARMS: tuple[str, ...] = _STATUS_ALARMS + _EXTRA_ALARMS


class BlackBox:
    """Bounded ring recorder with pre-trigger dump on any alarm transition.

    ``capacity`` frames are retained (a fixed-size deque). ``sample_period_s`` is
    the low-rate cadence between routine samples. On an alarm rising edge the
    dump is deferred until ``post_trigger_frames`` further ticks have been
    captured (at the full tick rate), so the file holds a short tail past the
    event; ``post_trigger_frames == 0`` dumps immediately.
    """

    def __init__(
        self,
        data_dir: str,
        *,
        enabled: bool = True,
        capacity: int = 256,
        sample_period_s: float = 1.0,
        post_trigger_frames: int = 0,
        now_fn=time.time,
        dump_fn=None,
    ) -> None:
        self.enabled = bool(enabled)
        self.dir = os.path.join(data_dir, "blackbox")
        self._capacity = max(1, int(capacity))
        self._ring: deque[dict] = deque(maxlen=self._capacity)
        self._sample_period_s = max(0.0, float(sample_period_s))
        self._post_trigger_frames = max(0, int(post_trigger_frames))
        self._now_fn = now_fn
        # Injectable writer seam (tests). Default schedules an off-loop gzip write.
        self._dump_fn = dump_fn or self._schedule_write

        self._prev_mask = 0
        self._last_sample = float("-inf")
        # None when not in a post-trigger window; else frames still to capture.
        self._post_remaining: int | None = None
        self._trigger_meta: dict | None = None
        # Keep references to in-flight off-loop write tasks so they aren't GC'd.
        self._dump_tasks: set = set()

    # ------------------------------------------------------------------ #
    # Hot path (called every control tick from the governor wrapper)
    # ------------------------------------------------------------------ #
    def observe(
        self,
        desired,
        applied,
        status,
        state,
        *,
        controller_fault: bool = False,
        link_failsafe: bool = False,
    ) -> None:
        """Record one control tick. Cheap by default; never raises.

        ``desired`` is the pre-governor motor command; ``applied`` is what the
        governor let through. ``status`` is the governor's :class:`SafetyStatus`;
        ``controller_fault`` / ``link_failsafe`` are the two alarm sources that
        don't live on it.
        """
        if not self.enabled:
            return
        try:
            self._observe(desired, applied, status, state,
                          controller_fault, link_failsafe)
        except Exception:  # noqa: BLE001 - a black-box bug must not break control
            logger.debug("blackbox observe failed; continuing", exc_info=True)

    def _observe(self, desired, applied, status, state,
                 controller_fault, link_failsafe) -> None:
        now = self._now_fn()
        mask = self._alarm_mask(status, controller_fault, link_failsafe)
        rising = mask & ~self._prev_mask
        self._prev_mask = mask

        # Sample when: an alarm just rose, we're inside a post-trigger tail, or
        # the low-rate cadence is due. Everything above is integer math -- no
        # allocation happens until we actually build+append a frame here.
        sample_due = (now - self._last_sample) >= self._sample_period_s
        if rising or self._post_remaining is not None or sample_due:
            self._ring.append(
                self._build_frame(now, desired, applied, status, state, mask)
            )
            self._last_sample = now

        if rising:
            # (Re)arm the post-trigger window; dump now if no tail is wanted.
            self._arm_trigger(now, rising, mask)
            if self._post_trigger_frames <= 0:
                self._flush_dump()
        elif self._post_remaining is not None:
            self._post_remaining -= 1
            if self._post_remaining <= 0:
                self._flush_dump()

    # ------------------------------------------------------------------ #
    # Alarm bookkeeping
    # ------------------------------------------------------------------ #
    @staticmethod
    def _alarm_mask(status, controller_fault: bool, link_failsafe: bool) -> int:
        mask = 0
        for i, key in enumerate(_STATUS_ALARMS):
            if getattr(status, key, False):
                mask |= 1 << i
        if controller_fault:
            mask |= 1 << len(_STATUS_ALARMS)
        if link_failsafe:
            mask |= 1 << (len(_STATUS_ALARMS) + 1)
        return mask

    @staticmethod
    def _alarm_names(mask: int) -> list[str]:
        return [name for i, name in enumerate(_ALL_ALARMS) if mask & (1 << i)]

    def _arm_trigger(self, now: float, rising: int, mask: int) -> None:
        """Start (or extend) the post-trigger window and record why it fired."""
        new_names = self._alarm_names(rising)
        if self._trigger_meta is None:
            self._trigger_meta = {
                "triggered_at": round(now, 3),
                "alarms": new_names,
                "active_alarms": self._alarm_names(mask),
            }
        else:
            # A second alarm tripped while the tail was still running: fold its
            # names in rather than starting an overlapping dump.
            for n in new_names:
                if n not in self._trigger_meta["alarms"]:
                    self._trigger_meta["alarms"].append(n)
            self._trigger_meta["active_alarms"] = self._alarm_names(mask)
        self._post_remaining = self._post_trigger_frames

    # ------------------------------------------------------------------ #
    # Frame construction
    # ------------------------------------------------------------------ #
    def _build_frame(self, now, desired, applied, status, state, mask) -> dict:
        pos = getattr(state, "position", None)
        mode = getattr(state, "mode", None)
        return {
            "t": round(now, 3),
            "mode": getattr(mode, "value", mode),
            "lat": round(pos.lat, 7) if pos is not None else None,
            "lon": round(pos.lon, 7) if pos is not None else None,
            "heading_deg": round(getattr(state, "heading_deg", 0.0), 2),
            "sog_knots": round(getattr(state, "sog_knots", 0.0), 2),
            "dist_anchor_m": round(getattr(state, "distance_to_anchor_m", 0.0), 2),
            # DESIRED (what the controller asked for) vs APPLIED (what the safety
            # governor let through) -- the whole point of the recorder.
            "desired": {
                "thrust": round(desired.thrust, 4),
                "steering": round(desired.steering, 4),
            },
            "applied": {
                "thrust": round(applied.thrust, 4),
                "steering": round(applied.steering, 4),
            },
            # Routine clamps recorded per-frame (they don't trigger a dump).
            "limited": {
                "thrust": bool(getattr(status, "thrust_limited", False)),
                "steer": bool(getattr(status, "steer_limited", False)),
                "reverse_blocked": bool(getattr(status, "reverse_blocked", False)),
            },
            "alarms": self._alarm_names(mask),
        }

    # ------------------------------------------------------------------ #
    # Dump
    # ------------------------------------------------------------------ #
    def _flush_dump(self) -> None:
        """Snapshot the ring and hand it to the writer, then disarm the window."""
        frames = list(self._ring)
        meta = self._trigger_meta or {"triggered_at": round(self._now_fn(), 3),
                                      "alarms": [], "active_alarms": []}
        meta = {**meta, "frame_count": len(frames)}
        self._post_remaining = None
        self._trigger_meta = None
        path = self._dump_path(meta)
        try:
            self._dump_fn(path, frames, meta)
        except Exception:  # noqa: BLE001 - a failed dump must not break control
            logger.exception("blackbox dump scheduling failed")

    def _dump_path(self, meta: dict) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(meta.get("triggered_at", self._now_fn())))
        alarms = meta.get("alarms") or []
        reason = alarms[0] if alarms else "alarm"
        return os.path.join(self.dir, f"blackbox-{stamp}-{reason}{_SUFFIX}")

    def _schedule_write(self, path: str, frames: list, meta: dict):
        """Default writer: run the gzip write off the event loop. With no running
        loop (tests / off the live path) write inline so a file still lands."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_dump(path, frames, meta)
            return None
        task = loop.create_task(asyncio.to_thread(self._write_dump, path, frames, meta))
        self._dump_tasks.add(task)
        task.add_done_callback(self._dump_tasks.discard)
        return task

    def _write_dump(self, path: str, frames: list, meta: dict) -> None:
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = path + ".tmp"
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                json.dump({"meta": meta, "frames": frames}, fh)
            os.replace(tmp, path)
            logger.warning(
                "black-box dump written: %s (%d frames, alarms=%s)",
                path, len(frames), meta.get("alarms"),
            )
        except Exception:  # noqa: BLE001
            logger.exception("black-box dump write failed")

    # ------------------------------------------------------------------ #
    # Read API (for the UI: list + download)
    # ------------------------------------------------------------------ #
    def frames(self) -> list[dict]:
        """A copy of the current ring contents (oldest first)."""
        return list(self._ring)

    def dumps(self) -> list[dict]:
        """List the dump files on disk, newest first: ``[{file, size}, ...]``."""
        try:
            names = os.listdir(self.dir)
        except OSError:
            return []
        out: list[dict] = []
        for name in sorted(names, reverse=True):
            if not name.endswith(_SUFFIX):
                continue
            try:
                size = os.path.getsize(os.path.join(self.dir, name))
            except OSError:
                continue
            out.append({"file": name, "size": size})
        return out

    def path_for(self, name: str) -> str | None:
        """Resolve a dump file name to a full path, refusing path traversal and
        anything outside the black-box directory. ``None`` if it isn't a real
        dump file."""
        safe = os.path.basename(str(name))
        if not safe.endswith(_SUFFIX):
            return None
        path = os.path.join(self.dir, safe)
        return path if os.path.isfile(path) else None
