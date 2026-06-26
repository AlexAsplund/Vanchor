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
