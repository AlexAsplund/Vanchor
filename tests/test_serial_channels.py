"""Tests for the serial split-motor channel devices.

All tests use :class:`~vanchor.hardware.serial_link.FakeSerialTransport` so no
physical port is opened. Tests cover:

  * Frame format (STEER / THRUST line protocol)
  * Value clamping before format (out-of-range values)
  * Neutral output on zero
  * Feedback line parsing -> healthy + feedback stored
  * EOF -> reconnect attempt (sleep is injected so no real wall-clock wait)
  * Write failures: channel degrades gracefully, marks unhealthy, does not raise
  * debug() content and the guarantee that it never raises
  * Write-on-every-flush cadence (mirrors the combined controller)
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

import pytest

from vanchor.hardware.serial_channels import SerialSteeringChannel, SerialThrustChannel
from vanchor.hardware.serial_link import FakeSerialTransport, append_crc


# --------------------------------------------------------------------------- #
# Fake sleep helper: records requested delays, returns immediately.
# --------------------------------------------------------------------------- #

class _RecordingSleep:
    """Stands in for asyncio.sleep in supervised channels.

    Returns immediately (no wall-clock wait) and records each requested delay
    so tests can assert reconnect backs off.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        # Yield once so the event loop can process cancellations, then return.
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# Helpers to run a channel with inbound data then stop it.
# --------------------------------------------------------------------------- #

async def _run_channel_briefly(channel, ticks: int = 20) -> None:
    """Start the channel, yield ``ticks`` times, then stop it."""
    await channel.start()
    for _ in range(ticks):
        await asyncio.sleep(0)
    await channel.stop()


# =========================================================================== #
# SerialSteeringChannel                                                        #
# =========================================================================== #

class TestSerialSteeringChannel:
    """Frame format, clamping, feedback, reconnect, debug."""

    # -- frame format -------------------------------------------------------- #

    async def test_frame_zero(self) -> None:
        """Zero value -> STEER 0"""
        t = FakeSerialTransport()
        ch = SerialSteeringChannel(t)
        ch.set_normalized(0.0)
        await t.open()
        await ch.flush()
        assert t.written == [append_crc("STEER 0")]

    async def test_frame_full_port(self) -> None:
        """Full port (-1.0) -> STEER -100"""
        t = FakeSerialTransport()
        ch = SerialSteeringChannel(t)
        ch.set_normalized(-1.0)
        await t.open()
        await ch.flush()
        assert t.written == [append_crc("STEER -100")]

    async def test_frame_full_starboard(self) -> None:
        """Full starboard (+1.0) -> STEER 100"""
        t = FakeSerialTransport()
        ch = SerialSteeringChannel(t)
        ch.set_normalized(1.0)
        await t.open()
        await ch.flush()
        assert t.written == [append_crc("STEER 100")]

    async def test_frame_midpoint(self) -> None:
        """Half starboard (0.5) -> STEER 50"""
        t = FakeSerialTransport()
        ch = SerialSteeringChannel(t)
        ch.set_normalized(0.5)
        await t.open()
        await ch.flush()
        assert t.written == [append_crc("STEER 50")]

    async def test_frame_rounding(self) -> None:
        """Round-to-int: 0.156 -> round(15.6) = 16"""
        t = FakeSerialTransport()
        ch = SerialSteeringChannel(t)
        ch.set_normalized(0.156)
        await t.open()
        await ch.flush()
        assert t.written == [append_crc("STEER 16")]

    # -- clamping ------------------------------------------------------------ #

    async def test_clamp_high(self) -> None:
        """set_normalized(2.0) -> clamped to +1.0 -> STEER 100"""
        t = FakeSerialTransport()
        ch = SerialSteeringChannel(t)
        ch.set_normalized(2.0)
        await t.open()
        await ch.flush()
        assert t.written == [append_crc("STEER 100")]

    async def test_clamp_low(self) -> None:
        """set_normalized(-5.0) -> clamped to -1.0 -> STEER -100"""
        t = FakeSerialTransport()
        ch = SerialSteeringChannel(t)
        ch.set_normalized(-5.0)
        await t.open()
        await ch.flush()
        assert t.written == [append_crc("STEER -100")]

    async def test_flush_clamps_independently(self) -> None:
        """flush() re-clamps before formatting: avoids out-of-range frames
        even if the internal state were somehow corrupted."""
        t = FakeSerialTransport()
        ch = SerialSteeringChannel(t)
        # Bypass set_normalized to inject a raw out-of-range value.
        ch._value = 1.5  # pyright: ignore[reportPrivateUsage]
        await t.open()
        await ch.flush()
        assert t.written == [append_crc("STEER 100")]

    # -- neutral on stop ----------------------------------------------------- #

    async def test_neutral_after_stop(self) -> None:
        """stop() writes STEER 0 as a best-effort neutral command."""
        t = FakeSerialTransport()
        ch = SerialSteeringChannel(t)
        ch.set_normalized(0.8)
        sleep = _RecordingSleep()
        ch._sup._sleep = sleep  # pyright: ignore[reportPrivateUsage]
        t.feed_eof()  # unblock the supervisor read loop
        await channel_start_stop(ch, t)
        # The stop command is the last written line.
        assert t.written and t.written[-1] == append_crc("STEER 0")

    # -- feedback parsing -> healthy ----------------------------------------- #

    async def test_feedback_parsing(self) -> None:
        """An 'A ...' feedback line -> last_feedback populated, rx_count incremented."""
        t = FakeSerialTransport()
        t.feed("A 12.5 1 -7")
        t.feed_eof()
        sleep = _RecordingSleep()
        ch = SerialSteeringChannel(t, sleep=sleep)
        await channel_start_stop(ch, t)
        assert ch.last_feedback is not None
        assert ch.last_feedback.angle_deg == pytest.approx(12.5)
        assert ch.last_feedback.ok is True
        assert ch.last_feedback.wrap_pct == pytest.approx(-7.0)
        assert ch._rx_count >= 1  # pyright: ignore[reportPrivateUsage]

    async def test_healthy_after_feedback(self) -> None:
        """healthy is True once the supervisor read loop is running."""
        t = FakeSerialTransport()
        t.feed("A 0.0 1 0")
        sleep = _RecordingSleep()
        ch = SerialSteeringChannel(t, sleep=sleep)
        await ch.start()
        # Yield until healthy (supervisor sets healthy=True on entry to run()).
        for _ in range(30):
            await asyncio.sleep(0)
            if ch.healthy:
                break
        assert ch.healthy
        # Clean up.
        t.feed_eof()
        await channel_start_stop(ch, t, already_started=True)

    async def test_malformed_lines_ignored(self) -> None:
        """Non-A lines and malformed lines don't set last_feedback."""
        t = FakeSerialTransport()
        t.feed("E 100 F RUN")
        t.feed("garbage line here")
        t.feed_eof()
        sleep = _RecordingSleep()
        ch = SerialSteeringChannel(t, sleep=sleep)
        await channel_start_stop(ch, t)
        assert ch.last_feedback is None

    # -- EOF -> reconnect attempt -------------------------------------------- #

    async def test_eof_triggers_reconnect(self) -> None:
        """EOF causes the supervisor to attempt reconnect (no real sleep)."""
        t = FakeSerialTransport()
        t.feed_eof()
        sleep = _RecordingSleep()
        ch = SerialSteeringChannel(t, sleep=sleep, backoff_start=0.1, backoff_max=1.0)
        await ch.start()
        # Let the loop process the EOF and start backing off.
        for _ in range(40):
            await asyncio.sleep(0)
            if sleep.delays:
                break
        await ch.stop()
        # At least one backoff delay was requested, proving reconnect ran.
        assert len(sleep.delays) >= 1

    async def test_eof_resets_healthy(self) -> None:
        """After an EOF, healthy goes False until reconnect."""
        t = FakeSerialTransport()
        t.feed_eof()
        sleep = _RecordingSleep()
        ch = SerialSteeringChannel(t, sleep=sleep, backoff_start=0.1)
        await ch.start()
        # Yield enough for the EOF to propagate.
        for _ in range(30):
            await asyncio.sleep(0)
            if not ch.healthy:
                break
        assert not ch.healthy
        await ch.stop()

    # -- write failure ------------------------------------------------------- #

    async def test_write_failure_does_not_raise(self) -> None:
        """A write failure in flush() must not propagate out."""
        t = FakeSerialTransport()
        t.fail_writes = True
        ch = SerialSteeringChannel(t)
        ch.set_normalized(0.5)
        await t.open()
        # Must not raise.
        await ch.flush()

    async def test_write_failure_marks_unhealthy(self) -> None:
        """A write failure marks the supervisor as unhealthy."""
        t = FakeSerialTransport()
        ch = SerialSteeringChannel(t)
        ch.set_normalized(0.3)
        await t.open()
        ch._sup.healthy = True  # pyright: ignore[reportPrivateUsage]
        t.fail_writes = True
        await ch.flush()
        assert not ch.healthy

    async def test_write_failure_last_frame_ok_false(self) -> None:
        """A write failure sets _last_frame_ok to False."""
        t = FakeSerialTransport()
        t.fail_writes = True
        ch = SerialSteeringChannel(t)
        ch.set_normalized(0.5)
        await t.open()
        await ch.flush()
        assert not ch._last_frame_ok  # pyright: ignore[reportPrivateUsage]

    # -- write cadence: always writes ---------------------------------------- #

    async def test_flush_writes_every_call(self) -> None:
        """flush() writes on every call (mirrors combined controller cadence)."""
        t = FakeSerialTransport()
        ch = SerialSteeringChannel(t)
        await t.open()
        ch.set_normalized(0.0)
        for _ in range(5):
            await ch.flush()
        assert len(t.written) == 5

    async def test_flush_writes_even_when_unchanged(self) -> None:
        """flush() writes even when the value hasn't changed (keepalive)."""
        t = FakeSerialTransport()
        ch = SerialSteeringChannel(t)
        await t.open()
        ch.set_normalized(0.5)
        await ch.flush()
        await ch.flush()
        assert len(t.written) == 2
        assert t.written[0] == t.written[1] == append_crc("STEER 50")

    # -- debug --------------------------------------------------------------- #

    def test_debug_never_raises_fresh(self) -> None:
        """debug() never raises on a freshly created channel."""
        ch = SerialSteeringChannel(FakeSerialTransport())
        result = ch.debug()
        assert isinstance(result, str)

    async def test_debug_never_raises_after_flush(self) -> None:
        """debug() never raises after a flush."""
        t = FakeSerialTransport()
        ch = SerialSteeringChannel(t)
        ch.set_normalized(0.75)
        await t.open()
        await ch.flush()
        result = ch.debug()
        assert isinstance(result, str)

    async def test_debug_contains_frame(self) -> None:
        """debug() output contains the last frame after flush."""
        t = FakeSerialTransport()
        ch = SerialSteeringChannel(t)
        ch.set_normalized(0.5)
        await t.open()
        await ch.flush()
        text = ch.debug()
        assert "STEER 50" in text

    async def test_debug_contains_feedback(self) -> None:
        """debug() shows feedback when available."""
        t = FakeSerialTransport()
        t.feed("A 33.0 1 -5")
        t.feed_eof()
        sleep = _RecordingSleep()
        ch = SerialSteeringChannel(t, sleep=sleep)
        await channel_start_stop(ch, t)
        await t.open()
        ch.set_normalized(0.1)
        await ch.flush()
        text = ch.debug()
        assert "33.0" in text

    def test_debug_does_not_raise_with_corrupt_state(self) -> None:
        """debug() never raises even with deliberately bad internal state."""
        ch = SerialSteeringChannel(FakeSerialTransport())
        # Corrupt the internal state.
        ch._value = float("nan")  # pyright: ignore[reportPrivateUsage]
        result = ch.debug()
        assert isinstance(result, str)

    async def test_flush_with_nan_value(self) -> None:
        """flush() with _value=NaN emits a NEUTRAL frame and does not raise."""
        t = FakeSerialTransport()
        ch = SerialSteeringChannel(t)
        ch._value = float("nan")  # pyright: ignore[reportPrivateUsage]
        await t.open()
        await ch.flush()  # must not raise
        assert t.written == [append_crc("STEER 0")]


# =========================================================================== #
# SerialThrustChannel                                                          #
# =========================================================================== #

class TestSerialThrustChannel:
    """Frame format, clamping, feedback, reconnect, debug."""

    # -- frame format -------------------------------------------------------- #

    async def test_frame_zero(self) -> None:
        """Zero value -> THRUST 0 F"""
        t = FakeSerialTransport()
        ch = SerialThrustChannel(t)
        ch.set_normalized(0.0)
        await t.open()
        await ch.flush()
        assert t.written == [append_crc("THRUST 0 F")]

    async def test_frame_full_forward(self) -> None:
        """Full forward (+1.0) -> THRUST 255 F"""
        t = FakeSerialTransport()
        ch = SerialThrustChannel(t)
        ch.set_normalized(1.0)
        await t.open()
        await ch.flush()
        assert t.written == [append_crc("THRUST 255 F")]

    async def test_frame_full_reverse(self) -> None:
        """Full reverse (-1.0) -> THRUST 255 R"""
        t = FakeSerialTransport()
        ch = SerialThrustChannel(t)
        ch.set_normalized(-1.0)
        await t.open()
        await ch.flush()
        assert t.written == [append_crc("THRUST 255 R")]

    async def test_frame_half_forward(self) -> None:
        """Half forward (0.5) -> THRUST 128 F (round(127.5) = 128)"""
        t = FakeSerialTransport()
        ch = SerialThrustChannel(t)
        ch.set_normalized(0.5)
        await t.open()
        await ch.flush()
        assert t.written == [append_crc("THRUST 128 F")]

    async def test_frame_direction_boundary(self) -> None:
        """Tiny negative -> R; tiny positive -> F; exactly 0 -> F."""
        t = FakeSerialTransport()
        ch = SerialThrustChannel(t)
        await t.open()

        ch.set_normalized(-0.001)
        await ch.flush()
        assert t.written[-1] == append_crc("THRUST 0 R")

        ch.set_normalized(0.001)
        await ch.flush()
        assert t.written[-1] == append_crc("THRUST 0 F")

        ch.set_normalized(0.0)
        await ch.flush()
        assert t.written[-1] == append_crc("THRUST 0 F")

    # -- clamping ------------------------------------------------------------ #

    async def test_clamp_high(self) -> None:
        """set_normalized(3.0) -> clamped to 1.0 -> THRUST 255 F"""
        t = FakeSerialTransport()
        ch = SerialThrustChannel(t)
        ch.set_normalized(3.0)
        await t.open()
        await ch.flush()
        assert t.written == [append_crc("THRUST 255 F")]

    async def test_clamp_low(self) -> None:
        """set_normalized(-3.0) -> clamped to -1.0 -> THRUST 255 R"""
        t = FakeSerialTransport()
        ch = SerialThrustChannel(t)
        ch.set_normalized(-3.0)
        await t.open()
        await ch.flush()
        assert t.written == [append_crc("THRUST 255 R")]

    async def test_flush_clamps_independently(self) -> None:
        """flush() re-clamps: internal _value > 1.0 -> THRUST 255 F."""
        t = FakeSerialTransport()
        ch = SerialThrustChannel(t)
        ch._value = 2.0  # pyright: ignore[reportPrivateUsage]
        await t.open()
        await ch.flush()
        assert t.written == [append_crc("THRUST 255 F")]

    # -- neutral on stop ----------------------------------------------------- #

    async def test_neutral_after_stop(self) -> None:
        """stop() writes THRUST 0 F as a best-effort neutral command."""
        t = FakeSerialTransport()
        sleep = _RecordingSleep()
        ch = SerialThrustChannel(t, sleep=sleep)
        ch.set_normalized(0.9)
        t.feed_eof()
        await channel_start_stop(ch, t)
        assert t.written and t.written[-1] == append_crc("THRUST 0 F")

    # -- feedback parsing -> engine status ----------------------------------- #

    async def test_feedback_parsing(self) -> None:
        """An 'E ...' feedback line -> last_engine_status populated."""
        t = FakeSerialTransport()
        t.feed("E 128 F RUN")
        t.feed_eof()
        sleep = _RecordingSleep()
        ch = SerialThrustChannel(t, sleep=sleep)
        await channel_start_stop(ch, t)
        assert ch.last_engine_status is not None
        assert ch.last_engine_status.pwm == 128
        assert ch.last_engine_status.direction == "F"
        assert ch.last_engine_status.state == "RUN"
        assert ch._rx_count >= 1  # pyright: ignore[reportPrivateUsage]

    async def test_healthy_after_feedback(self) -> None:
        """healthy is True once the supervisor read loop is running."""
        t = FakeSerialTransport()
        t.feed("E 0 F RUN")
        sleep = _RecordingSleep()
        ch = SerialThrustChannel(t, sleep=sleep)
        await ch.start()
        for _ in range(30):
            await asyncio.sleep(0)
            if ch.healthy:
                break
        assert ch.healthy
        t.feed_eof()
        await channel_start_stop(ch, t, already_started=True)

    async def test_malformed_lines_ignored(self) -> None:
        """Non-E lines and malformed lines don't set last_engine_status."""
        t = FakeSerialTransport()
        t.feed("A 0.0 1 0")  # steering line, not engine
        t.feed("garbage")
        t.feed_eof()
        sleep = _RecordingSleep()
        ch = SerialThrustChannel(t, sleep=sleep)
        await channel_start_stop(ch, t)
        assert ch.last_engine_status is None

    # -- EOF -> reconnect attempt -------------------------------------------- #

    async def test_eof_triggers_reconnect(self) -> None:
        """EOF causes the supervisor to attempt reconnect (no real sleep)."""
        t = FakeSerialTransport()
        t.feed_eof()
        sleep = _RecordingSleep()
        ch = SerialThrustChannel(t, sleep=sleep, backoff_start=0.1, backoff_max=1.0)
        await ch.start()
        for _ in range(40):
            await asyncio.sleep(0)
            if sleep.delays:
                break
        await ch.stop()
        assert len(sleep.delays) >= 1

    async def test_eof_resets_healthy(self) -> None:
        """After an EOF, healthy goes False until reconnect."""
        t = FakeSerialTransport()
        t.feed_eof()
        sleep = _RecordingSleep()
        ch = SerialThrustChannel(t, sleep=sleep, backoff_start=0.1)
        await ch.start()
        for _ in range(30):
            await asyncio.sleep(0)
            if not ch.healthy:
                break
        assert not ch.healthy
        await ch.stop()

    # -- write failure ------------------------------------------------------- #

    async def test_write_failure_does_not_raise(self) -> None:
        """A write failure in flush() must not propagate out."""
        t = FakeSerialTransport()
        t.fail_writes = True
        ch = SerialThrustChannel(t)
        ch.set_normalized(0.5)
        await t.open()
        await ch.flush()

    async def test_write_failure_marks_unhealthy(self) -> None:
        """A write failure marks the supervisor as unhealthy."""
        t = FakeSerialTransport()
        ch = SerialThrustChannel(t)
        ch.set_normalized(0.3)
        await t.open()
        ch._sup.healthy = True  # pyright: ignore[reportPrivateUsage]
        t.fail_writes = True
        await ch.flush()
        assert not ch.healthy

    async def test_write_failure_last_frame_ok_false(self) -> None:
        """A write failure sets _last_frame_ok to False."""
        t = FakeSerialTransport()
        t.fail_writes = True
        ch = SerialThrustChannel(t)
        ch.set_normalized(0.5)
        await t.open()
        await ch.flush()
        assert not ch._last_frame_ok  # pyright: ignore[reportPrivateUsage]

    # -- write cadence: always writes ---------------------------------------- #

    async def test_flush_writes_every_call(self) -> None:
        """flush() writes on every call (mirrors combined controller cadence)."""
        t = FakeSerialTransport()
        ch = SerialThrustChannel(t)
        await t.open()
        ch.set_normalized(0.0)
        for _ in range(5):
            await ch.flush()
        assert len(t.written) == 5

    async def test_flush_writes_even_when_unchanged(self) -> None:
        """flush() writes even when the value hasn't changed (keepalive)."""
        t = FakeSerialTransport()
        ch = SerialThrustChannel(t)
        await t.open()
        ch.set_normalized(0.5)
        await ch.flush()
        await ch.flush()
        assert len(t.written) == 2
        assert t.written[0] == t.written[1] == append_crc("THRUST 128 F")

    # -- debug --------------------------------------------------------------- #

    def test_debug_never_raises_fresh(self) -> None:
        """debug() never raises on a freshly created channel."""
        ch = SerialThrustChannel(FakeSerialTransport())
        result = ch.debug()
        assert isinstance(result, str)

    async def test_debug_never_raises_after_flush(self) -> None:
        """debug() never raises after a flush."""
        t = FakeSerialTransport()
        ch = SerialThrustChannel(t)
        ch.set_normalized(0.75)
        await t.open()
        await ch.flush()
        result = ch.debug()
        assert isinstance(result, str)

    async def test_debug_contains_frame(self) -> None:
        """debug() output contains the last frame after flush."""
        t = FakeSerialTransport()
        ch = SerialThrustChannel(t)
        ch.set_normalized(1.0)
        await t.open()
        await ch.flush()
        text = ch.debug()
        assert "THRUST 255 F" in text

    async def test_debug_contains_engine_status(self) -> None:
        """debug() shows engine status when available."""
        t = FakeSerialTransport()
        t.feed("E 200 F SOFTSTART")
        t.feed_eof()
        sleep = _RecordingSleep()
        ch = SerialThrustChannel(t, sleep=sleep)
        await channel_start_stop(ch, t)
        await t.open()
        ch.set_normalized(0.5)
        await ch.flush()
        text = ch.debug()
        assert "SOFTSTART" in text

    def test_debug_does_not_raise_with_corrupt_state(self) -> None:
        """debug() never raises even with deliberately bad internal state."""
        ch = SerialThrustChannel(FakeSerialTransport())
        ch._value = float("nan")  # pyright: ignore[reportPrivateUsage]
        result = ch.debug()
        assert isinstance(result, str)

    async def test_flush_with_nan_value(self) -> None:
        """flush() with _value=NaN emits a NEUTRAL frame and does not raise."""
        t = FakeSerialTransport()
        ch = SerialThrustChannel(t)
        ch._value = float("nan")  # pyright: ignore[reportPrivateUsage]
        await t.open()
        await ch.flush()  # must not raise
        assert t.written == [append_crc("THRUST 0 F")]


# =========================================================================== #
# Integration: both channels driven together                                   #
# =========================================================================== #

class TestChannelIntegration:
    """Lightweight integration: SplitMotor wrapping both channels."""

    async def test_split_motor_drives_both_channels(self) -> None:
        """SplitMotor.apply routes thrust and steering independently."""
        from vanchor.core.models import MotorCommand
        from vanchor.hardware.split_motor import SplitMotor

        t_thrust = FakeSerialTransport()
        t_steer = FakeSerialTransport()
        thrust_ch = SerialThrustChannel(t_thrust)
        steer_ch = SerialSteeringChannel(t_steer)
        motor = SplitMotor(thrust=thrust_ch, steering=steer_ch)

        await t_thrust.open()
        await t_steer.open()

        motor.apply(MotorCommand(thrust=0.5, steering=-0.25))
        await motor.flush()

        assert t_thrust.written == [append_crc("THRUST 128 F")]
        assert t_steer.written == [append_crc("STEER -25")]

    async def test_split_motor_stop_zeroes_both(self) -> None:
        """A STOP-shaped command zeroes both channels (Constraint 4)."""
        from vanchor.core.models import MotorCommand
        from vanchor.hardware.split_motor import SplitMotor

        t_thrust = FakeSerialTransport()
        t_steer = FakeSerialTransport()
        thrust_ch = SerialThrustChannel(t_thrust)
        steer_ch = SerialSteeringChannel(t_steer)
        motor = SplitMotor(thrust=thrust_ch, steering=steer_ch)

        await t_thrust.open()
        await t_steer.open()

        # Send non-zero first.
        motor.apply(MotorCommand(thrust=0.8, steering=0.5))
        await motor.flush()

        # STOP-shaped command.
        motor.apply(MotorCommand(thrust=0.0, steering=0.0))
        await motor.flush()

        assert t_thrust.written[-1] == append_crc("THRUST 0 F")
        assert t_steer.written[-1] == append_crc("STEER 0")

    async def test_one_channel_write_failure_does_not_block_other(self) -> None:
        """Write failure on thrust must not prevent steering from being written."""
        from vanchor.core.models import MotorCommand
        from vanchor.hardware.split_motor import SplitMotor

        t_thrust = FakeSerialTransport()
        t_steer = FakeSerialTransport()
        t_thrust.fail_writes = True  # thrust transport is down
        thrust_ch = SerialThrustChannel(t_thrust)
        steer_ch = SerialSteeringChannel(t_steer)
        motor = SplitMotor(thrust=thrust_ch, steering=steer_ch)

        await t_thrust.open()
        await t_steer.open()

        motor.apply(MotorCommand(thrust=0.5, steering=0.3))
        await motor.flush()  # Must not raise, even with thrust transport down.

        # Steering still gets its frame.
        assert t_steer.written == [append_crc("STEER 30")]
        # Thrust transport was down -> nothing written.
        assert t_thrust.written == []


# --------------------------------------------------------------------------- #
# Helper: start + stop a channel with clean-up.
# --------------------------------------------------------------------------- #

async def channel_start_stop(
    channel,
    transport: FakeSerialTransport,
    *,
    already_started: bool = False,
    ticks: int = 40,
) -> None:
    """Start (if needed), yield ``ticks`` times, then stop."""
    if not already_started:
        await channel.start()
    for _ in range(ticks):
        await asyncio.sleep(0)
    await channel.stop()
