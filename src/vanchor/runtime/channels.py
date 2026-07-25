"""Motor-channel adapters used by the Runtime device builder.

Provides the neutral-hold wrapper, the fan-out tee, and the sim split-channel
pair, plus the low-level motor lifecycle helpers (_start_motor, _stop_motor).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("vanchor.app")


async def _start_motor(motor) -> None:
    """Open a motor controller's lifecycle if it has one.

    The real ``SerialMotorController`` opens its transport (and starts the
    feedback reader) in ``start()``; without this its first ``flush()`` raises
    on a never-opened port. The sim motor (and a bare ``_TeeMotor``) has no
    ``start`` and is a no-op. Raises on failure so callers can roll back."""
    start = getattr(motor, "start", None)
    if start is None:
        return
    res = start()
    if hasattr(res, "__await__"):
        await res


async def _stop_motor(motor) -> None:
    """Best-effort stop of a motor controller (sends the shutdown CMD 0 and
    closes the port on the serial controller). Swallows errors -- a shutdown /
    device-swap must never be blocked by a motor that won't close cleanly. A
    motor with no ``stop`` (sim motor) is a no-op."""
    stop = getattr(motor, "stop", None)
    if stop is None:
        return
    try:
        res = stop()
        if hasattr(res, "__await__"):
            await res
    except Exception:  # noqa: BLE001 - shutdown/swap must not be blocked
        logger.debug("motor stop failed (best-effort)")


class _NeutralChannelMotor:
    """Hold a disabled channel at neutral (0.0) before delegating to the inner
    motor controller.

    Used for combined-plan configs where one channel source is ``"none"`` while
    the other rides the shared serial/sim board.  The combined controller still
    transmits both ``thrust`` and ``steering`` fields in every frame; this adapter
    ensures the disabled field is always 0.0 regardless of what the control loop
    computes, honouring the docstring promise in
    :func:`~vanchor.hardware.link_plan.plan_motor_links`.

    Duck-typed to the ``MotorController`` interface.
    """

    def __init__(self, inner, neutral_channel: str) -> None:
        self._inner = inner
        self._neutral = neutral_channel  # "steering" or "thrust"

    def apply(self, command) -> None:
        import dataclasses
        command = dataclasses.replace(command, **{self._neutral: 0.0})
        self._inner.apply(command)

    async def flush(self) -> None:
        flush = getattr(self._inner, "flush", None)
        if flush is None:
            return
        res = flush()
        if hasattr(res, "__await__"):
            await res

    async def start(self) -> None:
        await _start_motor(self._inner)

    async def stop(self) -> None:
        await _stop_motor(self._inner)

    def debug(self) -> str:
        try:
            inner_dbg = self._inner.debug()
        except Exception:  # noqa: BLE001
            inner_dbg = repr(self._inner)
        return f"NeutralChannel({self._neutral}=0) -> {inner_dbg}"


class _TeeMotor:
    """Fan one ``MotorCommand`` out to several motor controllers at once — e.g.
    drive the simulated boat AND a real steering servo for bench testing.
    Duck-typed to the ``MotorController`` interface (sync ``apply`` + ``flush``,
    which may be sync or async)."""

    def __init__(self, motors) -> None:
        self._motors = [m for m in motors if m is not None]

    def apply(self, command) -> None:
        for m in self._motors:
            m.apply(command)

    async def flush(self) -> None:
        for m in self._motors:
            flush = getattr(m, "flush", None)
            if flush is None:
                continue
            res = flush()
            if hasattr(res, "__await__"):
                await res

    async def start(self) -> None:
        # Open every inner motor that has a lifecycle (e.g. the real serial
        # controller opens its port + feedback task here). The sim motor has no
        # start() and is skipped.
        for m in self._motors:
            await _start_motor(m)

    async def stop(self) -> None:
        # Best-effort stop of every inner motor (sends CMD 0 + closes the port on
        # the serial controller). Never let one failure block the others.
        for m in self._motors:
            await _stop_motor(m)


class _SimChannelState:
    """Shared mutable state for a pair of sim split-motor channel adapters.

    Both :class:`_SimThrustChannel` and :class:`_SimSteeringChannel` hold a
    reference to the same state object so that either channel's flush can
    reconstruct the full :class:`~vanchor.core.models.MotorCommand` that the
    :class:`~vanchor.sim.devices.SimMotorController` expects.
    """

    __slots__ = ("thrust", "steering")

    def __init__(self) -> None:
        self.thrust: float = 0.0
        self.steering: float = 0.0


class _SimThrustChannel:
    """A split :class:`~vanchor.hardware.split_motor.MotorChannel` that drives
    the thrust axis of a :class:`~vanchor.sim.devices.SimMotorController`.

    ``set_normalized`` records the commanded thrust; ``flush`` applies the
    combined (thrust + steering) command to the underlying sim motor so the
    physics simulation sees the correct full command. Shares its
    :class:`_SimChannelState` with a sibling :class:`_SimSteeringChannel`.
    """

    def __init__(self, sim_motor, state: _SimChannelState) -> None:
        self._sim = sim_motor
        self._state = state

    def set_normalized(self, value: float) -> None:
        self._state.thrust = max(-1.0, min(1.0, value))

    async def flush(self) -> None:
        from ..core.models import MotorCommand
        self._sim.apply(MotorCommand(
            thrust=self._state.thrust, steering=self._state.steering))

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def debug(self) -> str:
        return f"SimThrustChannel: thrust={self._state.thrust:+.3f}"

    @property
    def healthy(self) -> bool | None:
        return None  # sim: health not applicable


class _SimSteeringChannel:
    """A split :class:`~vanchor.hardware.split_motor.MotorChannel` that drives
    the steering axis of a :class:`~vanchor.sim.devices.SimMotorController`.

    Symmetric counterpart to :class:`_SimThrustChannel`; flush applies the
    combined command (so both channels' flushes are idempotent — the second
    just re-applies the same already-complete command).
    """

    def __init__(self, sim_motor, state: _SimChannelState) -> None:
        self._sim = sim_motor
        self._state = state

    def set_normalized(self, value: float) -> None:
        self._state.steering = max(-1.0, min(1.0, value))

    async def flush(self) -> None:
        from ..core.models import MotorCommand
        self._sim.apply(MotorCommand(
            thrust=self._state.thrust, steering=self._state.steering))

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def debug(self) -> str:
        return f"SimSteeringChannel: steering={self._state.steering:+.3f}"

    @property
    def healthy(self) -> bool | None:
        return None  # sim: health not applicable
