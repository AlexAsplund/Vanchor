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
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from ..core import events
from ..core.events import EventBus
from ..core.models import MotorCommand
from .interfaces import MotorController, Sensor
from .serial_link import SerialTransport, append_crc, strip_verify_crc

logger = logging.getLogger("vanchor.hardware.serial")


@dataclass(frozen=True)
class SteeringFeedback:
    """One decoded steering-feedback report from the steering Arduino.

    The firmware (``firmware/steering/steering.ino``) emits a line of the form
    ``A <angle_deg> <ok> <wrap_pct> [<seq>]`` at ~10 Hz where ``angle_deg`` is
    the measured feedback azimuth, ``ok`` is a 1/0 plausibility flag and
    ``wrap_pct`` the cable-wrap fraction (percent).

    ``seq`` is the OPTIONAL heartbeat echo (roadmap #18): the sequence number of
    the last ``CMD`` line the board parsed, echoed straight back so the Pi can
    detect a one-way serial failure. It is ``None`` when the field is absent —
    i.e. when talking to an older firmware that predates the heartbeat — which
    the driver treats as "unknown", never as a failure (see
    :class:`SerialMotorController`). A value of ``-1`` means the board is
    heartbeat-capable but has not yet parsed any ``CMD`` (its echo is "no
    command seen"), which is distinct from the field being absent.
    """

    angle_deg: float
    ok: bool
    wrap_pct: float
    seq: int | None = None


def parse_steering_feedback(line: str) -> SteeringFeedback | None:
    """Parse one ``A <angle_deg> <ok> <wrap_pct> [<seq>]`` feedback line.

    Returns a :class:`SteeringFeedback` on success or ``None`` for anything
    that is not a well-formed ``A`` report (blank lines, other line types such
    as ``CMD``/``E`` echoes, truncated/partial lines, non-numeric fields). This
    is deliberately lenient so a noisy or partially-buffered serial stream can
    never raise out of the read loop.

    The 5th ``seq`` field is optional and backward-compatible: a 4-field line
    (older firmware) parses fine with ``seq=None``; a present-but-garbage 5th
    field is ignored (also ``seq=None``) rather than rejecting the whole report.
    """
    payload, crc_ok = strip_verify_crc(line)
    if crc_ok is False:
        return None                     # corrupted feedback: reject, never guess
    parts = payload.split()
    if len(parts) < 4 or parts[0] != "A":
        return None
    try:
        angle = float(parts[1])
        wrap = float(parts[3])
    except ValueError:
        return None
    ok = parts[2] == "1"
    seq: int | None = None
    if len(parts) >= 5:
        try:
            seq = int(parts[4])
        except ValueError:
            seq = None
    return SteeringFeedback(angle_deg=angle, ok=ok, wrap_pct=wrap, seq=seq)


@dataclass(frozen=True)
class EngineStatus:
    """One decoded applied-state report from the engine Arduino.

    The firmware (``firmware/engine/engine.ino``) emits a line of the form
    ``E <pwm> <dir> <state> [<seq>]`` at ~5 Hz for debugging / observability.
    ``pwm`` is the applied throttle magnitude (0..255), ``dir`` is ``F``/``R``
    and ``state`` one of ``RUN`` / ``SOFTSTART`` / ``REVDELAY`` / ``FAILSAFE``.
    ``seq`` is the optional heartbeat echo (roadmap #18), ``None`` when absent.

    Historically the Pi dropped ``E`` lines on the floor; the driver now parses
    them so the last engine state is observable and the heartbeat echo on the
    engine board is honoured just like the steering board's ``A`` echo.
    """

    pwm: int
    direction: str
    state: str
    seq: int | None = None


def parse_engine_status(line: str) -> EngineStatus | None:
    """Parse one ``E <pwm> <dir> <state> [<seq>]`` engine-status line.

    Returns an :class:`EngineStatus` on success or ``None`` for anything that is
    not a well-formed ``E`` report (other line types, blanks, non-numeric pwm,
    truncated lines). As lenient as :func:`parse_steering_feedback` so a noisy
    stream never raises out of the read loop.
    """
    payload, crc_ok = strip_verify_crc(line)
    if crc_ok is False:
        return None                     # corrupted status: reject, never guess
    parts = payload.split()
    if len(parts) < 4 or parts[0] != "E":
        return None
    try:
        pwm = int(parts[1])
    except ValueError:
        return None
    direction = parts[2]
    if direction not in ("F", "R"):
        return None
    state = parts[3]
    seq: int | None = None
    if len(parts) >= 5:
        try:
            seq = int(parts[4])
        except ValueError:
            seq = None
    return EngineStatus(pwm=pwm, direction=direction, state=state, seq=seq)


# --------------------------------------------------------------------------- #
# Supervised read loop (shared by every serial reader)
# --------------------------------------------------------------------------- #
SleepFn = Callable[[float], Awaitable[None]]
LineHandler = Callable[[str], Awaitable[None]]


class _SerialReadSupervisor:
    """Read lines off a transport forever, reconnecting through drops.

    This is the single piece of supervision shared by both serial readers (the
    NMEA sensors and the motor controller's steering-feedback loop) so the
    reconnect/backoff/health logic lives in exactly one place. The owning
    device opens the transport once (in its ``start``) and then hands the
    already-open transport here; on EOF or an unexpected read error the loop
    closes the transport best-effort and retries :meth:`SerialTransport.open`
    with exponential backoff (``backoff_start`` → ×2 → capped at
    ``backoff_max``) *forever*, until :meth:`request_stop` is called. Re-opening
    the same transport instance is what the real ``PySerialTransport`` supports
    (``close`` drops the reader, ``open`` re-establishes it).

    Each successfully read line is handed to ``handle_line`` (an async
    callable). Garbage lines (``ValueError`` / ``asyncio.LimitOverrunError``,
    e.g. wrong-baud binary with no newline in the 64 KB buffer) are discarded
    and logged rate-limited without dropping the connection.

    Health is exposed for other layers to poll (no telemetry wiring here):

      ``healthy``              True while connected and reading; False while
                               disconnected / backing off.
      ``last_data_monotonic``  ``time.monotonic`` stamp of the last line read,
                               or ``None`` before the first line.
    """

    def __init__(
        self,
        transport: SerialTransport,
        handle_line: LineHandler,
        *,
        name: str,
        sleep: SleepFn = asyncio.sleep,
        backoff_start: float = 1.0,
        backoff_max: float = 15.0,
    ) -> None:
        self.transport = transport
        self._handle_line = handle_line
        self._name = name
        self._sleep = sleep
        self._backoff_start = backoff_start
        self._backoff_max = backoff_max
        self._stop = asyncio.Event()
        self.healthy: bool = False
        self.last_data_monotonic: float | None = None

    def request_stop(self) -> None:
        """Ask the loop to exit; unblocks a backoff wait immediately."""
        self._stop.set()

    async def run(self) -> None:
        # The transport is already open (the owning device opened it in start()).
        self.healthy = True
        last_garbage_warn = 0.0
        while not self._stop.is_set():
            try:
                line = await self.transport.read_line()
            except asyncio.CancelledError:
                raise
            except (ValueError, asyncio.LimitOverrunError) as exc:
                # Oversized/garbage line — discard, keep the connection.
                last_garbage_warn = self._warn_garbage(exc, last_garbage_warn)
                continue
            except EOFError:
                logger.warning("%s: serial stream closed; reconnecting", self._name)
                await self._reconnect()
                continue
            except Exception as exc:  # unexpected transport/read error
                logger.warning(
                    "%s: read error (%s); reconnecting", self._name, exc
                )
                await self._reconnect()
                continue
            self.last_data_monotonic = _time.monotonic()
            await self._handle_line(line)

    def _warn_garbage(self, exc: BaseException, last_warn: float) -> float:
        now = _time.monotonic()
        if now - last_warn >= 5.0:
            logger.warning(
                "%s: oversized/garbage line discarded – %s", self._name, exc
            )
            return now
        return last_warn

    async def _reconnect(self) -> None:
        """Close and re-open the transport with exponential backoff.

        Returns as soon as the transport is re-opened *or* a stop is requested;
        the caller's loop re-checks the stop flag on return.
        """
        self.healthy = False
        try:
            await self.transport.close()
        except Exception:  # best-effort — the port may already be gone
            logger.debug("%s: error closing transport during reconnect", self._name)
        backoff = self._backoff_start
        while not self._stop.is_set():
            if await self._wait(backoff):
                return  # stop requested mid-backoff
            try:
                await self.transport.open()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                next_backoff = min(backoff * 2, self._backoff_max)
                logger.warning(
                    "%s: reconnect failed (%s); retrying in %.0fs",
                    self._name,
                    exc,
                    next_backoff,
                )
                backoff = next_backoff
                continue
            logger.info("%s: serial reconnected", self._name)
            self.healthy = True
            return

    async def _wait(self, delay: float) -> bool:
        """Sleep ``delay`` but wake early if a stop is requested.

        Races the (injectable) sleep against the stop event so backoff is both
        interruptible and testable — tests inject a sleep that returns
        immediately (recording the delay) and never wall-clock wait. Returns
        True iff a stop was requested.
        """
        if self._stop.is_set():
            return True
        napping = asyncio.ensure_future(self._sleep(delay))
        stopping = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait(
                {napping, stopping}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            napping.cancel()
            stopping.cancel()
        return self._stop.is_set()


# --------------------------------------------------------------------------- #
# Sensors
# --------------------------------------------------------------------------- #
class _SerialNmeaSensor(Sensor):
    """Shared read-loop for serial NMEA sensors.

    Runs a background task (a :class:`_SerialReadSupervisor`) that reads lines
    from the transport and publishes each non-empty line onto the bus as
    :data:`events.NMEA_IN`. Parsing / validation is the navigator's job; this
    layer is a dumb pipe. The supervisor reconnects automatically across an
    unplug/replug, so a transient serial drop no longer silently kills the
    sensor. :attr:`healthy` / :attr:`last_data_monotonic` are pollable health
    signals (see :class:`_SerialReadSupervisor`).
    """

    def __init__(
        self,
        transport: SerialTransport,
        bus: EventBus | None = None,
        *,
        sleep: SleepFn = asyncio.sleep,
        backoff_start: float = 1.0,
        backoff_max: float = 15.0,
    ) -> None:
        self.transport = transport
        self.bus = bus
        self._task: asyncio.Task | None = None
        # --- debug capture (Devices -> Debug live view) -------------------- #
        # A small ring buffer of the most recent RAW NMEA lines read off the
        # port, plus a received count. One debug() below serves both subclasses.
        self._recent_lines: deque[str] = deque(maxlen=6)
        self._rx_count: int = 0
        self._sup = _SerialReadSupervisor(
            transport,
            self._handle_line,
            name=type(self).__name__,
            sleep=sleep,
            backoff_start=backoff_start,
            backoff_max=backoff_max,
        )

    @property
    def healthy(self) -> bool:
        return self._sup.healthy

    @property
    def last_data_monotonic(self) -> float | None:
        return self._sup.last_data_monotonic

    async def _handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        self._recent_lines.append(line)
        self._rx_count += 1
        if self.bus is not None:
            await self.bus.publish(events.NMEA_IN, line)

    def debug(self) -> str:
        # Shared by SerialGps + SerialCompass; type(self).__name__ names each.
        cls = type(self).__name__
        try:
            port = getattr(self.transport, "port", None) or repr(self.transport)
            if not self._recent_lines:
                return f"{cls}: waiting for data…"
            lines = [
                cls,
                f"  port    : {port}",
                f"  healthy : {self.healthy}",
                f"  count   : {self._rx_count}",
                "  recent  :",
            ]
            lines.extend(f"    {raw}" for raw in self._recent_lines)
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001 - debug must never raise
            return f"{cls}: debug error ({exc})"

    async def start(self) -> None:
        await self.transport.open()
        self._task = asyncio.ensure_future(self._sup.run())

    async def stop(self) -> None:
        self._sup.request_stop()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        await self.transport.close()


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

    #: Command sequence numbers wrap modulo this (keeps the ``CMD`` line short
    #: and the wrap period — thousands of commands — far longer than any ack
    #: window, so an echoed seq is never ambiguous within the tracked history).
    SEQ_MODULO: int = 10000

    #: How many recently-sent seqs to remember for ack-matching. At ~10 Hz this
    #: is several seconds of history, comfortably longer than ``ack_timeout_s``.
    RECENT_SENT_MAX: int = 64

    def __init__(
        self,
        transport: SerialTransport,
        *,
        reverse_delay_s: float = 0.9,
        time_fn: TimeFn | None = None,
        sleep: SleepFn = asyncio.sleep,
        backoff_start: float = 1.0,
        backoff_max: float = 15.0,
        heartbeat: bool = False,
        ack_timeout_s: float = 2.0,
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
        # Latest engine applied-state report decoded from an inbound ``E`` line
        # (roadmap #18): previously these lines were dropped; now they are
        # parsed, stored here and logged (state changes) so the engine board's
        # applied throttle/direction/state is observable from the driver.
        self.last_engine_status: EngineStatus | None = None
        self._last_engine_state: str | None = None
        # --- heartbeat round-trip (roadmap #18) --------------------------- #
        # When ``heartbeat`` is on, every ``flush`` appends an incrementing seq
        # to the ``CMD`` line and the driver expects the firmware to echo that
        # seq back in its ``A``/``E`` feedback. If our recent commands stop
        # being acknowledged within ``ack_timeout_s`` the device is marked
        # unhealthy via the EXISTING health flag — this catches a ONE-WAY serial
        # failure (writes land but nothing comes back, or feedback keeps coming
        # but never reflects our commands) that the plain read-loop supervisor,
        # which only sees EOF/errors, cannot. Default OFF so the wire format and
        # health semantics are bit-for-bit unchanged unless explicitly enabled.
        self._heartbeat = heartbeat
        self._ack_timeout_s = ack_timeout_s
        self._seq = 0
        self._recent_sent: deque[int] = deque(maxlen=self.RECENT_SENT_MAX)
        # Latches True once we have proof the firmware speaks the heartbeat
        # protocol (a feedback line that carries a seq field at all). Until then
        # a missing echo is "unknown", never a failure — an older non-echoing
        # firmware must never brick the driver.
        self._seq_supported = False
        # ``time_fn`` stamp of the last acknowledged command (an echoed seq that
        # matches one we sent), or of the moment we latched heartbeat support.
        self._last_ack_monotonic: float | None = None
        self._last_acked_seq: int | None = None
        self._feedback_task: asyncio.Task | None = None
        # Reads the steering-feedback line off the same transport we write CMD
        # to, and — crucially — reconnects that transport across a drop so the
        # link comes back after an unplug/replug rather than dying silently.
        self._sup = _SerialReadSupervisor(
            transport,
            self._handle_feedback_line,
            name=type(self).__name__,
            sleep=sleep,
            backoff_start=backoff_start,
            backoff_max=backoff_max,
        )
        # Rate-limit for the "write while transport down" warning in flush().
        self._last_write_warn: float = 0.0
        # --- debug capture (Devices -> Debug live view) -------------------- #
        # The last raw ``CMD`` frame handed to the transport, whether the write
        # landed, and a monotonic stamp. The last applied command is _command.
        self._last_frame: str | None = None
        self._last_frame_ok: bool = False
        self._last_frame_monotonic: float | None = None

    @property
    def healthy(self) -> bool:
        """Link health: transport up AND (if armed) recent commands acked.

        Default-preserving: with ``heartbeat`` off, or before we have any proof
        the firmware echoes seq, this returns the supervisor's flag exactly as
        before. Once heartbeat is armed *and* the firmware has been seen to echo
        seq, an ack drought longer than ``ack_timeout_s`` also reads unhealthy —
        that is the one-way-failure signal the raw read loop cannot produce.
        """
        if not self._sup.healthy:
            return False
        if not self._heartbeat or not self._seq_supported:
            return True  # unknown/disarmed -> defer to the transport-level flag
        if self._last_ack_monotonic is None:  # pragma: no cover - defensive
            return True
        return (self._time_fn() - self._last_ack_monotonic) <= self._ack_timeout_s

    @property
    def last_data_monotonic(self) -> float | None:
        return self._sup.last_data_monotonic

    @property
    def last_acked_seq(self) -> int | None:
        """The most recent command seq the firmware has echoed back (or None)."""
        return self._last_acked_seq

    def apply(self, command: MotorCommand) -> None:
        self._command = command.clamped()

    def debug(self) -> str:
        cls = type(self).__name__
        try:
            if self._last_frame is None:
                return f"{cls}: waiting for data…"
            cmd = self._command
            status = "ok" if self._last_frame_ok else "write failed (link down)"
            lines = [
                cls,
                f"  command : thrust={cmd.thrust:+.3f}  steering={cmd.steering:+.3f}",
                f"  frame   : {self._last_frame!r} ({status})",
                f"  healthy : {self.healthy}",
            ]
            if self.last_feedback is not None:
                fb = self.last_feedback
                lines.append(
                    f"  steer fb: angle={fb.angle_deg:.1f}° ok={fb.ok} wrap={fb.wrap_pct:.0f}%"
                )
            if self.last_engine_status is not None:
                es = self.last_engine_status
                lines.append(
                    f"  engine  : pwm={es.pwm} dir={es.direction} state={es.state}"
                )
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001 - debug must never raise
            return f"{cls}: debug error ({exc})"

    async def start(self) -> None:
        await self.transport.open()
        # Start reading the Arduino's steering-feedback line off the same
        # transport we write ``CMD`` to. The supervisor also reconnects that
        # transport across a drop. Lives entirely in the hardware layer.
        self._feedback_task = asyncio.ensure_future(self._sup.run())

    async def stop(self) -> None:
        self._sup.request_stop()
        if self._feedback_task is not None:
            self._feedback_task.cancel()
            self._feedback_task = None
        # Best-effort: command a full stop before closing (only meaningful if
        # the transport is currently open; a write on a down link is dropped).
        try:
            await self.transport.write_line(self._format(0, "F", 0))
        except Exception:  # pragma: no cover - defensive
            logger.debug("failed to send stop command on shutdown")
        await self.transport.close()

    # -- steering feedback (#83) ------------------------------------------ #
    async def _handle_feedback_line(self, line: str) -> None:
        """Consume one ``A <angle> <ok> <wrap>`` feedback line.

        The steering Arduino reports its measured azimuth on the same serial
        link the controller writes ``CMD`` to (~10 Hz). We keep only the latest
        report in :attr:`last_feedback`; the runtime reads it into the steering
        telemetry. Malformed/partial lines and unrelated line types (e.g. ``E``
        error echoes) parse to ``None`` and are ignored, so noisy serial never
        disturbs the last good value. Connection-level survival (garbage lines,
        EOF, reconnect) is handled by :class:`_SerialReadSupervisor`.

        **Integration seam (#83):** :attr:`last_feedback` is the hook the app
        polls. ``VanchorApp._build_telemetry`` reads
        ``getattr(self.controller.motor, "last_feedback", None)`` and, when
        present, sets ``steering.angle_deg`` / ``feedback_ok`` / ``wrap_pct``
        from it. The simulator's motor controller has no such attribute, so the
        sim path is unaffected. No extra app.py wiring beyond that one read.

        Roadmap #18 extends this to two more line types on the same link:
        ``A`` lines may carry a heartbeat seq echo (fed to :meth:`_note_seq`),
        and ``E`` engine-status lines — previously dropped — are now parsed into
        :attr:`last_engine_status`, logged on state changes, and their own seq
        echo honoured.
        """
        stripped = line.strip()
        feedback = parse_steering_feedback(stripped)
        if feedback is not None:
            self.last_feedback = feedback
            self._note_seq(feedback.seq)
            return
        status = parse_engine_status(stripped)
        if status is not None:
            self._note_engine_status(status)
            self._note_seq(status.seq)

    def _note_engine_status(self, status: EngineStatus) -> None:
        """Store the latest engine status and log real state transitions.

        Only a *change* of state is logged (rate-limited implicitly by the
        firmware's ~5 Hz report) so a steady ``RUN`` stream is quiet; a step
        into ``FAILSAFE`` logs at WARNING because that means the engine board's
        own watchdog has tripped (it stopped seeing our commands).
        """
        self.last_engine_status = status
        if status.state != self._last_engine_state:
            level = logging.WARNING if status.state == "FAILSAFE" else logging.INFO
            logger.log(
                level,
                "%s: engine status %s (pwm=%d dir=%s)",
                type(self).__name__,
                status.state,
                status.pwm,
                status.direction,
            )
            self._last_engine_state = status.state

    def _note_seq(self, seq: int | None) -> None:
        """Record a heartbeat echo from a feedback line.

        ``seq is None`` means the line carried no seq field at all: an older
        firmware that predates the heartbeat. We stay in the "unknown" state and
        never latch — such a firmware must not be able to brick the driver.

        A present seq field (even the ``-1`` "no command seen yet" sentinel)
        proves the firmware speaks the heartbeat protocol, so we latch support
        and start the ack clock. An echoed seq that matches one we actually sent
        acknowledges the round-trip and refreshes the clock; anything else (the
        ``-1`` sentinel, or a stale value we never sent, e.g. because our writes
        are not reaching the board) does NOT refresh it, so the ack window will
        elapse and mark the device unhealthy.
        """
        if seq is None:
            return
        if not self._seq_supported:
            self._seq_supported = True
            self._last_ack_monotonic = self._time_fn()
        if seq >= 0 and seq in self._recent_sent:
            self._last_ack_monotonic = self._time_fn()
            self._last_acked_seq = seq

    async def flush(self) -> None:
        """Apply the reverse-delay interlock and write the latest command.

        A write must never raise out of the control loop while the transport is
        down mid-reconnect: catch the error, mark unhealthy and drop the command
        (log rate-limited). The firmware's own watchdog stops the motor on
        command loss, and the feedback supervisor restores the link.

        With ``heartbeat`` armed, an incrementing seq is appended to the ``CMD``
        line so the firmware can echo it back for one-way-failure detection. The
        seq is recorded as "sent" only on a successful write (a dropped write
        never reached the board, so the firmware will never ack it).
        """
        thrust, steering = self._command.thrust, self._command.steering
        thrust = self._gate_reverse(thrust)

        pwm = min(255, max(0, round(abs(thrust) * 255)))
        direction = "R" if thrust < 0 else "F"
        steer = min(100, max(-100, round(steering * 100)))
        line = self._format(pwm, direction, steer)
        seq: int | None = None
        if self._heartbeat:
            self._seq = (self._seq + 1) % self.SEQ_MODULO
            seq = self._seq
            line = f"{line} {seq}"
        line = append_crc(line)          # protocol v2 line integrity (*HH)
        self._last_frame = line
        self._last_frame_monotonic = _time.monotonic()
        try:
            await self.transport.write_line(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._sup.healthy = False
            self._last_frame_ok = False
            now = _time.monotonic()
            if now - self._last_write_warn >= 5.0:
                logger.warning(
                    "%s: transport write failed while down (%s); dropping command",
                    type(self).__name__,
                    exc,
                )
                self._last_write_warn = now
        else:
            self._last_frame_ok = True
            if seq is not None:
                self._recent_sent.append(seq)

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
