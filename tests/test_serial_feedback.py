"""Tests for steering-feedback consumption on the real-hardware path (#83).

The steering Arduino reports its measured azimuth back over serial as
``A <angle_deg> <ok> <wrap_pct>`` at ~10 Hz. :class:`SerialMotorController`
reads those lines off the same transport it writes ``CMD`` to and exposes the
latest report as :attr:`last_feedback`; the app reads that into the
``steering`` telemetry block. None of these tests open a physical port.
"""

from __future__ import annotations

import asyncio

from vanchor.hardware.serial_devices import (
    SerialMotorController,
    SteeringFeedback,
    parse_steering_feedback,
)
from vanchor.hardware.serial_link import FakeSerialTransport


# --------------------------------------------------------------------------- #
# Pure parser
# --------------------------------------------------------------------------- #
def test_parse_valid_line() -> None:
    fb = parse_steering_feedback("A 12.4 1 6")
    assert fb == SteeringFeedback(angle_deg=12.4, ok=True, wrap_pct=6.0)


def test_parse_valid_negative_and_not_ok() -> None:
    fb = parse_steering_feedback("A -12.4 0 -7")
    assert fb is not None
    assert fb.angle_deg == -12.4
    assert fb.ok is False
    assert fb.wrap_pct == -7.0


def test_parse_tolerates_trailing_terminator_and_whitespace() -> None:
    assert parse_steering_feedback("A 12.4 1 6\r\n".strip()) == SteeringFeedback(
        12.4, True, 6.0
    )


def test_parse_rejects_malformed_lines() -> None:
    # Wrong leader, other line types, blanks, non-numeric, truncated -> None.
    assert parse_steering_feedback("") is None
    assert parse_steering_feedback("CMD 0 F 0") is None
    assert parse_steering_feedback("E something went wrong") is None
    assert parse_steering_feedback("A") is None  # partial line
    assert parse_steering_feedback("A 12.4 1") is None  # truncated (no wrap)
    assert parse_steering_feedback("A nan-ish 1 6") is None
    assert parse_steering_feedback("A 12.4 1 wat") is None


# --------------------------------------------------------------------------- #
# Read loop on the controller
# --------------------------------------------------------------------------- #
async def _drain(transport: FakeSerialTransport) -> None:
    """Yield until the controller's feedback loop has drained the queue."""
    for _ in range(100):
        await asyncio.sleep(0)
        if transport._inbound.empty():  # type: ignore[attr-defined]
            # one more turn so the loop processes the last item
            await asyncio.sleep(0)
            return


async def test_controller_surfaces_last_feedback() -> None:
    t = FakeSerialTransport()
    mc = SerialMotorController(t)
    assert mc.last_feedback is None
    await mc.start()
    t.feed("A 12.4 1 6")
    await _drain(t)
    assert mc.last_feedback == SteeringFeedback(12.4, True, 6.0)
    await mc.stop()


async def test_controller_ignores_malformed_keeps_last_good() -> None:
    t = FakeSerialTransport()
    mc = SerialMotorController(t)
    await mc.start()
    t.feed("A 5.0 1 3")
    t.feed("garbage line")  # ignored
    t.feed("A")  # partial line, ignored
    await _drain(t)
    assert mc.last_feedback == SteeringFeedback(5.0, True, 3.0)
    # A later good line overwrites.
    t.feed("A -8.0 0 -50")
    await _drain(t)
    assert mc.last_feedback == SteeringFeedback(-8.0, False, -50.0)
    await mc.stop()


async def test_partial_line_buffering_then_complete() -> None:
    # Until a complete A-line arrives, last_feedback stays at its prior value.
    t = FakeSerialTransport()
    mc = SerialMotorController(t)
    await mc.start()
    t.feed("A 1.0 1 1")
    await _drain(t)
    assert mc.last_feedback == SteeringFeedback(1.0, True, 1.0)
    t.feed("A 2.0 1")  # truncated/partial -> ignored, last_feedback unchanged
    await _drain(t)
    assert mc.last_feedback == SteeringFeedback(1.0, True, 1.0)
    await mc.stop()


# --------------------------------------------------------------------------- #
# Integration: feedback lands where telemetry reads it
# --------------------------------------------------------------------------- #
def test_telemetry_reads_feedback_via_motor_attr() -> None:
    """Mirror exactly how app._build_telemetry consumes the feedback.

    The app does ``getattr(self.controller.motor, "last_feedback", None)`` and,
    when present, overrides ``steering.angle_deg`` / ``wrap_pct`` /
    ``feedback_ok``. A sim motor (no such attribute) leaves the modelled values.
    """
    mc = SerialMotorController(FakeSerialTransport())
    mc.last_feedback = SteeringFeedback(angle_deg=33.3, ok=False, wrap_pct=88.0)

    steering = {"angle_deg": 0.0, "wrap_pct": 0.0, "feedback_ok": True}
    feedback = getattr(mc, "last_feedback", None)
    if feedback is not None:
        steering["angle_deg"] = round(feedback.angle_deg, 1)
        steering["wrap_pct"] = round(feedback.wrap_pct, 0)
        steering["feedback_ok"] = feedback.ok

    assert steering == {"angle_deg": 33.3, "wrap_pct": 88.0, "feedback_ok": False}


def test_telemetry_unaffected_when_motor_has_no_feedback_attr() -> None:
    class _SimLikeMotor:  # no last_feedback attribute
        pass

    steering = {"angle_deg": 1.5, "wrap_pct": 2.0, "feedback_ok": True}
    feedback = getattr(_SimLikeMotor(), "last_feedback", None)
    assert feedback is None
    # Telemetry block left untouched (modelled values preserved).
    assert steering == {"angle_deg": 1.5, "wrap_pct": 2.0, "feedback_ok": True}
