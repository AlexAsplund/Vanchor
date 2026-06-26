"""Hardware abstraction layer.

These abstract base classes are the seam between the controller and the
physical world. The simulator implements exactly these interfaces, so swapping
in real serial hardware later means writing new subclasses -- nothing in the
controller or control logic changes.

  Sensor          -- something that produces NMEA and pushes it onto the bus
  MotorController  -- accepts a normalized MotorCommand (thrust + steering)
  Actuator         -- a generic servo/stepper position channel (0..1 or angle)
"""

from __future__ import annotations

import abc

from ..core.models import MotorCommand


class Sensor(abc.ABC):
    """A device that emits NMEA sentences (GPS, compass, ...).

    Implementations publish raw sentences onto the event bus (topic
    ``nmea.in``). ``start``/``stop`` manage any background polling task.
    """

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...


class MotorController(abc.ABC):
    """Accepts actuator-level motor commands.

    A real implementation translates ``thrust`` to an ESC/PWM signal and
    ``steering`` to a stepper/servo position over serial; the simulator applies
    them to the boat physics. ``apply`` is synchronous-friendly so the control
    loop can call it deterministically; ``flush`` does any async I/O.
    """

    @abc.abstractmethod
    def apply(self, command: MotorCommand) -> None:
        """Record/translate the latest command (must be cheap, non-blocking)."""

    async def flush(self) -> None:
        """Push the latest command to the device (no-op for the simulator)."""

    async def start(self) -> None:  # pragma: no cover - trivial default
        return None

    async def stop(self) -> None:  # pragma: no cover - trivial default
        return None


class Actuator(abc.ABC):
    """A generic single-channel actuator (servo angle or stepper position).

    Provided to satisfy the "general servo/stepper motor control interface"
    requirement: real steering hardware (a stepper driving the motor head, or a
    servo) implements this; :mod:`vanchor.sim.devices` provides a simulated one.
    """

    @abc.abstractmethod
    def set_normalized(self, value: float) -> None:
        """Command a position in the range [-1, 1]."""

    @property
    @abc.abstractmethod
    def position(self) -> float:
        """The actuator's current normalized position."""
