"""In-process emulation of the helm-Pico I²C register machine.

Provides :class:`TunnelEmulator` — a faithful software implementation of the
I²C register map described in ``I2C-TUNNEL.md`` §2/§3 — so that
:class:`~vanchor.hardware.i2c_link.I2cTransport` can be tested end-to-end
without any physical hardware.

A thin *line responder* on top of the register machine parses incoming CMD
lines (CRC-verified with the real :func:`~vanchor.hardware.serial_link.strip_verify_crc`),
queues ``A <angle> 1 0.0 <seq>`` feedback replies, and exposes the
received/rejected lists for test assertions.

Use :func:`make_emulator_factory` to wire the emulator into an
:class:`~vanchor.hardware.i2c_link.I2cTransport` via its ``smbus_factory``
constructor parameter — the same pattern as :func:`make_factory` in
``tests/test_i2c_link.py``.
"""

from __future__ import annotations

import collections
from typing import Any

from vanchor.hardware.i2c_link import _ReadMsg, _WriteMsg
from vanchor.hardware.serial_link import append_crc, strip_verify_crc

# Register map (I2C-TUNNEL.md §2)
_REG_WHOAMI  = 0x00
_REG_VERSION = 0x01
_REG_TXA_L   = 0x02
_REG_TXA_H   = 0x03
_REG_RXF_L   = 0x04
_REG_RXF_H   = 0x05
_REG_FLAGS   = 0x06
_REG_DATA    = 0x10

_RX_FIFO_CAP = 256     # bytes (I2C-TUNNEL.md §2)
_TX_FIFO_CAP = 1024    # bytes (I2C-TUNNEL.md §2)

FLAG_RX_OVERFLOW = 0x01
FLAG_TX_OVERFLOW = 0x02


class TunnelEmulator:
    """In-process emulation of the helm-Pico I²C register machine.

    Implements the register semantics from I2C-TUNNEL.md §2/§3 faithfully:

    * **Pointer byte**: first byte of each write transaction selects the
      register; pointer survives until overwritten.
    * **Auto-increment**: reading registers 0x00–0x06 increments the pointer
      after each byte read; register 0x10 (DATA) does *not* auto-increment.
    * **TXA latch**: reading TXA_L (0x02) atomically captures the TX FIFO
      depth into a latch; TXA_H (0x03) returns the captured high byte.
    * **RXF latch**: symmetric to TXA for RX free-space registers.
    * **DATA reads** drain bytes from the TX FIFO; 0x00 filler when empty.
    * **DATA writes** push bytes into the RX FIFO (fed to the line responder).
    * **FLAGS** (0x06): clear-on-read; bit0 = RX overflow, bit1 = TX overflow.
    * **Writes to RO regs** are silently ignored (pointer is still updated).
    * **TX FIFO**: all-or-nothing line queuing — if a line won't fit it is
      dropped and FLAG_TX_OVERFLOW is set.
    * **RX FIFO**: overflow → FLAG_RX_OVERFLOW; excess bytes are dropped.

    Line responder
    ~~~~~~~~~~~~~~
    Incoming DATA bytes are scanned for newline-terminated lines.  Each
    complete line is CRC-verified.  Valid ``CMD`` lines are stored in
    :attr:`received_lines` and trigger a ``A <angle> 1 0.0 <seq>`` reply
    (CRC'd, queued all-or-nothing into the TX FIFO, echoing the CMD's
    heartbeat seq or -1).  Lines with a bad CRC go to :attr:`rejected_lines`.

    Introspection
    ~~~~~~~~~~~~~
    ``received_lines``  list of raw CRC-valid CMD lines the emulator accepted.
    ``rejected_lines``  list of lines whose CRC failed.
    ``flags``           current FLAGS value (read-only property; does not clear).

    Die switch
    ~~~~~~~~~~
    :meth:`kill` makes every subsequent ``i2c_rdwr`` raise ``OSError``,
    simulating a dead/NAK-ing bus for reconnect tests.
    """

    def __init__(self, *, fixed_angle: float = 45.0) -> None:
        self._reg_ptr: int = 0x00
        # RX FIFO: bytes written by the Pi master (CMD bytes in)
        self._rx_fifo: collections.deque[int] = collections.deque()
        # TX FIFO: bytes destined for the Pi master (A/E/C lines out)
        self._tx_fifo: collections.deque[int] = collections.deque()
        self._flags: int = 0
        self._txa_latch: int = 0          # TX depth snapshot latched at TXA_L read
        self._rxf_latch: int = _RX_FIFO_CAP  # RX free-space snapshot
        self._fixed_angle = fixed_angle
        # Introspection
        self.received_lines: list[str] = []
        self.rejected_lines: list[str] = []
        # Die switch
        self._dead: Exception | None = None

    # -- die switch --------------------------------------------------------- #

    def kill(self, exc: Exception | None = None) -> None:
        """Make all subsequent i2c_rdwr calls raise *exc* (default ``OSError``)."""
        self._dead = exc or OSError("TunnelEmulator killed")

    # -- flags property (non-clearing introspection) ------------------------ #

    @property
    def flags(self) -> int:
        """Current FLAGS register value, without the clear-on-read side-effect."""
        return self._flags

    # -- smbus2-compatible interface ---------------------------------------- #

    def i2c_rdwr(self, *msgs: Any) -> None:
        """Process an I²C multi-message transaction (mirrors smbus2.SMBus.i2c_rdwr)."""
        if self._dead is not None:
            raise self._dead
        for msg in msgs:
            if isinstance(msg, _WriteMsg):
                self._handle_write(bytes(msg))
            elif isinstance(msg, _ReadMsg):
                self._handle_read(msg)

    def close(self) -> None:
        """No-op: the emulator has no physical resource to release."""

    # -- register machine --------------------------------------------------- #

    def _handle_write(self, data: bytes) -> None:
        """First byte sets the register pointer; remaining bytes go to DATA or are ignored."""
        if not data:
            return
        self._reg_ptr = data[0]
        if self._reg_ptr == _REG_DATA and len(data) > 1:
            payload = data[1:]
            space = _RX_FIFO_CAP - len(self._rx_fifo)
            if len(payload) > space:
                # Overflow: accept up to capacity, drop the rest, set flag.
                self._rx_fifo.extend(payload[:space])
                self._flags |= FLAG_RX_OVERFLOW
            else:
                self._rx_fifo.extend(payload)
            self._process_rx()
        # Writes to read-only registers 0x00–0x06 are silently ignored
        # (the pointer has already been updated above).

    def _handle_read(self, msg: _ReadMsg) -> None:
        """Fill msg._buf from the register machine; auto-increment 0x00–0x06."""
        ptr = self._reg_ptr
        for i in range(msg.length):
            msg._buf[i] = self._read_reg(ptr)
            # DATA (0x10) does not auto-increment; all other defined regs do for
            # 0x00–0x06 (per spec); unknown regs also do not increment.
            if 0x00 <= ptr <= 0x06:
                ptr += 1
        self._reg_ptr = ptr

    def _read_reg(self, reg: int) -> int:
        """Return the byte value for a single register access."""
        if reg == _REG_WHOAMI:
            return 0x56
        if reg == _REG_VERSION:
            return 0x01
        if reg == _REG_TXA_L:
            self._txa_latch = len(self._tx_fifo)
            return self._txa_latch & 0xFF
        if reg == _REG_TXA_H:
            return (self._txa_latch >> 8) & 0xFF
        if reg == _REG_RXF_L:
            self._rxf_latch = _RX_FIFO_CAP - len(self._rx_fifo)
            return self._rxf_latch & 0xFF
        if reg == _REG_RXF_H:
            return (self._rxf_latch >> 8) & 0xFF
        if reg == _REG_FLAGS:
            val = self._flags
            self._flags = 0   # clear on read
            return val
        if reg == _REG_DATA:
            return self._tx_fifo.popleft() if self._tx_fifo else 0x00
        return 0x00   # unknown / reserved registers

    # -- RX line responder -------------------------------------------------- #

    def _process_rx(self) -> None:
        """Scan the RX FIFO for complete lines and dispatch each."""
        # Convert deque→bytes once per call; the FIFO is at most 256 B.
        buf = bytes(self._rx_fifo)
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.replace(b"\r", b"").decode("ascii", errors="replace").strip()
            if line:
                self._respond_to(line)
        self._rx_fifo = collections.deque(buf)

    def _respond_to(self, line: str) -> None:
        """CRC-check one complete line and reply if it is a valid CMD."""
        payload, crc_ok = strip_verify_crc(line)
        if crc_ok is False:
            self.rejected_lines.append(line)
            return
        parts = payload.split()
        if not parts or parts[0] != "CMD":
            return   # CONF* and other lines are silently consumed
        self.received_lines.append(line)
        # Extract optional heartbeat seq: CMD <pwm> <dir> <steer> [<seq>]
        seq = -1
        if len(parts) >= 5:
            try:
                seq = int(parts[4])
            except ValueError:
                seq = -1
        # Queue A feedback reply (CRC'd, echoing seq)
        a_line = append_crc(f"A {self._fixed_angle} 1 0.0 {seq}")
        self._queue_tx_line(a_line)

    def _queue_tx_line(self, line: str) -> bool:
        """Add *line*\\r\\n to the TX FIFO (all-or-nothing per spec).

        Returns ``True`` on success.  Returns ``False`` and sets
        FLAG_TX_OVERFLOW if there is not enough room (line is dropped).
        """
        frame = line.encode("ascii") + b"\r\n"
        if len(self._tx_fifo) + len(frame) > _TX_FIFO_CAP:
            self._flags |= FLAG_TX_OVERFLOW
            return False
        self._tx_fifo.extend(frame)
        return True


# --------------------------------------------------------------------------- #
# Factory helper
# --------------------------------------------------------------------------- #

def make_emulator_factory(emulator: TunnelEmulator):
    """Return an ``smbus_factory`` callable that wires *emulator* into I2cTransport.

    Usage::

        em = TunnelEmulator()
        t = I2cTransport(3, smbus_factory=make_emulator_factory(em))

    Follows the same pattern as :func:`make_factory` in ``tests/test_i2c_link.py``.
    """
    def factory(bus_num: int):  # noqa: ARG001
        return emulator, lambda data: _WriteMsg(data), lambda n: _ReadMsg(n)
    return factory
