"""Hardware abstraction layer.

These abstract base classes are the seam between the controller and the
physical world. The simulator implements exactly these interfaces, so swapping
in real serial hardware later means writing new subclasses -- nothing in the
controller or control logic changes.

  Sensor          -- something that produces NMEA and pushes it onto the bus
  MotorController  -- accepts a normalized MotorCommand (thrust + steering)
  Actuator         -- a generic servo/stepper position channel (0..1 or angle)
  BatteryMonitor   -- a battery gauge (voltage/current/state-of-charge)
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

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


class NullMotor(MotorController):
    """A motor that is 'Not connected': it safely accepts and discards commands.

    Used when ``motor_source`` is ``"none"`` so the control loop can run without
    crashing while no actuation happens. Motor-commanding modes are disabled in
    the UI + controller (see :mod:`vanchor.core.capabilities`); this is only the
    backstop that makes an errant command a harmless no-op.
    """

    def apply(self, command: MotorCommand) -> None:  # noqa: D102
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


@dataclass
class BatteryReading:
    """One raw sample from a battery gauge.

    ``soc_pct`` may be ``None`` when the gauge does not report a state-of-charge
    directly (a bare shunt only measures voltage + current) — the driver then
    estimates it (coulomb counting / resting-voltage)."""

    voltage_v: float
    current_a: float
    soc_pct: float | None = None


class BatteryMonitor(abc.ABC):
    """A battery gauge: reports pack voltage, current and state-of-charge.

    This is the reference *4th* (non-core) device kind (roadmap #42), built via
    the versioned capability API (#43). Implementations may run a background poll
    loop (``start``/``stop``); :meth:`snapshot` returns the same telemetry shape
    the sim battery produces so the UI, the range/time-to-empty estimate and the
    Return-to-Launch logic are agnostic to whether the numbers come from a
    simulated pack or a real shunt over the HAL.
    """

    async def start(self) -> None:  # pragma: no cover - trivial default
        return None

    async def stop(self) -> None:  # pragma: no cover - trivial default
        return None

    @abc.abstractmethod
    def snapshot(self) -> dict:
        """Battery telemetry: ``{soc_pct, voltage_v, current_a, draw_w, range_m,
        time_to_empty_s}`` (``time_to_empty_s`` may be ``None`` when unknown)."""

    def health(self) -> dict:  # pragma: no cover - trivial default
        """``{"ok": bool, "detail": str}`` — overridden by drivers that can fault."""
        return {"ok": True, "detail": ""}
