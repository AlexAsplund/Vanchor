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
from vanchor.hardware.serial_devices import (
    SerialCompass,
    SerialGps,
    SerialMotorController,
)
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


async def test_start_stop_refcounted_for_dual_server_lifespan():
    # The CLI serves ONE app over two uvicorn servers (HTTP + HTTPS), so the
    # FastAPI lifespan runs twice -> start()/stop() are each called twice. The
    # runtime must boot once (no double controller/simulator/serial-reader) and
    # tear down only when the LAST server shuts down.
    rt, transport, motor = _serial_motor_runtime()
    await rt.start()
    n_tasks = len(rt._tasks)
    sim_task = rt._sim_task
    assert transport.opened

    await rt.start()                       # second server's lifespan
    assert rt._start_count == 2
    assert len(rt._tasks) == n_tasks        # no duplicate controller/sim/supervisor tasks
    assert rt._sim_task is sim_task         # simulator not restarted

    await rt.stop()                        # first server shuts down -> still up
    assert rt._start_count == 1
    assert not transport.closed             # motor still live
    assert not sim_task.done()

    await rt.stop()                        # last server shuts down -> real teardown
    assert rt._start_count == 0
    assert transport.closed
    assert "CMD 0 F 0" in transport.written


async def test_gps_and_compass_on_same_serial_port_share_one_reader():
    # A combo NMEA source (RMC/GGA + HDG on one port) configured as both the
    # serial GPS and the serial compass must open the port ONCE and read it with
    # a single reader -- not two readers double-reading + double-publishing.
    cfg = load(None)
    cfg.hardware.gps_source = "serial"
    cfg.hardware.compass_source = "serial"
    cfg.hardware.gps_port = cfg.hardware.compass_port = "/dev/ttyTEST"
    gtx, ctx = FakeSerialTransport(), FakeSerialTransport()
    with patch.object(Runtime, "_build_serial_gps", lambda self, c: SerialGps(gtx, self.bus)), \
         patch.object(Runtime, "_build_serial_compass", lambda self, c: SerialCompass(ctx, self.bus)):
        rt = Runtime(cfg)
    # Same port -> the compass slot reuses the GPS reader instance.
    assert rt.gps is rt.compass
    await rt.start()
    try:
        assert gtx.open_calls == 1   # the shared port opened exactly once...
        assert ctx.open_calls == 0   # ...and the compass's own transport was never used
    finally:
        await rt.stop()
    assert gtx.closed                # shared reader closed once, no double-close error


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
