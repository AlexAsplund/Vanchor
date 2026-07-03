"""A tiny async publish/subscribe event bus.

The whole system is wired through this bus so components stay decoupled: a
sensor publishes ``nmea.in`` without knowing the navigator exists; the UI
publishes ``command.set_mode`` without knowing the controller exists.

Handlers may be plain functions or coroutine functions. Exceptions in one
handler are logged and never break delivery to the others.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

logger = logging.getLogger("vanchor.events")

Handler = Callable[[Any], Awaitable[None] | None]

# Common topic names, gathered here for discoverability.
NMEA_IN = "nmea.in"  # a raw NMEA sentence arrived from a sensor
IMU_IN = "imu.in"  # a raw ImuSample (accel+gyro) arrived from an AHRS device
NAV_FIX = "nav.fix"  # navigator produced a fresh GpsFix
NAV_HEADING = "nav.heading"  # navigator produced a fresh heading
NAV_APB = "nav.apb"  # navigator parsed an APB autopilot sentence
MOTOR_COMMAND = "motor.command"  # controller emitted a MotorCommand
TELEMETRY = "telemetry"  # periodic snapshot for the UI


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._wildcard: list[Callable[[str, Any], Awaitable[None] | None]] = []

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._handlers[topic].append(handler)

    def subscribe_all(
        self, handler: Callable[[str, Any], Awaitable[None] | None]
    ) -> None:
        """Receive ``(topic, payload)`` for every published event (for logging
        and observability)."""
        self._wildcard.append(handler)

    async def publish(self, topic: str, payload: Any = None) -> None:
        for handler in list(self._handlers.get(topic, ())):
            await self._invoke(handler, payload)
        # mypy narrows ``handler`` to ``Handler`` from the loop above; the
        # wildcard handlers have a different (topic, payload) signature.
        for handler in list(self._wildcard):  # type: ignore[assignment]
            await self._invoke_wild(handler, topic, payload)  # type: ignore[arg-type]

    @staticmethod
    async def _invoke(handler: Handler, payload: Any) -> None:
        try:
            result = handler(payload)
            if inspect.isawaitable(result):
                await result
        except Exception:  # pragma: no cover - defensive
            logger.exception("event handler %r failed", handler)

    @staticmethod
    async def _invoke_wild(
        handler: Callable[[str, Any], Awaitable[None] | None], topic: str, payload: Any
    ) -> None:
        try:
            result = handler(topic, payload)
            if inspect.isawaitable(result):
                await result
        except Exception:  # pragma: no cover - defensive
            logger.exception("wildcard handler %r failed", handler)


def run_soon(coro: Awaitable[None]) -> "asyncio.Task[None]":
    """Schedule a coroutine on the running loop, keeping a reference so it is
    not garbage-collected mid-flight."""
    task = asyncio.ensure_future(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


_BACKGROUND: set["asyncio.Task[None]"] = set()
