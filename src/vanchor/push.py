"""Web Push notifications (adoption pack #7).

Server-initiated notifications so the boat can raise an alarm on the
operator's phone with the app closed and the phone locked -- the one channel
that still works when NO client is connected (link-loss failsafe, passive
anchor alarm at the dock).

Optional extra: pip install vanchor-ng[push]  (pywebpush + py-vapid).
Without it everything degrades to "unavailable" -- no import errors, the
Settings card explains how to enable it.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .core.config import PushConfig

logger = logging.getLogger("vanchor.push")

# Notification kinds (rate-limited independently). Keep in sync with the
# watcher table in Runtime.evaluate_push_alerts and docs/push-notifications.md.
KINDS = ("anchor_drag", "anchor_alarm", "battery", "depth", "link", "test")

# Max stored subscriptions (one boat, a handful of phones).
_MAX_SUBS = 16

_SENTINEL = object()  # stops the worker thread


def _atomic_write_json(path: Path, data: Any) -> None:
    """Atomic JSON write (tmp + os.replace). Mirrors core/prefs.py."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


class PushService:
    """Subscription store + VAPID keypair + background send worker.

    All public methods are thread-safe (a single ``threading.Lock`` guards the
    store and rate-limit state).  Network I/O is confined to the daemon worker
    thread; ``notify()`` only enqueues and returns immediately.
    """

    def __init__(
        self,
        data_dir: str,
        config: "PushConfig",
        now_fn: Callable[[], float] = time.monotonic,
        transport: Any = None,
    ) -> None:
        self._dir = Path(data_dir) / "push"
        self.config = config
        self._now = now_fn
        self._transport = transport  # requests.Session-compatible test seam

        self._lock = threading.Lock()
        self._subs: list[dict] = []
        self._last_kind_send: dict[str, float] = {}
        self._recent_sends: deque[float] = deque()

        self._worker: threading.Thread | None = None
        self._q: queue.Queue = queue.Queue()
        self._stop_flag = threading.Event()

        self._available: bool | None = None  # cached after first probe
        self._unavailable_reason: str | None = None
        self._pubkey_cache: str | None = None

        self._load_subscriptions()

    # ------------------------------------------------------------------ #
    # Availability
    # ------------------------------------------------------------------ #

    @property
    def available(self) -> bool:
        """True if pywebpush + py_vapid are importable. Result is cached."""
        if self._available is None:
            self._probe_availability()
        return bool(self._available)

    @property
    def unavailable_reason(self) -> str | None:
        """Human-readable explanation when ``available`` is False."""
        if self._available is None:
            self._probe_availability()
        return self._unavailable_reason

    def _probe_availability(self) -> None:
        try:
            import sys
            # A module set to None in sys.modules means "deliberately missing"
            # (monkeypatched in tests).
            if sys.modules.get("pywebpush") is None and "pywebpush" in sys.modules:
                raise ImportError("pywebpush set to None")
            import pywebpush  # noqa: F401
            import py_vapid    # noqa: F401
            self._available = True
            self._unavailable_reason = None
        except ImportError:
            self._available = False
            self._unavailable_reason = (
                "pywebpush not installed "
                "(pip install vanchor-ng[push] to enable Web Push)"
            )

    # ------------------------------------------------------------------ #
    # VAPID keys
    # ------------------------------------------------------------------ #

    @property
    def _key_path(self) -> Path:
        return self._dir / "vapid_private.pem"

    def keys_exist(self) -> bool:
        """True if the VAPID private key file exists (no generation)."""
        return self._key_path.exists()

    def public_key(self) -> str:
        """Return the b64url applicationServerKey, generating it on first call.

        Raises ``RuntimeError`` if pywebpush / py_vapid are not installed.
        """
        if self._pubkey_cache is not None:
            return self._pubkey_cache
        self._pubkey_cache = self._ensure_keys()
        return self._pubkey_cache

    def _ensure_keys(self) -> str:
        """Generate or load the VAPID keypair; return the public key as b64url."""
        if not self.available:
            raise RuntimeError(
                f"push extra not available: {self.unavailable_reason}"
            )
        from py_vapid import Vapid02  # lazy
        from cryptography.hazmat.primitives import serialization  # lazy (bundled with py-vapid)

        self._dir.mkdir(parents=True, exist_ok=True)
        key_path = self._key_path

        if key_path.exists():
            vapid = Vapid02.from_file(str(key_path))
        else:
            vapid = Vapid02()
            vapid.generate_keys()
            vapid.save_key(str(key_path))
            try:
                key_path.chmod(0o600)
            except OSError:
                pass
            logger.info("generated VAPID keypair at %s", key_path)

        raw = vapid.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    # ------------------------------------------------------------------ #
    # Subscription store
    # ------------------------------------------------------------------ #

    def _load_subscriptions(self) -> None:
        """Load subscriptions.json; corrupt/missing -> empty list."""
        path = self._dir / "subscriptions.json"
        try:
            data = json.loads(path.read_text())
            self._subs = list(data.get("subs", []))
        except (OSError, ValueError):
            self._subs = []

    def _save_subscriptions(self) -> None:
        """Persist subscriptions atomically (caller holds lock)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            self._dir / "subscriptions.json",
            {"subs": self._subs},
        )

    def add_subscription(self, sub: dict, ua: str = "") -> dict:
        """Validate + upsert + persist. Returns {ok, count} or {ok, error}."""
        if not self.config.enabled:
            return {"ok": False, "error": "push disabled in config"}
        # Validate.
        if not isinstance(sub, dict):
            return {"ok": False, "error": "malformed subscription"}
        endpoint = sub.get("endpoint", "")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            return {"ok": False, "error": "malformed subscription"}
        keys = sub.get("keys") or {}
        if not isinstance(keys.get("p256dh"), str) or not keys["p256dh"]:
            return {"ok": False, "error": "malformed subscription"}
        if not isinstance(keys.get("auth"), str) or not keys["auth"]:
            return {"ok": False, "error": "malformed subscription"}

        with self._lock:
            # Upsert by endpoint.
            for existing in self._subs:
                if existing.get("endpoint") == endpoint:
                    existing["keys"] = keys
                    existing["ua"] = ua[:200]
                    break
            else:
                entry = {
                    "endpoint": endpoint,
                    "keys": keys,
                    "ua": ua[:200],
                    "created": time.time(),
                }
                self._subs.append(entry)
                # Cap at _MAX_SUBS: drop oldest by created.
                if len(self._subs) > _MAX_SUBS:
                    self._subs.sort(key=lambda s: s.get("created", 0.0))
                    self._subs = self._subs[-_MAX_SUBS:]
            self._save_subscriptions()
            return {"ok": True, "count": len(self._subs)}

    def remove_subscription(self, endpoint: str) -> bool:
        """Remove a subscription by endpoint. Returns True if it was present."""
        with self._lock:
            before = len(self._subs)
            self._subs = [s for s in self._subs if s.get("endpoint") != endpoint]
            removed = len(self._subs) < before
            if removed:
                self._save_subscriptions()
            return removed

    def subscription_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def status(self) -> dict:
        """Summary dict for GET /api/push/status — never generates keys, never raises."""
        return {
            "ok": True,
            "available": self.available,
            "reason": self.unavailable_reason,
            "enabled": self.config.enabled,
            "keys_exist": self.keys_exist(),
            "subscriptions": self.subscription_count(),
        }

    # ------------------------------------------------------------------ #
    # Rate limiting / notify
    # ------------------------------------------------------------------ #

    def notify(
        self,
        kind: str,
        title: str,
        body: str,
        tag: str | None = None,
        url: str = "/",
    ) -> bool:
        """Rate-limited enqueue. Returns True if enqueued. NON-BLOCKING."""
        if not self.config.enabled:
            logger.debug("push: notify skipped — disabled in config")
            return False
        if not self.available:
            logger.debug("push: notify skipped — unavailable (%s)", self.unavailable_reason)
            return False
        with self._lock:
            if not self._subs:
                logger.debug("push: notify skipped — no subscriptions")
                return False
            now = self._now()
            # Per-kind interval floor.
            last = self._last_kind_send.get(kind, float("-inf"))
            if now - last < self.config.min_interval_s:
                logger.debug("push: notify skipped — per-kind interval for %s", kind)
                return False
            # Global burst cap.
            window_start = now - self.config.burst_window_s
            while self._recent_sends and self._recent_sends[0] < window_start:
                self._recent_sends.popleft()
            if len(self._recent_sends) >= self.config.burst_limit:
                logger.debug("push: notify skipped — burst cap reached")
                return False
            # Accept: record timers immediately (before delivery).
            self._last_kind_send[kind] = now
            self._recent_sends.append(now)

        if kind not in KINDS:
            logger.debug("push: unknown kind %r (forward-compat; enqueuing anyway)", kind)

        payload = {
            "title": title,
            "body": body,
            "kind": kind,
            "tag": tag if tag is not None else f"vanchor-{kind}",
            "url": url,
            "ts": time.time(),
        }
        self._enqueue(payload)
        return True

    def _enqueue(self, payload: dict) -> None:
        """Enqueue a payload; start the worker thread lazily."""
        if self._worker is None or not self._worker.is_alive():
            self._stop_flag.clear()
            self._worker = threading.Thread(
                target=self._run_worker,
                name="push-worker",
                daemon=True,
            )
            self._worker.start()
        self._q.put(payload)

    # ------------------------------------------------------------------ #
    # Worker thread
    # ------------------------------------------------------------------ #

    def _run_worker(self) -> None:
        """Background daemon: drain the queue and send each payload."""
        while not self._stop_flag.is_set():
            try:
                payload = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if payload is _SENTINEL:
                break
            with self._lock:
                subs = list(self._subs)
            for sub in subs:
                try:
                    ok, err = self._send_one(sub, payload)
                    if not ok:
                        logger.warning("push: send failed for %s: %s",
                                       sub.get("endpoint", "?")[:60], err)
                except Exception:  # noqa: BLE001
                    logger.exception("push: unexpected error in worker")

    def _send_one(self, sub: dict, payload: dict) -> tuple[bool, str | None]:
        """Send one push notification. Returns (success, error_str|None)."""
        from pywebpush import webpush, WebPushException  # lazy
        try:
            webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]},
                data=json.dumps(payload),
                vapid_private_key=str(self._key_path),
                vapid_claims={"sub": self.config.subject},
                ttl=int(self.config.ttl_s),
                timeout=self.config.timeout_s,
                requests_session=self._transport,  # None -> library default
            )
            return True, None
        except WebPushException as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code in (404, 410):
                self.remove_subscription(sub["endpoint"])
                return False, f"pruned expired subscription ({code})"
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - a send must never kill the worker
            return False, str(exc)

    # ------------------------------------------------------------------ #
    # send_now (synchronous — for the Test button)
    # ------------------------------------------------------------------ #

    def send_now(
        self,
        kind: str,
        title: str,
        body: str,
        tag: str | None = None,
        url: str = "/",
    ) -> dict:
        """Synchronous send bypassing rate limits. BLOCKING network I/O.

        Returns {"ok": bool, "sent": n, "failed": n, "errors": [str, ...]}.
        Caller must wrap in asyncio.to_thread.
        """
        if not self.available:
            return {"ok": False, "sent": 0, "failed": 0,
                    "errors": [self.unavailable_reason or "unavailable"]}
        if not self.config.enabled:
            return {"ok": False, "sent": 0, "failed": 0,
                    "errors": ["push disabled in config"]}
        with self._lock:
            subs = list(self._subs)
        if not subs:
            return {"ok": False, "sent": 0, "failed": 0, "errors": ["no subscriptions"]}

        payload = {
            "title": title,
            "body": body,
            "kind": kind,
            "tag": tag if tag is not None else f"vanchor-{kind}",
            "url": url,
            "ts": time.time(),
        }
        sent = 0
        failed = 0
        errors: list[str] = []
        for sub in subs:
            try:
                ok, err = self._send_one(sub, payload)
                if ok:
                    sent += 1
                else:
                    failed += 1
                    if err:
                        errors.append(err)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(str(exc))

        return {"ok": sent > 0, "sent": sent, "failed": failed, "errors": errors}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def stop(self) -> None:
        """Signal + join the worker thread (2 s timeout). Idempotent."""
        self._stop_flag.set()
        self._q.put(_SENTINEL)
        w = self._worker
        if w is not None and w.is_alive():
            w.join(timeout=2.0)
