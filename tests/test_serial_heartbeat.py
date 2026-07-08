"""Tests for the firmware heartbeat round-trip + E-line parsing (roadmap #18).

The Pi tags every ``CMD`` with an incrementing sequence number when the
heartbeat is armed; the firmware echoes the last seq it parsed back in its
``A`` (steering) / ``E`` (engine) feedback. :class:`SerialMotorController`
tracks those echoes and, if its recent commands stop being acknowledged within
``ack_timeout_s``, marks itself unhealthy via the EXISTING per-device health
flag -- this is how a ONE-WAY serial failure (writes land but nothing comes
back, or feedback keeps coming but never reflects our commands) is detected.

Crucially, an older firmware that does not echo seq must stay "unknown", never
"failed": the driver must not be brickable by talking to old firmware. None of
these tests open a physical port.
"""

from __future__ import annotations

import asyncio

from vanchor.hardware.serial_devices import (
    EngineStatus,
    SerialMotorController,
    SteeringFeedback,
    parse_engine_status,
    parse_steering_feedback,
)
from vanchor.hardware.serial_link import FakeSerialTransport, append_crc


# --------------------------------------------------------------------------- #
# Pure parsers: optional seq field on A / E lines
# --------------------------------------------------------------------------- #
def test_parse_steering_feedback_with_seq() -> None:
    assert parse_steering_feedback("A 12.4 1 6 42") == SteeringFeedback(
        angle_deg=12.4, ok=True, wrap_pct=6.0, seq=42
    )
    # -1 sentinel: firmware is heartbeat-capable but has parsed no CMD yet.
    fb = parse_steering_feedback("A 0.0 1 0 -1")
    assert fb is not None and fb.seq == -1


def test_parse_steering_feedback_seq_absent_is_none() -> None:
    # Older firmware (no 5th field) -> seq is None, and equals the seqless value.
    assert parse_steering_feedback("A 12.4 1 6") == SteeringFeedback(12.4, True, 6.0)
    assert parse_steering_feedback("A 12.4 1 6").seq is None  # type: ignore[union-attr]


def test_parse_steering_feedback_garbage_seq_ignored() -> None:
    # A garbage 5th field must not reject an otherwise valid report.
    fb = parse_steering_feedback("A 12.4 1 6 wat")
    assert fb == SteeringFeedback(12.4, True, 6.0, seq=None)


def test_parse_engine_status() -> None:
    assert parse_engine_status("E 128 F RUN 7") == EngineStatus(
        pwm=128, direction="F", state="RUN", seq=7
    )
    assert parse_engine_status("E 0 R FAILSAFE") == EngineStatus(
        pwm=0, direction="R", state="FAILSAFE", seq=None
    )


def test_parse_engine_status_rejects_non_e_lines() -> None:
    assert parse_engine_status("A 12.4 1 6") is None
    assert parse_engine_status("") is None
    assert parse_engine_status("E") is None
    assert parse_engine_status("E abc F RUN") is None  # non-numeric pwm
    assert parse_engine_status("E 100 X RUN") is None  # bad direction


# --------------------------------------------------------------------------- #
# Read-loop helpers
# --------------------------------------------------------------------------- #
async def _drain(transport: FakeSerialTransport) -> None:
    """Yield until the controller's feedback loop has drained the queue."""
    for _ in range(200):
        await asyncio.sleep(0)
        if transport._inbound.empty():  # type: ignore[attr-defined]
            await asyncio.sleep(0)  # one more turn to process the last item
            return


# --------------------------------------------------------------------------- #
# Wire format: heartbeat is opt-in and default-preserving
# --------------------------------------------------------------------------- #
async def test_heartbeat_off_leaves_wire_format_unchanged() -> None:
    t = FakeSerialTransport()
    mc = SerialMotorController(t)  # heartbeat defaults OFF
    await mc.start()
    await asyncio.sleep(0)  # let the read loop come up (sets the transport flag)
    await mc.flush()
    await mc.flush()
    assert t.written == [append_crc("CMD 0 F 0"), append_crc("CMD 0 F 0")]  # no seq appended
    assert mc.healthy is True  # unchanged health semantics
    await mc.stop()


async def test_heartbeat_on_appends_incrementing_seq() -> None:
    t = FakeSerialTransport()
    mc = SerialMotorController(t, heartbeat=True)
    await mc.start()
    await mc.flush()
    await mc.flush()
    assert t.written[:2] == [append_crc("CMD 0 F 0 1"), append_crc("CMD 0 F 0 2")]
    await mc.stop()


async def test_dropped_write_does_not_record_the_seq() -> None:
    # A write that fails never reached the board, so the firmware can't ack it:
    # it must not be counted as "sent", and the transport is marked unhealthy.
    t = FakeSerialTransport()
    mc = SerialMotorController(t, heartbeat=True)
    await mc.start()
    t.fail_writes = True
    await mc.flush()  # raises internally, caught -> unhealthy, seq NOT recorded
    assert mc.healthy is False
    assert t.written == []
    await mc.stop()


# --------------------------------------------------------------------------- #
# (a) echoed seq -> healthy ; (b) echo stops -> unhealthy after the window
# --------------------------------------------------------------------------- #
async def test_ack_keeps_healthy_then_times_out_then_recovers() -> None:
    clock = [0.0]
    t = FakeSerialTransport()
    mc = SerialMotorController(
        t, heartbeat=True, ack_timeout_s=2.0, time_fn=lambda: clock[0]
    )
    await mc.start()
    await asyncio.sleep(0)

    # (a) send CMD seq=1 and have the firmware echo it back -> acknowledged.
    await mc.flush()
    t.feed("A 0.0 1 0 1")
    await _drain(t)
    assert mc.last_acked_seq == 1
    assert mc.healthy is True

    # Still within the window with no new ack: healthy.
    clock[0] = 1.9
    assert mc.healthy is True

    # (b) window elapsed with no fresh ack -> unhealthy (one-way fw->Pi failure).
    clock[0] = 2.1
    assert mc.healthy is False

    # A fresh matching echo revives it (link restored).
    await mc.flush()  # seq=2
    t.feed("A 0.0 1 0 2")
    await _drain(t)
    assert mc.last_acked_seq == 2
    assert mc.healthy is True
    await mc.stop()


async def test_one_way_pi_to_fw_dead_detected_via_stale_seq() -> None:
    # Pi->firmware is dead: the board is alive and keeps emitting feedback, but
    # it never received our commands so it echoes the -1 "no command" sentinel.
    # The seq field IS present (so we know it speaks heartbeat) but never matches
    # a seq we sent -> the ack clock never advances -> unhealthy after the window.
    clock = [0.0]
    t = FakeSerialTransport()
    mc = SerialMotorController(
        t, heartbeat=True, ack_timeout_s=2.0, time_fn=lambda: clock[0]
    )
    await mc.start()
    await asyncio.sleep(0)

    await mc.flush()  # seq=1 (never reaches the board in this scenario)
    t.feed("A 0.0 1 0 -1")  # heartbeat-capable, but "no command seen"
    await _drain(t)
    assert mc.last_acked_seq is None  # nothing of ours was ever acknowledged
    assert mc.healthy is True  # still inside the initial grace window

    clock[0] = 3.0
    t.feed("A 0.0 1 0 -1")  # still only the sentinel, no real ack
    await _drain(t)
    assert mc.healthy is False
    await mc.stop()


# --------------------------------------------------------------------------- #
# Backward compatibility: firmware that does NOT echo seq must not brick us
# --------------------------------------------------------------------------- #
async def test_non_echoing_firmware_stays_unknown_not_failed() -> None:
    clock = [0.0]
    t = FakeSerialTransport()
    mc = SerialMotorController(
        t, heartbeat=True, ack_timeout_s=2.0, time_fn=lambda: clock[0]
    )
    await mc.start()
    await asyncio.sleep(0)

    await mc.flush()  # seq=1 on the wire
    # Older firmware: 4-field A line, NO seq echo at all.
    t.feed("A 0.0 1 0")
    await _drain(t)
    assert mc.last_feedback == SteeringFeedback(0.0, True, 0.0)
    assert mc.last_acked_seq is None

    # Even far past the ack window we stay healthy: a missing echo is "unknown",
    # never "failed" -- an old firmware cannot brick the driver.
    clock[0] = 1000.0
    assert mc.healthy is True
    await mc.stop()


# --------------------------------------------------------------------------- #
# (c) E (engine status) lines are parsed, stored, and honour their seq echo
# --------------------------------------------------------------------------- #
async def test_engine_status_line_is_parsed_and_stored() -> None:
    t = FakeSerialTransport()
    mc = SerialMotorController(t)
    await mc.start()
    assert mc.last_engine_status is None
    t.feed("E 128 F RUN 5")
    await _drain(t)
    assert mc.last_engine_status == EngineStatus(128, "F", "RUN", seq=5)
    # A later line overwrites, and unrelated line types are ignored.
    t.feed("garbage")
    t.feed("E 0 R FAILSAFE 6")
    await _drain(t)
    assert mc.last_engine_status == EngineStatus(0, "R", "FAILSAFE", seq=6)
    await mc.stop()


async def test_engine_status_seq_acks_the_heartbeat() -> None:
    # The heartbeat round-trip also works when it is the ENGINE board (E lines)
    # that echoes the seq, not the steering board.
    clock = [0.0]
    t = FakeSerialTransport()
    mc = SerialMotorController(
        t, heartbeat=True, ack_timeout_s=2.0, time_fn=lambda: clock[0]
    )
    await mc.start()
    await asyncio.sleep(0)

    await mc.flush()  # seq=1
    t.feed("E 0 F RUN 1")
    await _drain(t)
    assert mc.last_acked_seq == 1
    assert mc.healthy is True
    await mc.stop()
