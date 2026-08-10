"""Generic online-fetch relay: fetch online data *through a connected client*
when the boat's Pi has no internet of its own.

The Pi is usually offline on the water, but the phone/tablet controlling it has
internet (cellular). So any server code that needs an online resource -- the
smart router's OpenStreetMap/Overpass water polygons, map tiles, a WMM update,
... -- calls :meth:`FetchRelay.fetch` instead of hitting the network directly.

Strategy (per the design):

* **Try the server directly first**, with a short timeout -- fast when the Pi
  *does* have internet (e.g. at the dock).
* On a direct failure, flip a **sticky "offline" circuit breaker** so this and
  the following fetches (e.g. every tile of one route) go **straight to the
  relay** instead of each re-trying-and-timing-out the direct path.
* **Relay** the request to connected clients over the telemetry WebSocket; the
  first client to answer wins. The client checks its own cache first and only
  hits the internet on a miss (that logic lives in the browser).

This module is transport-agnostic and fully unit-testable: the WebSocket
broadcast, the "is a client connected?" check, the direct fetch, the clock and
the id generator are all injected.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger("vanchor.fetch_relay")


class FetchRelayError(RuntimeError):
    """An online fetch could not be completed -- directly *or* via a client.

    Carries a human-readable message suitable for surfacing to the operator
    (the whole point: these failures must never be silent)."""


class FetchRelayTargetError(FetchRelayError):
    """The relay pipeline WORKED -- a client answered -- but the *target*
    failed (e.g. Overpass replied HTTP 504 under load). Distinct from the base
    class so callers with alternative targets (the second Overpass endpoint)
    know retrying a DIFFERENT url through the same relay is worthwhile, whereas
    a base FetchRelayError (no client / no answer) makes any retry pointless."""


@dataclass
class _Pending:
    future: "asyncio.Future[bytes]"
    url: str


class FetchRelay:
    def __init__(
        self,
        *,
        broadcast: Callable[[dict], Awaitable[None]],
        has_clients: Callable[[], bool],
        direct_fetch: Callable[..., Awaitable[bytes]] | None = None,
        clock: Callable[[], float] = None,  # type: ignore[assignment]
        new_id: Callable[[], str] = None,   # type: ignore[assignment]
        offline_ttl_s: float = 60.0,
        relay_timeout_s: float = 30.0,
        direct_timeout_s: float = 4.0,
    ) -> None:
        self._broadcast = broadcast
        self._has_clients = has_clients
        self._direct = direct_fetch or _default_direct_fetch
        self._clock = clock or _monotonic
        self._new_id = new_id or _rand_id
        self._offline_ttl_s = offline_ttl_s
        self._relay_timeout_s = relay_timeout_s
        self._direct_timeout_s = direct_timeout_s
        self._pending: dict[str, _Pending] = {}
        # Identical concurrent requests coalesce onto one relayed fetch:
        # (url, method, body) -> the pending entry whose future they share.
        self._inflight: dict[tuple, _Pending] = {}
        self._offline_until: float = 0.0  # sticky-offline circuit-breaker deadline
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the event loop so :meth:`fetch_sync` can be called from
        worker threads (the route planner runs in an executor)."""
        self._loop = loop

    # -- state (for telemetry / UI) -------------------------------------- #
    @property
    def offline(self) -> bool:
        """True while the sticky circuit breaker is engaged (relaying, not
        trying direct)."""
        return self._clock() < self._offline_until

    def _trip_offline(self) -> None:
        self._offline_until = self._clock() + self._offline_ttl_s

    # -- the public fetch ------------------------------------------------- #
    async def fetch(self, url: str, *, method: str = "GET",
                    headers: dict | None = None, body: bytes | None = None,
                    timeout: float | None = None,
                    direct_timeout: float | None = None) -> bytes:
        """Return the resource bytes, via a direct server fetch or (if offline)
        by relaying through a connected client. Raises :class:`FetchRelayError`
        with a clear message when neither can satisfy it.

        ``direct_timeout`` overrides the short default for sources that are
        legitimately slow even when online (an Overpass query can take tens of
        seconds server-side). An offline Pi usually fails FAST regardless (no
        default route -> immediate "network unreachable"), so a longer direct
        timeout doesn't delay the offline fallback in practice."""
        if not self.offline:
            dt = direct_timeout if direct_timeout is not None else self._direct_timeout_s
            try:
                return await asyncio.wait_for(
                    self._direct(url, method=method, headers=headers, body=body,
                                 timeout=dt),
                    timeout=dt + 1.0)
            except Exception as exc:  # noqa: BLE001 - any direct failure -> go offline
                logger.info("direct fetch of %s failed (%s); switching to client relay",
                            url, exc)
                self._trip_offline()
        return await self._relay(url, method=method, headers=headers,
                                 body=body, timeout=timeout)

    def fetch_sync(self, url: str, *, method: str = "GET",
                   headers: dict | None = None, body: bytes | None = None,
                   timeout: float | None = None,
                   direct_timeout: float | None = None) -> bytes:
        """Blocking :meth:`fetch` for WORKER THREADS (e.g. the route planner in
        its executor). Must never be called from the event-loop thread."""
        if self._loop is None:
            raise FetchRelayError("fetch relay not started (no event loop bound)")
        fut = asyncio.run_coroutine_threadsafe(
            self.fetch(url, method=method, headers=headers, body=body,
                       timeout=timeout, direct_timeout=direct_timeout),
            self._loop)
        outer = (timeout or self._relay_timeout_s) + (direct_timeout or self._direct_timeout_s) + 10.0
        return fut.result(timeout=outer)

    async def _relay(self, url: str, *, method: str, headers: dict | None,
                     body: bytes | None, timeout: float | None) -> bytes:
        if not self._has_clients():
            raise FetchRelayError(
                "No internet on the boat and no connected device to fetch "
                f"through (needed {url}).")
        # Coalesce identical in-flight requests: concurrent callers wanting the
        # SAME resource (a viewport burst, parallel tile loops) share ONE relayed
        # request instead of stampeding the client and the target (429s).
        key = (url, method, body)
        existing = self._inflight.get(key)
        if existing is not None and not existing.future.done():
            # shield: a secondary awaiter timing out must not cancel the shared
            # future out from under the primary (or other) awaiters.
            return await asyncio.wait_for(asyncio.shield(existing.future),
                                          timeout or self._relay_timeout_s)
        rid = self._new_id()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[bytes] = loop.create_future()
        pending = _Pending(fut, url)
        self._pending[rid] = pending
        self._inflight[key] = pending
        msg: dict[str, Any] = {"type": "fetch_request", "id": rid, "url": url,
                               "method": method}
        if headers:
            msg["headers"] = headers
        if body is not None:
            msg["body_b64"] = base64.b64encode(body).decode("ascii")
        try:
            await self._broadcast(msg)
            return await asyncio.wait_for(fut, timeout or self._relay_timeout_s)
        except asyncio.TimeoutError as exc:
            raise FetchRelayError(
                f"No connected device answered the fetch for {url} in time.") from exc
        finally:
            self._pending.pop(rid, None)
            if self._inflight.get(key) is pending:
                self._inflight.pop(key, None)

    # -- called by the /api/relay result endpoint ------------------------ #
    def resolve(self, request_id: str, *, ok: bool, data: bytes | None = None,
                error: str | None = None) -> bool:
        """Complete a pending relay request with the client's result. Returns
        True if it matched a live pending request (a late/duplicate answer from a
        second client returns False and is ignored)."""
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        if ok and data is not None:
            pending.future.set_result(data)
        else:
            # The client DID answer -- the target itself failed (504, DNS, ...).
            # TargetError so callers with alternative targets can fail over.
            pending.future.set_exception(FetchRelayTargetError(
                error or f"The connected device could not fetch {pending.url}."))
        return True


# --------------------------------------------------------------------------- #
# Defaults (kept out of __init__ so tests inject fakes)
# --------------------------------------------------------------------------- #
def _monotonic() -> float:
    import time
    return time.monotonic()


def _rand_id() -> str:
    import secrets
    return secrets.token_hex(8)


async def _default_direct_fetch(url: str, *, method: str = "GET",
                                headers: dict | None = None,
                                body: bytes | None = None,
                                timeout: float = 10.0) -> bytes:
    """Blocking HTTP via requests, run in a thread so it never blocks the loop."""
    def _do() -> bytes:
        import requests
        resp = requests.request(method, url, headers=headers, data=body,
                                timeout=timeout)
        resp.raise_for_status()
        return resp.content
    return await asyncio.to_thread(_do)


def relay_http_post(relay: "FetchRelay | None", *, direct_timeout: float = 25.0,
                    timeout: float = 90.0):
    """An ``http_post(url, body, headers) -> bytes`` adapter for SYNC code
    (``water.fetch_overpass``'s injectable seam), or ``None`` when no relay
    exists so callers keep their direct default."""
    if relay is None:
        return None

    def _post(url: str, body: bytes, headers: dict) -> bytes:
        return relay.fetch_sync(url, method="POST", headers=headers, body=body,
                                timeout=timeout, direct_timeout=direct_timeout)
    return _post
