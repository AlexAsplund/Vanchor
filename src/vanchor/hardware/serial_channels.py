"""Serial channel devices for split-motor operation.

Each class drives ONE axis of the split motor over its own serial line. They
implement :class:`~vanchor.hardware.split_motor.MotorChannel` and are used by
:class:`~vanchor.hardware.split_motor.SplitMotor` when
:func:`~vanchor.hardware.link_plan.plan_motor_links` resolves to
``kind="split"`` with ``source="serial"`` on one or both sides.

Both channels reuse the supervision and reconnect machinery from
:class:`~vanchor.hardware.serial_devices._SerialReadSupervisor` — the same
exponential-backoff reconnect loop shared by every serial reader in the
codebase. Each channel runs its OWN transport and feedback supervisor so a
fault on one axis never blocks the other.

Write cadence
~~~~~~~~~~~~~
``flush()`` writes a frame on **every call**, mirroring the combined
:class:`~vanchor.hardware.serial_devices.SerialMotorController` cadence. This
keeps the firmware's 800 ms loss-of-signal watchdog fed. Non-finite (NaN/inf)
internal values are silently replaced with 0.0 before formatting so a
corrupted state can neither raise past the transport error-handler nor emit a
malformed frame. Transport errors are caught, the channel is marked unhealthy,
and the command is dropped (logged rate-limited) so a transport-down condition
never propagates out of the channel.

BENCH-VERIFY
~~~~~~~~~~~~
No physical split boards (a steering-only Arduino and a thrust-only Arduino)
existed as of 2026-07-06. These classes implement the *planned* split
protocols documented in the "Split firmware protocol" section of
``firmware/README.md``. They MUST be bench-verified against actual firmware
before deploying on a boat. The protocols are intentionally simple line-
oriented ASCII so they can be tested with any USB-serial console.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time as _time

from .serial_devices import (
    EngineStatus,
    SleepFn,
    SteeringFeedback,
    _SerialReadSupervisor,
    parse_engine_status,
    parse_steering_feedback,
)
from .serial_link import SerialTransport, append_crc
from .split_motor import MotorChannel

logger = logging.getLogger("vanchor.hardware.serial")


class SerialSteeringChannel(MotorChannel):
    """Drives a steering-only split board over a serial line.

    Line protocol (Pi → Arduino), protocol v2.1 — the target azimuth goes in
    plain DEGREES so there is no normalized-scale contract between the two
    codebases (the pre-v2.1 normalized ``STEER -100..100`` token was silently
    rescaled when the firmware's range constant changed; degrees can't be)::

        STEERD <deg>\\r\\n

    where ``deg`` is the signed target azimuth in decimal degrees off the bow
    (port −, starboard +), ``normalized * full_scale_deg``. The firmware
    clamps to its own soft endstops (±steer_range_deg).

    Feedback (Arduino → Pi): the standard steering report ``A <angle_deg>
    <ok> <wrap_pct> [<seq>]`` at ~10 Hz — the SAME degrees, so command and
    feedback compare directly on a bench console.

    Example lines::

        STEERD 0.0       # centred
        STEERD 180.0     # full mechanical swing (astern)
        STEERD -35.0     # 35° to port (the autopilot's authority)

    BENCH-VERIFY: no physical split steering board exists as of 2026-07-06.
    This protocol is the planned split-channel variant; it must be verified
    against actual firmware (see ``firmware/README.md`` — "Split firmware
    protocol").
    """

    def __init__(
        self,
        transport: SerialTransport,
        *,
        full_scale_deg: float = 180.0,
        sleep: SleepFn = asyncio.sleep,
        backoff_start: float = 1.0,
        backoff_max: float = 15.0,
    ) -> None:
        self.transport = transport
        # Physical degrees a full-scale normalized command maps to — the ONE
        # steering scale constant (BoatConfig.max_steer_angle_deg).
        self.full_scale_deg = float(full_scale_deg)
        self._value: float = 0.0
        self.last_feedback: SteeringFeedback | None = None
        self._feedback_task: asyncio.Task | None = None
        # Last frame written to the transport + outcome.
        self._last_frame: str | None = None
        self._last_frame_ok: bool = False
        # Rate-limit for the "write while transport down" warning in flush().
        self._last_write_warn: float = 0.0
        # Count of successfully parsed feedback lines (for debug).
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

    def set_normalized(self, value: float) -> None:
        """Record the latest normalized steering command in [-1, 1] (clamped)."""
        self._value = max(-1.0, min(1.0, value))

    async def start(self) -> None:
        await self.transport.open()
        self._feedback_task = asyncio.ensure_future(self._sup.run())

    async def stop(self) -> None:
        self._sup.request_stop()
        if self._feedback_task is not None:
            self._feedback_task.cancel()
            self._feedback_task = None
        # Best-effort: command a centred stop before closing.
        try:
            await self.transport.write_line(append_crc(self._format(0.0)))
        except Exception:  # pragma: no cover - defensive
            logger.debug("%s: failed to send stop command on shutdown", type(self).__name__)
        await self.transport.close()

    async def flush(self) -> None:
        """Write the latest steering command; catches all transport errors.

        Non-finite (NaN/inf) ``_value`` is treated as 0.0 before the clamp so
        a corrupted value can neither raise past the transport try/except nor
        emit a malformed frame.  Transport errors are caught, the channel is
        marked unhealthy, and the command is dropped (logged rate-limited) —
        the error must NOT propagate out so :class:`SplitMotor` can still
        service the other channel.
        """
        value = self._value if math.isfinite(self._value) else 0.0
        deg = max(-1.0, min(1.0, value)) * self.full_scale_deg
        line = append_crc(self._format(deg))     # protocol v2 (*HH)
        self._last_frame = line
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
                    "%s: transport write failed (%s); dropping command",
                    type(self).__name__,
                    exc,
                )
                self._last_write_warn = now
        else:
            self._last_frame_ok = True

    async def _handle_line(self, line: str) -> None:
        stripped = line.strip()
        feedback = parse_steering_feedback(stripped)
        if feedback is not None:
            self.last_feedback = feedback
            self._rx_count += 1

    @staticmethod
    def _format(deg: float) -> str:
        return f"STEERD {deg:.1f}"

    def debug(self) -> str:
        """Human-readable snapshot; never raises."""
        cls = type(self).__name__
        try:
            if self._last_frame is None:
                return f"{cls}: waiting for data…"
            status = "ok" if self._last_frame_ok else "write failed (link down)"
            lines = [
                cls,
                f"  value   : {self._value:+.3f} ({self._value * self.full_scale_deg:+.1f}°)",
                f"  frame   : {self._last_frame!r} ({status})",
                f"  healthy : {self.healthy}",
                f"  rx_count: {self._rx_count}",
            ]
            if self.last_feedback is not None:
                fb = self.last_feedback
                lines.append(
                    f"  feedback: angle={fb.angle_deg:.1f}°"
                    f" ok={fb.ok} wrap={fb.wrap_pct:.0f}%"
                )
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001 - debug must never raise
            return f"{cls}: debug error ({exc})"


class SerialThrustChannel(MotorChannel):
    """Drives a thrust-only split board over a serial line.

    Line protocol (Pi → Arduino)::

        THRUST <pwm> <dir>\\r\\n

    where ``pwm`` is an integer in ``0..255``
    (``round(|normalized| * 255)``) and ``dir`` is ``F`` (forward,
    ``normalized ≥ 0``) or ``R`` (reverse, ``normalized < 0``).

    Feedback (Arduino → Pi): the standard engine-status report
    ``E <pwm> <dir> <state> [<seq>]`` at ~5 Hz, parsed with
    :func:`~vanchor.hardware.serial_devices.parse_engine_status`.

    Example lines::

        THRUST 0 F       # stopped
        THRUST 255 F     # full ahead
        THRUST 128 R     # half astern

    BENCH-VERIFY: no physical split thrust board exists as of 2026-07-06.
    This protocol is the planned split-channel variant; it must be verified
    against actual firmware (see ``firmware/README.md`` — "Split firmware
    protocol").
    """

    def __init__(
        self,
        transport: SerialTransport,
        *,
        sleep: SleepFn = asyncio.sleep,
        backoff_start: float = 1.0,
        backoff_max: float = 15.0,
    ) -> None:
        self.transport = transport
        self._value: float = 0.0
        self.last_engine_status: EngineStatus | None = None
        self._last_engine_state: str | None = None
        self._feedback_task: asyncio.Task | None = None
        # Last frame written to the transport + outcome.
        self._last_frame: str | None = None
        self._last_frame_ok: bool = False
        # Rate-limit for the "write while transport down" warning in flush().
        self._last_write_warn: float = 0.0
        # Count of successfully parsed feedback lines (for debug).
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

    def set_normalized(self, value: float) -> None:
        """Record the latest normalized thrust command in [-1, 1] (clamped)."""
        self._value = max(-1.0, min(1.0, value))

    async def start(self) -> None:
        await self.transport.open()
        self._feedback_task = asyncio.ensure_future(self._sup.run())

    async def stop(self) -> None:
        self._sup.request_stop()
        if self._feedback_task is not None:
            self._feedback_task.cancel()
            self._feedback_task = None
        # Best-effort: command a stop before closing.
        try:
            await self.transport.write_line(append_crc(self._format(0, "F")))
        except Exception:  # pragma: no cover - defensive
            logger.debug("%s: failed to send stop command on shutdown", type(self).__name__)
        await self.transport.close()

    async def flush(self) -> None:
        """Write the latest thrust command; catches all transport errors.

        Non-finite (NaN/inf) ``_value`` is treated as 0.0 before the clamp so
        a corrupted value can neither raise past the transport try/except nor
        emit a malformed frame.  Transport errors are caught, the channel is
        marked unhealthy, and the command is dropped (logged rate-limited) —
        the error must NOT propagate out so :class:`SplitMotor` can still
        service the other channel.
        """
        value = self._value if math.isfinite(self._value) else 0.0
        pwm = min(255, max(0, round(abs(value) * 255)))
        direction = "R" if value < 0 else "F"
        line = append_crc(self._format(pwm, direction))   # protocol v2 (*HH)
        self._last_frame = line
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
                    "%s: transport write failed (%s); dropping command",
                    type(self).__name__,
                    exc,
                )
                self._last_write_warn = now
        else:
            self._last_frame_ok = True

    async def _handle_line(self, line: str) -> None:
        stripped = line.strip()
        status = parse_engine_status(stripped)
        if status is not None:
            self._note_engine_status(status)
            self._rx_count += 1

    def _note_engine_status(self, status: EngineStatus) -> None:
        """Store the latest engine status and log real state transitions."""
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

    @staticmethod
    def _format(pwm: int, direction: str) -> str:
        return f"THRUST {pwm} {direction}"

    def debug(self) -> str:
        """Human-readable snapshot; never raises."""
        cls = type(self).__name__
        try:
            if self._last_frame is None:
                return f"{cls}: waiting for data…"
            status = "ok" if self._last_frame_ok else "write failed (link down)"
            lines = [
                cls,
                f"  value   : {self._value:+.3f} (pwm={round(abs(self._value)*255)})",
                f"  frame   : {self._last_frame!r} ({status})",
                f"  healthy : {self.healthy}",
                f"  rx_count: {self._rx_count}",
            ]
            if self.last_engine_status is not None:
                es = self.last_engine_status
                lines.append(
                    f"  engine  : pwm={es.pwm} dir={es.direction} state={es.state}"
                )
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001 - debug must never raise
            return f"{cls}: debug error ({exc})"
