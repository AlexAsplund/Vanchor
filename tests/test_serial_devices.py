"""Tests for the real-hardware serial drivers using a fake transport.

None of these tests open a physical serial port: they drive
:class:`FakeSerialTransport`, feeding inbound NMEA and inspecting outbound
motor-protocol lines.
"""

from __future__ import annotations

import asyncio

from vanchor.core import events
from vanchor.core.events import EventBus
from vanchor.core.models import GeoPoint, MotorCommand
from vanchor.hardware.serial_devices import (
    SerialCompass,
    SerialGps,
    SerialMotorController,
    SteeringFeedback,
)
from vanchor.hardware.serial_link import FakeSerialTransport, PySerialTransport
from vanchor.nav import nmea


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
async def test_fake_transport_round_trip() -> None:
    t = FakeSerialTransport()
    await t.open()
    assert t.opened
    t.feed("hello\r\n")
    assert await t.read_line() == "hello"
    await t.write_line("CMD 0 F 0")
    assert t.written == ["CMD 0 F 0"]
    await t.close()
    assert t.closed


async def test_fake_transport_eof() -> None:
    t = FakeSerialTransport()
    t.feed_eof()
    try:
        await t.read_line()
        assert False, "expected EOFError"
    except EOFError:
        pass


def test_pyserial_transport_imports_without_hardware() -> None:
    # Constructing must not require pyserial-asyncio or a port.
    pt = PySerialTransport("/dev/ttyUSB0", baudrate=9600)
    assert pt.port == "/dev/ttyUSB0"
    assert pt.baudrate == 9600


# --------------------------------------------------------------------------- #
# Sensors
# --------------------------------------------------------------------------- #
async def test_serial_gps_publishes_nmea_in() -> None:
    bus = EventBus()
    received: list[str] = []
    bus.subscribe(events.NMEA_IN, received.append)

    transport = FakeSerialTransport()
    raw = nmea.encode_rmc(GeoPoint(59.3, 18.0), sog_knots=3.2, cog_deg=42.0)
    transport.feed(raw)

    gps = SerialGps(transport, bus)
    await gps.start()
    # Give the read loop a chance to run.
    for _ in range(10):
        await asyncio.sleep(0)
        if received:
            break
    await gps.stop()

    assert received == [raw]
    # And it really is a parseable RMC with our values.
    sentence = nmea.parse(received[0])
    assert isinstance(sentence, nmea.RMC)
    assert abs(sentence.sog_knots - 3.2) < 0.05


async def test_serial_gps_skips_blank_lines() -> None:
    bus = EventBus()
    received: list[str] = []
    bus.subscribe(events.NMEA_IN, received.append)

    transport = FakeSerialTransport()
    transport.feed("   ")
    transport.feed("$GPHDT,123.4,T*hh")

    gps = SerialGps(transport, bus)
    await gps.start()
    for _ in range(10):
        await asyncio.sleep(0)
        if received:
            break
    await gps.stop()

    assert received == ["$GPHDT,123.4,T*hh"]


async def test_serial_compass_is_a_sensor() -> None:
    bus = EventBus()
    received: list[str] = []
    bus.subscribe(events.NMEA_IN, received.append)

    transport = FakeSerialTransport()
    line = nmea.encode_hdm(180.0)
    transport.feed(line)

    compass = SerialCompass(transport, bus)
    await compass.start()
    for _ in range(10):
        await asyncio.sleep(0)
        if received:
            break
    await compass.stop()

    assert received == [line]


async def test_serial_sensor_stops_on_eof() -> None:
    bus = EventBus()
    transport = FakeSerialTransport()
    transport.feed_eof()
    gps = SerialGps(transport, bus)
    await gps.start()
    # The loop should terminate cleanly; closing should be idempotent.
    for _ in range(10):
        await asyncio.sleep(0)
    await gps.stop()
    assert transport.closed


# --------------------------------------------------------------------------- #
# Motor controller protocol
# --------------------------------------------------------------------------- #
async def test_motor_stop_command() -> None:
    transport = FakeSerialTransport()
    motor = SerialMotorController(transport, time_fn=lambda: 0.0)
    motor.apply(MotorCommand(thrust=0.0, steering=0.0))
    await motor.flush()
    assert transport.written == ["CMD 0 F 0"]


async def test_motor_full_forward() -> None:
    transport = FakeSerialTransport()
    motor = SerialMotorController(transport, time_fn=lambda: 0.0)
    motor.apply(MotorCommand(thrust=1.0, steering=0.0))
    await motor.flush()
    assert transport.written == ["CMD 255 F 0"]


async def test_motor_steering_extremes() -> None:
    transport = FakeSerialTransport()
    motor = SerialMotorController(transport, time_fn=lambda: 0.0)

    motor.apply(MotorCommand(thrust=1.0, steering=-1.0))
    await motor.flush()
    motor.apply(MotorCommand(thrust=0.5, steering=1.0))
    await motor.flush()

    assert transport.written == ["CMD 255 F -100", "CMD 128 F 100"]


async def test_motor_clamps_out_of_range() -> None:
    transport = FakeSerialTransport()
    motor = SerialMotorController(transport, time_fn=lambda: 0.0)
    motor.apply(MotorCommand(thrust=5.0, steering=-9.0))
    await motor.flush()
    assert transport.written == ["CMD 255 F -100"]


# --------------------------------------------------------------------------- #
# Reverse-delay interlock
# --------------------------------------------------------------------------- #
async def test_reverse_delay_blocks_immediate_flip_and_allows_after_delay() -> None:
    clock = {"t": 0.0}
    transport = FakeSerialTransport()
    motor = SerialMotorController(
        transport, reverse_delay_s=0.9, time_fn=lambda: clock["t"]
    )

    # Drive forward.
    motor.apply(MotorCommand(thrust=1.0, steering=0.0))
    await motor.flush()
    assert transport.written[-1] == "CMD 255 F 0"

    # Immediately request reverse -> blocked, held at stop.
    motor.apply(MotorCommand(thrust=-1.0, steering=0.0))
    await motor.flush()
    assert transport.written[-1] == "CMD 0 F 0"

    # Still within the cooldown -> still blocked.
    clock["t"] = 0.5
    await motor.flush()
    assert transport.written[-1] == "CMD 0 F 0"

    # After the delay elapses -> reverse is allowed.
    clock["t"] = 1.0  # 1.0s of being "zero" >= 0.9s
    await motor.flush()
    assert transport.written[-1] == "CMD 255 R 0"


async def test_reverse_allowed_when_already_stopped_long_enough() -> None:
    clock = {"t": 10.0}  # constructed; been at zero the whole time
    transport = FakeSerialTransport()
    motor = SerialMotorController(
        transport, reverse_delay_s=0.9, time_fn=lambda: clock["t"]
    )
    # First command is reverse from a standstill: no prior direction, allowed.
    motor.apply(MotorCommand(thrust=-1.0, steering=0.0))
    await motor.flush()
    assert transport.written[-1] == "CMD 255 R 0"


async def test_no_delay_for_same_direction_changes() -> None:
    clock = {"t": 0.0}
    transport = FakeSerialTransport()
    motor = SerialMotorController(
        transport, reverse_delay_s=0.9, time_fn=lambda: clock["t"]
    )
    motor.apply(MotorCommand(thrust=0.5, steering=0.0))
    await motor.flush()
    motor.apply(MotorCommand(thrust=1.0, steering=0.0))
    await motor.flush()  # same direction, no time advance -> allowed
    assert transport.written == ["CMD 128 F 0", "CMD 255 F 0"]


async def test_reverse_delay_not_bypassed_through_zero() -> None:
    """A zero-thrust tick between opposing thrusts must NOT bypass the delay.

    The sequence +1 → 0 (one ~200 ms PID tick) → -1 used to bypass the
    interlock because the zero-thrust branch cleared _last_dir.  The delay
    must count from the first zero tick, and the opposite sign must remain
    blocked until the full delay has elapsed.
    """
    clock = {"t": 0.0}
    transport = FakeSerialTransport()
    motor = SerialMotorController(
        transport, reverse_delay_s=0.9, time_fn=lambda: clock["t"]
    )

    # Drive forward.
    motor.apply(MotorCommand(thrust=1.0, steering=0.0))
    await motor.flush()
    assert transport.written[-1] == "CMD 255 F 0"

    # One zero tick at t=0.2 (simulates PID crossing zero).
    clock["t"] = 0.2
    motor.apply(MotorCommand(thrust=0.0, steering=0.0))
    await motor.flush()
    assert transport.written[-1] == "CMD 0 F 0"

    # Immediately request reverse after a single zero tick — must still be blocked.
    clock["t"] = 0.4
    motor.apply(MotorCommand(thrust=-1.0, steering=0.0))
    await motor.flush()
    assert transport.written[-1] == "CMD 0 F 0", (
        "reverse delay was bypassed by a single zero-thrust tick"
    )

    # Still within the cooldown (0.4 - 0.2 = 0.2 s elapsed of 0.9 s required).
    clock["t"] = 0.8
    await motor.flush()
    assert transport.written[-1] == "CMD 0 F 0"

    # Delay elapsed from the first zero tick (0.2 + 0.9 = 1.1 s) → reverse allowed.
    clock["t"] = 1.2
    await motor.flush()
    assert transport.written[-1] == "CMD 255 R 0"


# --------------------------------------------------------------------------- #
# Garbage / oversized line survival
# --------------------------------------------------------------------------- #
async def test_sensor_read_loop_survives_garbage_line() -> None:
    """A ValueError/LimitOverrunError from a garbage line must not kill the loop.

    Real hardware: wrong-baud binary data with no newline overflows the asyncio
    StreamReader's 64 KB limit → LimitOverrunError or ValueError.  The loop
    must discard the bad line, log a warning, and continue processing valid
    lines.
    """
    bus = EventBus()
    received: list[str] = []
    bus.subscribe(events.NMEA_IN, received.append)

    transport = FakeSerialTransport()
    # Inject a simulated garbage-line exception (mirrors LimitOverrunError on
    # real hardware), then a normal NMEA sentence.
    transport.feed_exception(ValueError("line too long / no newline in 64 KB"))
    transport.feed("$GPGGA,valid,line")

    gps = SerialGps(transport, bus)
    await gps.start()
    for _ in range(30):
        await asyncio.sleep(0)
        if received:
            break
    await gps.stop()

    assert received == ["$GPGGA,valid,line"], (
        "sensor read loop died on garbage; subsequent valid lines were lost"
    )


async def test_sensor_read_loop_survives_multiple_garbage_then_eof() -> None:
    """Multiple garbage injections followed by EOF must exit cleanly."""
    bus = EventBus()
    received: list[str] = []
    bus.subscribe(events.NMEA_IN, received.append)

    transport = FakeSerialTransport()
    transport.feed_exception(ValueError("overrun 1"))
    transport.feed_exception(ValueError("overrun 2"))
    transport.feed("$GPHDT,270.0,T*hh")
    transport.feed_eof()

    gps = SerialGps(transport, bus)
    await gps.start()
    for _ in range(30):
        await asyncio.sleep(0)
        if len(received) >= 1:
            break
    await gps.stop()

    assert received == ["$GPHDT,270.0,T*hh"]


# --------------------------------------------------------------------------- #
# Automatic reconnect (unplug / replug survival)
# --------------------------------------------------------------------------- #
async def _pump(predicate, limit: int = 200) -> None:
    """Yield to the event loop until ``predicate()`` is true (or ``limit``)."""
    for _ in range(limit):
        await asyncio.sleep(0)
        if predicate():
            return


async def test_sensor_reconnects_after_eof_and_resumes() -> None:
    """EOF must not kill the sensor: it reconnects and later lines flow."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    bus = EventBus()
    received: list[str] = []
    bus.subscribe(events.NMEA_IN, received.append)

    transport = FakeSerialTransport()
    transport.feed("$GPGGA,before")
    transport.feed_eof()  # stream drops after the first line

    gps = SerialGps(transport, bus, sleep=fake_sleep)
    await gps.start()
    await _pump(lambda: gps.healthy and received == ["$GPGGA,before"])
    assert gps.healthy is True  # reconnected

    # A line fed after the reconnect must be picked up on the re-opened link.
    transport.feed("$GPGGA,after")
    await _pump(lambda: received == ["$GPGGA,before", "$GPGGA,after"])
    await gps.stop()

    assert received == ["$GPGGA,before", "$GPGGA,after"]
    assert transport.open_calls == 2  # initial open + one reconnect


async def test_sensor_reconnect_backoff_grows() -> None:
    """Repeated reopen failures back off exponentially (via injected sleep)."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    bus = EventBus()
    transport = FakeSerialTransport()
    gps = SerialGps(transport, bus, sleep=fake_sleep, backoff_start=1.0, backoff_max=15.0)
    await gps.start()  # initial open succeeds

    transport.fail_opens(3)  # next three reconnect opens fail, fourth succeeds
    transport.feed_eof()
    await _pump(lambda: gps.healthy and len(sleeps) >= 4)
    await gps.stop()

    assert gps.healthy is True
    assert sleeps[:4] == [1.0, 2.0, 4.0, 8.0]  # 1 → 2 → 4 → 8


async def test_sensor_backoff_caps_at_max() -> None:
    """Backoff saturates at backoff_max rather than growing unbounded."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    bus = EventBus()
    transport = FakeSerialTransport()
    gps = SerialGps(transport, bus, sleep=fake_sleep, backoff_start=1.0, backoff_max=5.0)
    await gps.start()
    transport.fail_opens(6)
    transport.feed_eof()
    await _pump(lambda: gps.healthy)
    await gps.stop()
    # 1, 2, 4, then capped at 5, 5, 5 for the remaining failures.
    assert sleeps[:6] == [1.0, 2.0, 4.0, 5.0, 5.0, 5.0]


async def test_sensor_healthy_transitions_false_then_true() -> None:
    """healthy is False while backing off, True again after the reopen."""
    gate = asyncio.Event()

    async def gated_sleep(delay: float) -> None:
        await gate.wait()  # hold the backoff open until the test releases it

    bus = EventBus()
    transport = FakeSerialTransport()
    gps = SerialGps(transport, bus, sleep=gated_sleep)
    await gps.start()
    await _pump(lambda: gps.healthy is True)  # let the read loop come up
    assert gps.healthy is True

    transport.feed_eof()
    await _pump(lambda: gps.healthy is False)
    assert gps.healthy is False  # disconnected, waiting in backoff

    gate.set()  # release the backoff → reopen succeeds
    await _pump(lambda: gps.healthy is True)
    assert gps.healthy is True
    await gps.stop()


async def test_stop_during_backoff_exits_promptly() -> None:
    """stop() must unblock a loop parked in a (would-be-forever) backoff wait."""
    gate = asyncio.Event()  # never set: a naive sleep would hang forever

    async def gated_sleep(delay: float) -> None:
        await gate.wait()

    bus = EventBus()
    transport = FakeSerialTransport()
    gps = SerialGps(transport, bus, sleep=gated_sleep)
    await gps.start()
    transport.feed_eof()
    await _pump(lambda: gps.healthy is False)

    # Must return without waiting on the (never-released) gate.
    await asyncio.wait_for(gps.stop(), timeout=1.0)
    assert transport.closed


async def test_last_data_monotonic_stamped_on_read() -> None:
    """last_data_monotonic advances as lines arrive; None before any."""
    bus = EventBus()
    transport = FakeSerialTransport()
    gps = SerialGps(transport, bus)
    assert gps.last_data_monotonic is None
    transport.feed("$GPGGA,x")
    await gps.start()
    await _pump(lambda: gps.last_data_monotonic is not None)
    assert gps.last_data_monotonic is not None
    await gps.stop()


# --------------------------------------------------------------------------- #
# Motor reconnect + write-while-down survival
# --------------------------------------------------------------------------- #
async def test_motor_flush_while_down_does_not_raise_and_recovers() -> None:
    """A write while the transport is down is dropped, not raised, and recovers."""
    transport = FakeSerialTransport()
    motor = SerialMotorController(transport, time_fn=lambda: 0.0)
    await motor.start()

    transport.fail_writes = True  # transport has gone down under the write path
    motor.apply(MotorCommand(thrust=0.5, steering=0.0))
    await motor.flush()  # must NOT raise
    assert motor.healthy is False
    assert transport.written == []  # command dropped, not queued

    transport.fail_writes = False  # link back
    await motor.flush()
    assert transport.written == ["CMD 128 F 0"]
    await motor.stop()


async def test_motor_feedback_reconnects_after_eof() -> None:
    """The motor's feedback loop reconnects and keeps decoding after a drop."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    transport = FakeSerialTransport()
    motor = SerialMotorController(transport, sleep=fake_sleep)
    await motor.start()

    transport.feed("A 1.0 1 1")
    await _pump(lambda: motor.last_feedback == SteeringFeedback(1.0, True, 1.0))

    transport.feed_eof()  # link drops
    await _pump(lambda: motor.healthy and transport.open_calls == 2)
    assert motor.healthy is True

    transport.feed("A 2.0 0 5")  # arrives on the reconnected link
    await _pump(lambda: motor.last_feedback == SteeringFeedback(2.0, False, 5.0))
    assert motor.last_feedback == SteeringFeedback(2.0, False, 5.0)
    await motor.stop()
