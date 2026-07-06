"""Split-motor composite: drive steering and thrust as two independent channels.

The controller keeps its single seam — it hands an atomic
:class:`~vanchor.core.models.MotorCommand` to ``Controller.motor`` and never knows
whether that motor is one combined board or two separate ones. :class:`SplitMotor`
lives BELOW that seam: it fans the command's two fields out to two
:class:`MotorChannel` objects, guarding each so a fault on one channel can never
block, crash, or stall the other (Constraint 4 of the motor-split plan).

A "combined" rig (the default: both channels on one Arduino speaking one ``CMD``
frame) does NOT use this — it stays exactly today's single controller. This module
is only for genuinely split hardware (e.g. a modified Minn Kota head with its own
steering servo plus an independent thrust ESC).
"""

from __future__ import annotations

import abc
import logging

from ..core.models import MotorCommand
from .interfaces import MotorController

logger = logging.getLogger(__name__)


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


class MotorChannel(abc.ABC):
    """One actuation channel of a split motor (steering OR thrust).

    Sibling to :class:`~vanchor.hardware.interfaces.MotorController` but for a
    single normalized axis. ``set_normalized`` records the latest command (cheap,
    non-blocking, clamped to [-1, 1]); ``flush`` does any async I/O. Lifecycle and
    ``debug`` mirror the motor/sensor conventions and must never raise from
    ``debug``.
    """

    @abc.abstractmethod
    def set_normalized(self, value: float) -> None:
        """Record/translate the latest normalized command in [-1, 1]."""

    async def flush(self) -> None:  # pragma: no cover - trivial default
        """Push the latest command to the device (no-op by default)."""
        return None

    async def start(self) -> None:  # pragma: no cover - trivial default
        return None

    async def stop(self) -> None:  # pragma: no cover - trivial default
        return None

    def debug(self) -> str:  # pragma: no cover - overridden per device
        """Human-readable snapshot of this channel; must never raise."""
        return f"{type(self).__name__}: no debug data available"

    @property
    def healthy(self) -> bool | None:  # pragma: no cover - trivial default
        """Whether the channel link is up. ``None`` = unknown/not-applicable."""
        return True


class SplitMotor(MotorController):
    """A :class:`MotorController` that routes each command field to its own channel.

    ``apply`` sends ``cmd.thrust`` to the thrust channel and ``cmd.steering`` to the
    steering channel; a ``None`` channel is simply skipped ("not connected"). Every
    per-channel call is guarded: an exception from one channel is logged and never
    prevents the other channel from being serviced — so a STOP still reaches the
    working channel even if its neighbour is faulted (Constraint 4).
    """

    def __init__(
        self,
        thrust: MotorChannel | None,
        steering: MotorChannel | None,
    ) -> None:
        self.thrust = thrust
        self.steering = steering

    def apply(self, command: MotorCommand) -> None:
        # Route each field independently and guarded. A STOP-shaped command (0, 0)
        # therefore zeroes BOTH channels even if one raises.
        self._guard_sync(self.thrust, "thrust", command.thrust)
        self._guard_sync(self.steering, "steering", command.steering)

    def _guard_sync(self, channel: MotorChannel | None, name: str, value: float) -> None:
        if channel is None:
            return  # not connected — skip
        try:
            channel.set_normalized(_clamp(value))
        except Exception:  # noqa: BLE001
            logger.exception("split motor: %s channel set_normalized failed", name)

    async def flush(self) -> None:
        await self._guard_async("flush", "thrust", self.thrust)
        await self._guard_async("flush", "steering", self.steering)

    async def start(self) -> None:
        await self._guard_async("start", "thrust", self.thrust)
        await self._guard_async("start", "steering", self.steering)

    async def stop(self) -> None:
        await self._guard_async("stop", "thrust", self.thrust)
        await self._guard_async("stop", "steering", self.steering)

    async def _guard_async(
        self, method: str, name: str, channel: MotorChannel | None
    ) -> None:
        if channel is None:
            return
        try:
            await getattr(channel, method)()
        except Exception:  # noqa: BLE001
            logger.exception("split motor: %s channel %s() failed", name, method)

    def debug(self) -> str:
        """Compose both channels' debug snapshots; never raises."""
        return (
            f"SplitMotor\n"
            f"  thrust  : {self._channel_debug(self.thrust)}\n"
            f"  steering: {self._channel_debug(self.steering)}"
        )

    @staticmethod
    def _channel_debug(channel: MotorChannel | None) -> str:
        if channel is None:
            return "not connected"
        try:
            return channel.debug()
        except Exception as exc:  # noqa: BLE001
            return f"debug error ({exc})"
