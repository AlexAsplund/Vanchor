"""Real-hardware serial drivers (GPS, compass, motor controller).

These implement the same ABCs as the simulated devices
(:mod:`vanchor.sim.devices`), so the controller, navigator and event wiring
cannot tell them apart -- swapping ``Sim*`` for ``Serial*`` is the entirety of
"running on real hardware". All byte-level I/O goes through a
:class:`~vanchor.hardware.serial_link.SerialTransport`, so every driver here is
fully testable with :class:`~vanchor.hardware.serial_link.FakeSerialTransport`
and never opens a physical port.

  SerialGps / SerialCompass  -- read NMEA lines from a transport and republish
                                each one onto the event bus (topic ``nmea.in``)
  SerialMotorController      -- translate a :class:`MotorCommand` into a simple
                                ASCII line protocol for an Arduino-style board
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from dataclasses import dataclass
from typing import Callable

from ..core import events
from ..core.events import EventBus
from ..core.models import MotorCommand
from .interfaces import MotorController, Sensor
from .serial_link import SerialTransport

logger = logging.getLogger("vanchor.hardware.serial")


@dataclass(frozen=True)
class SteeringFeedback:
    """One decoded steering-feedback report from the steering Arduino.

    The firmware (``firmware/steering/steering.ino``) emits a line of the form
    ``A <angle_deg> <ok> <wrap_pct>`` at ~10 Hz where ``angle_deg`` is the
    measured feedback azimuth, ``ok`` is a 1/0 plausibility flag and
    ``wrap_pct`` the cable-wrap fraction (percent).
    """

    angle_deg: float
    ok: bool
    wrap_pct: float


def parse_steering_feedback(line: str) -> SteeringFeedback | None:
    """Parse one ``A <angle_deg> <ok> <wrap_pct>`` feedback line.

    Returns a :class:`SteeringFeedback` on success or ``None`` for anything
    that is not a well-formed ``A`` report (blank lines, other line types such
    as ``CMD``/``E`` echoes, truncated/partial lines, non-numeric fields). This
    is deliberately lenient so a noisy or partially-buffered serial stream can
    never raise out of the read loop.
    """
    parts = line.split()
    if len(parts) < 4 or parts[0] != "A":
        return None
    try:
        angle = float(parts[1])
        wrap = float(parts[3])
    except ValueError:
        return None
    ok = parts[2] == "1"
    return SteeringFeedback(angle_deg=angle, ok=ok, wrap_pct=wrap)


# --------------------------------------------------------------------------- #
# Sensors
# --------------------------------------------------------------------------- #
class _SerialNmeaSensor(Sensor):
    """Shared read-loop for serial NMEA sensors.

    Runs a background task that reads lines from the transport and publishes
    each non-empty line onto the bus as :data:`events.NMEA_IN`. Parsing /
    validation is the navigator's job; this layer is a dumb pipe.
    """

    def __init__(self, transport: SerialTransport, bus: EventBus | None = None) -> None:
        self.transport = transport
        self.bus = bus
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.transport.open()
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        await self.transport.close()

    async def _loop(self) -> None:
        _last_garbage_warn: float = 0.0
        while True:
            try:
                line = await self.transport.read_line()
            except asyncio.CancelledError:
                raise
            except EOFError:
                logger.warning("%s: serial stream closed", type(self).__name__)
                return
            except (ValueError, asyncio.LimitOverrunError) as exc:
                # Oversized or unparseable line (e.g. wrong-baud binary garbage
                # with no newline within the 64 KB StreamReader buffer).
                # Discard and keep reading; log at most once every 5 s.
                _now = _time.monotonic()
                if _now - _last_garbage_warn >= 5.0:
                    logger.warning(
                        "%s: oversized/garbage line discarded – %s",
                        type(self).__name__,
                        exc,
                    )
                    _last_garbage_warn = _now
                continue
            except Exception:  # pragma: no cover - defensive
                logger.exception("%s: read error", type(self).__name__)
                return
            line = line.strip()
            if not line:
                continue
            if self.bus is not None:
                await self.bus.publish(events.NMEA_IN, line)


class SerialGps(_SerialNmeaSensor):
    """A GPS receiver on a serial port emitting RMC/GGA sentences."""


class SerialCompass(_SerialNmeaSensor):
    """A digital compass on a serial port emitting HDM/HDG sentences."""


# --------------------------------------------------------------------------- #
# Motor controller
# --------------------------------------------------------------------------- #
TimeFn = Callable[[], float]


class SerialMotorController(MotorController):
    """Drive an Arduino-style motor board over a serial line protocol.

    Line protocol (newline-terminated, one command per :meth:`flush`)::

        CMD <pwm> <dir> <steer>

    where:

      ``pwm``    integer 0..255  -- magnitude of thrust (0 = stop)
      ``dir``    ``F`` or ``R``  -- forward or reverse (drive direction)
      ``steer``  integer -100..100 -- steering: -100 hard port, +100 hard
                 starboard, 0 centred

    Example lines::

        CMD 0 F 0          # stopped, centred
        CMD 255 F 0        # full ahead, centred
        CMD 128 R -100     # half astern, hard port
        CMD 255 F 100      # full ahead, hard starboard

    The normalized :class:`MotorCommand` maps as ``pwm = round(|thrust| * 255)``
    and ``steer = round(steering * 100)``; ``thrust >= 0`` is ``F`` else ``R``.

    **Reverse delay.** Real ESCs / motor drivers can be damaged (or stall) by an
    instantaneous forward<->reverse reversal. This controller enforces a
    ``reverse_delay_s`` (default 0.9 s) during which a thrust *sign flip* is
    blocked: when the requested direction would reverse, the commanded thrust is
    forced to zero until thrust has been ~zero for at least the delay, then the
    new direction is allowed. Time is read from an injectable ``time_fn`` so the
    behaviour is deterministic in tests (the delay is evaluated on each
    :meth:`flush`).
    """

    #: Thrust magnitudes at or below this are treated as "stopped" for the
    #: purpose of the reverse-delay interlock.
    ZERO_THRUST_EPS: float = 1e-3

    def __init__(
        self,
        transport: SerialTransport,
        *,
        reverse_delay_s: float = 0.9,
        time_fn: TimeFn | None = None,
    ) -> None:
        self.transport = transport
        self.reverse_delay_s = reverse_delay_s
        self._time_fn = time_fn or _default_time
        self._command = MotorCommand()
        # Sign of the last *non-zero* thrust actually emitted: +1, -1, or 0.
        self._last_dir: int = 0
        # Timestamp at which thrust last became ~zero (start of the cooldown).
        self._zero_since: float | None = self._time_fn()
        # Latest steering-feedback report decoded from the inbound ``A`` line, or
        # ``None`` until the firmware has reported at least once. The runtime
        # polls this to populate the closed-loop steering telemetry on real
        # hardware (see the integration note below).
        self.last_feedback: SteeringFeedback | None = None
        self._feedback_task: asyncio.Task | None = None

    def apply(self, command: MotorCommand) -> None:
        self._command = command.clamped()

    async def start(self) -> None:
        await self.transport.open()
        # Start reading the Arduino's steering-feedback line off the same
        # transport we write ``CMD`` to. Lives entirely in the hardware layer.
        self._feedback_task = asyncio.ensure_future(self._read_feedback())

    async def stop(self) -> None:
        if self._feedback_task is not None:
            self._feedback_task.cancel()
            self._feedback_task = None
        # Best-effort: command a full stop before closing.
        try:
            await self.transport.write_line(self._format(0, "F", 0))
        except Exception:  # pragma: no cover - defensive
            logger.debug("failed to send stop command on shutdown")
        await self.transport.close()

    # -- steering feedback (#83) ------------------------------------------ #
    async def _read_feedback(self) -> None:
        """Read ``A <angle> <ok> <wrap>`` feedback lines off the transport.

        The steering Arduino reports its measured azimuth on the same serial
        link the controller writes ``CMD`` to (~10 Hz). We keep only the latest
        report in :attr:`last_feedback`; the runtime reads it into the steering
        telemetry. Malformed/partial lines and unrelated line types (e.g. ``E``
        error echoes) are ignored so the loop never dies on noisy serial.

        **Integration seam (#83):** :attr:`last_feedback` is the hook the app
        polls. ``VanchorApp._build_telemetry`` reads
        ``getattr(self.controller.motor, "last_feedback", None)`` and, when
        present, sets ``steering.angle_deg`` / ``feedback_ok`` / ``wrap_pct``
        from it. The simulator's motor controller has no such attribute, so the
        sim path is unaffected. No extra app.py wiring beyond that one read.
        """
        _last_garbage_warn: float = 0.0
        while True:
            try:
                line = await self.transport.read_line()
            except asyncio.CancelledError:
                raise
            except EOFError:
                logger.warning("%s: serial stream closed", type(self).__name__)
                return
            except (ValueError, asyncio.LimitOverrunError) as exc:
                # Oversized or unparseable line (e.g. wrong-baud binary garbage).
                # Discard and keep reading; log at most once every 5 s.
                _now = _time.monotonic()
                if _now - _last_garbage_warn >= 5.0:
                    logger.warning(
                        "%s: oversized/garbage feedback line discarded – %s",
                        type(self).__name__,
                        exc,
                    )
                    _last_garbage_warn = _now
                continue
            except Exception:  # pragma: no cover - defensive
                logger.exception("%s: feedback read error", type(self).__name__)
                return
            feedback = parse_steering_feedback(line.strip())
            if feedback is not None:
                self.last_feedback = feedback

    async def flush(self) -> None:
        """Apply the reverse-delay interlock and write the latest command."""
        thrust, steering = self._command.thrust, self._command.steering
        thrust = self._gate_reverse(thrust)

        pwm = min(255, max(0, round(abs(thrust) * 255)))
        direction = "R" if thrust < 0 else "F"
        steer = min(100, max(-100, round(steering * 100)))
        await self.transport.write_line(self._format(pwm, direction, steer))

    # -- protocol helpers ------------------------------------------------- #
    @staticmethod
    def _format(pwm: int, direction: str, steer: int) -> str:
        return f"CMD {pwm} {direction} {steer}"

    def format_command(self, command: MotorCommand) -> str:
        """Format ``command`` to its protocol line (no interlock; for tests)."""
        thrust = command.clamped().thrust
        pwm = min(255, max(0, round(abs(thrust) * 255)))
        direction = "R" if thrust < 0 else "F"
        steer = min(100, max(-100, round(command.clamped().steering * 100)))
        return self._format(pwm, direction, steer)

    # -- reverse-delay interlock ------------------------------------------ #
    def _gate_reverse(self, thrust: float) -> float:
        """Return the thrust to actually emit, honouring the reverse delay.

        Called once per :meth:`flush`. Tracks how long thrust has been ~zero and
        refuses to apply a thrust whose sign opposes the last emitted direction
        until that cooldown has elapsed.
        """
        now = self._time_fn()
        new_dir = 0 if abs(thrust) <= self.ZERO_THRUST_EPS else (1 if thrust > 0 else -1)

        if new_dir == 0:
            # Stopped (or near-stopped): start/continue the cooldown clock.
            # Do NOT clear _last_dir here — we must remember which direction we
            # were travelling so the opposite-direction check still fires after
            # one or more zero-thrust ticks (the PID crossing-zero scenario).
            if self._zero_since is None:
                self._zero_since = now
            return thrust

        opposes = self._last_dir != 0 and new_dir != self._last_dir
        if opposes:
            elapsed = now - self._zero_since if self._zero_since is not None else 0.0
            if elapsed < self.reverse_delay_s:
                # Block the flip: hold at stop and keep waiting.
                logger.debug(
                    "reverse delay: blocking %s flip (%.2fs / %.2fs)",
                    "fwd->rev" if new_dir < 0 else "rev->fwd",
                    elapsed,
                    self.reverse_delay_s,
                )
                if self._zero_since is None:
                    self._zero_since = now
                return 0.0

        # Allowed: emit, and remember this direction. We are no longer at zero.
        self._last_dir = new_dir
        self._zero_since = None
        return thrust


def _default_time() -> float:
    import time

    return time.monotonic()
