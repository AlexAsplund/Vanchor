"""Tests for make_motor_transport() in src/vanchor/hardware/i2c_link.py.

Covers:
* Non-i2c port → PySerialTransport (kwargs forwarded).
* i2c:3:0x42 (good), hex addr, decimal addr, default addr (omitted).
* Bus=0 edge case.
* Malformed schemes → ValueError with a message naming i2c.
* Serial kwargs ignored for i2c: port → DEBUG log emitted.
* link_plan._normalize_port leaves i2c: strings unchanged.
* App-level: Runtime built with motor_source="serial", motor_port="i2c:3:0x42"
  produces a SerialMotorController whose .transport is an I2cTransport.
  open() is never called at construction — no bus IO.
"""

from __future__ import annotations

import logging
import tempfile

import pytest

from vanchor.hardware.i2c_link import I2cTransport, make_motor_transport
from vanchor.hardware.serial_link import PySerialTransport


# ─────────────────────────────────────────────────────────────────────────── #
# Non-i2c → PySerialTransport                                                 #
# ─────────────────────────────────────────────────────────────────────────── #

def test_non_i2c_returns_pyserial():
    t = make_motor_transport("/dev/ttyUSB0", baudrate=115200)
    assert isinstance(t, PySerialTransport)
    assert t.port == "/dev/ttyUSB0"
    assert t.baudrate == 115200


def test_non_i2c_com_port_returns_pyserial():
    """Windows-style COM ports should also go through PySerialTransport."""
    t = make_motor_transport("COM3", baudrate=9600, bytesize=7)
    assert isinstance(t, PySerialTransport)
    assert t.port == "COM3"
    assert t.bytesize == 7


# ─────────────────────────────────────────────────────────────────────────── #
# Good i2c: schemes                                                           #
# ─────────────────────────────────────────────────────────────────────────── #

def test_i2c_hex_addr():
    t = make_motor_transport("i2c:3:0x42")
    assert isinstance(t, I2cTransport)
    assert t._bus_num == 3
    assert t._addr == 0x42


def test_i2c_decimal_addr():
    t = make_motor_transport("i2c:2:66")
    assert isinstance(t, I2cTransport)
    assert t._bus_num == 2
    assert t._addr == 66   # 0x42 == 66


def test_i2c_default_addr_when_omitted():
    """i2c:<bus> with no addr → default 0x42."""
    t = make_motor_transport("i2c:3")
    assert isinstance(t, I2cTransport)
    assert t._bus_num == 3
    assert t._addr == 0x42


def test_i2c_bus_zero():
    t = make_motor_transport("i2c:0")
    assert isinstance(t, I2cTransport)
    assert t._bus_num == 0
    assert t._addr == 0x42


def test_i2c_non_default_hex_addr():
    t = make_motor_transport("i2c:1:0x20")
    assert isinstance(t, I2cTransport)
    assert t._bus_num == 1
    assert t._addr == 0x20


# ─────────────────────────────────────────────────────────────────────────── #
# Malformed i2c: schemes → ValueError                                         #
# ─────────────────────────────────────────────────────────────────────────── #

@pytest.mark.parametrize("bad_port", [
    "i2c:",          # missing bus
    "i2c:abc",       # non-integer bus
    "i2c:3:xyz",     # non-integer addr
    "i2c:-1",        # negative bus
    "i2c::0x42",     # empty bus segment
])
def test_malformed_i2c_raises_valueerror(bad_port):
    with pytest.raises(ValueError, match="i2c"):
        make_motor_transport(bad_port)


# ─────────────────────────────────────────────────────────────────────────── #
# Serial kwargs ignored for i2c ports                                         #
# ─────────────────────────────────────────────────────────────────────────── #

def test_serial_kwargs_ignored_no_error(caplog):
    """Serial kwargs are silently dropped; a DEBUG message is emitted."""
    with caplog.at_level(logging.DEBUG, logger="vanchor.hardware.i2c"):
        t = make_motor_transport("i2c:3:0x42", baudrate=115200, bytesize=8,
                                 parity="N", stopbits=1.0)
    assert isinstance(t, I2cTransport)
    # At least one DEBUG record should mention "ignored"
    debug_texts = " ".join(r.message for r in caplog.records
                           if r.levelno == logging.DEBUG)
    assert "ignored" in debug_texts.lower()


def test_serial_kwargs_not_logged_when_absent(caplog):
    """No DEBUG log when no serial kwargs are given."""
    with caplog.at_level(logging.DEBUG, logger="vanchor.hardware.i2c"):
        make_motor_transport("i2c:3")
    # Should not emit an "ignored" message when kwargs are empty
    debug_texts = " ".join(r.message for r in caplog.records
                           if r.levelno == logging.DEBUG)
    assert "ignored" not in debug_texts.lower()


# ─────────────────────────────────────────────────────────────────────────── #
# link_plan._normalize_port leaves i2c: strings unchanged                     #
# ─────────────────────────────────────────────────────────────────────────── #

def test_normalize_port_leaves_i2c_unchanged():
    """_normalize_port must not mangle or reject i2c: port strings."""
    from vanchor.hardware.link_plan import _normalize_port
    assert _normalize_port("i2c:3:0x42") == "i2c:3:0x42"
    assert _normalize_port("i2c:0") == "i2c:0"
    assert _normalize_port("i2c:1:0x20") == "i2c:1:0x20"


# ─────────────────────────────────────────────────────────────────────────── #
# App-level: Runtime with i2c motor_port builds I2cTransport                 #
# ─────────────────────────────────────────────────────────────────────────── #

def test_runtime_i2c_motor_port_builds_i2ctransport():
    """A Runtime with motor_source=serial + motor_port=i2c:3:0x42 constructs a
    SerialMotorController whose .transport is an I2cTransport.

    open() is NOT called at construction time — the supervisor only opens the
    transport when the event loop starts (runtime.run()).  So this test is
    bus-IO-free and needs no smbus2 install.
    """
    from vanchor.app import Runtime
    from vanchor.core.config import load
    from vanchor.hardware.serial_devices import SerialMotorController

    cfg = load(None)
    cfg.data_dir = tempfile.mkdtemp(prefix="vanchor-i2c-rt-")
    cfg.hardware.motor_source = "serial"
    cfg.hardware.motor_port = "i2c:3:0x42"

    rt = Runtime(cfg)
    motor = rt.controller.motor

    assert isinstance(motor, SerialMotorController), (
        f"expected SerialMotorController, got {type(motor).__name__}"
    )
    assert isinstance(motor.transport, I2cTransport), (
        f"expected I2cTransport on .transport, got {type(motor.transport).__name__}"
    )
    # Verify the parsed values are correct
    assert motor.transport._bus_num == 3
    assert motor.transport._addr == 0x42
