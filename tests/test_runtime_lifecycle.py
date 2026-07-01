"""Runtime device lifecycle wiring for the MOTOR (#64 safety floor + #: the
motor must be opened on start, closed on stop, and retired on a live reload).

Regression guard: ``Runtime.start()`` used to open only gps/compass/depth, so a
``motor_source: serial`` boat never opened its transport and the first ``flush()``
raised; ``Runtime.stop()`` never sent the shutdown CMD 0; and a device reload
swapped the motor without stopping the old one (port + feedback-task leak).

None of these tests open a physical serial port -- they drive an in-memory
``FakeSerialTransport`` patched in via ``_build_serial_motor``.
"""

from __future__ import annotations

from unittest.mock import patch

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.hardware.serial_devices import SerialMotorController
from vanchor.hardware.serial_link import FakeSerialTransport


def _serial_motor_runtime():
    """A full-sim boat whose MOTOR is a serial controller over a fake transport."""
    cfg = load(None)
    cfg.hardware.motor_source = "serial"
    transport = FakeSerialTransport()
    motor = SerialMotorController(transport)
    # Patch only around construction; reload builds its own motors from config.
    with patch.object(Runtime, "_build_serial_motor", lambda self, c: motor):
        rt = Runtime(cfg)
    assert rt.controller.motor is motor
    return rt, transport, motor


async def test_start_opens_serial_motor():
    rt, transport, motor = _serial_motor_runtime()
    assert not transport.opened
    await rt.start()
    try:
        # start() opened the transport and spun up the feedback reader, so the
        # first flush() won't raise on a never-opened port.
        assert transport.opened
        assert motor._feedback_task is not None
    finally:
        await rt.stop()


async def test_stop_closes_and_stops_serial_motor():
    rt, transport, motor = _serial_motor_runtime()
    await rt.start()
    await rt.stop()
    # stop() retired the motor: port closed + feedback task cleared, and the
    # best-effort shutdown CMD 0 was written before the close.
    assert transport.closed
    assert motor._feedback_task is None
    assert "CMD 0 F 0" in transport.written


async def test_reload_devices_stops_old_motor():
    rt, transport, motor = _serial_motor_runtime()
    await rt.start()
    assert transport.opened

    # Live-reload to a full-sim motor: the old serial motor must be retired.
    rt.config.hardware.motor_source = "sim"
    res = await rt.reload_devices()
    assert res["applied"]
    assert transport.closed                # old serial port closed
    assert motor._feedback_task is None     # its feedback task killed (no leak)
    assert rt.controller.motor is not motor  # swapped to the new (sim) motor

    await rt.stop()
