"""End-to-end I²C motor-controller integration tests.

Proves that the *existing* :class:`~vanchor.hardware.serial_devices.SerialMotorController`
closes the full I²C loop — CMD out, A feedback in — with **zero protocol-code
changes**.  Only the transport changes (I2cTransport instead of
PySerialTransport); everything above is byte-identical.

Two test suites in one file
~~~~~~~~~~~~~~~~~~~~~~~~~~~
1. :class:`TestEmulatorSpec` — spec-fidelity unit tests for
   :class:`~tunnel_emulator.TunnelEmulator` itself.  The emulator is only
   trustworthy when its own register machine is verified against
   I2C-TUNNEL.md §2/§3.  Tests cover: register pointer, auto-increment
   range, TXA latch semantics, DATA no-auto-increment, 0x00 filler on
   over-read, FLAGS clear-on-read, TX all-or-nothing overflow, RX overflow.

2. :class:`TestI2cMotorE2e` — integration tests:
   (a) flush() → emulator sees a CRC-valid CMD with correct pwm/dir/steer.
   (b) Tunneled A reply lands in controller.last_feedback (full loop).
   (c) Heartbeat seq echoes round-trip; controller health tracks it.
   (d) Emulator death → EOFError → supervisor reconnect path (open() called
       again via the factory seam, mirroring test_i2c_link's reconnect test).
"""

from __future__ import annotations

import asyncio
import os
import sys

# tunnel_emulator.py lives alongside this file; make it importable regardless
# of how pytest is invoked (sys.path includes the project root, not tests/).
sys.path.insert(0, os.path.dirname(__file__))

from tunnel_emulator import (  # noqa: E402
    FLAG_RX_OVERFLOW,
    FLAG_TX_OVERFLOW,
    TunnelEmulator,
    _REG_DATA,
    _RX_FIFO_CAP,
    _TX_FIFO_CAP,
    make_emulator_factory,
)
from vanchor.core.models import MotorCommand
from vanchor.hardware.i2c_link import I2cTransport, _ReadMsg, _WriteMsg
from vanchor.hardware.serial_devices import SerialMotorController
from vanchor.hardware.serial_link import append_crc, strip_verify_crc


# --------------------------------------------------------------------------- #
# Shared test helpers
# --------------------------------------------------------------------------- #

async def instant_sleep(delay: float) -> None:
    """Fake asyncio.sleep: yields to the event loop once then returns.

    Keeps tests off the wall clock and makes poll-loop / backoff iterations
    virtually instant.
    """
    await asyncio.sleep(0)


async def eventually(condition, *, timeout: float = 5.0) -> None:
    """Yield to the event loop until *condition()* is True, or raise TimeoutError."""
    async def _wait() -> None:
        while not condition():
            await asyncio.sleep(0)
    await asyncio.wait_for(_wait(), timeout=timeout)


def _make_transport(emulator: TunnelEmulator) -> I2cTransport:
    """Build an I2cTransport backed by *emulator* with instant sleep."""
    return I2cTransport(
        3,
        smbus_factory=make_emulator_factory(emulator),
        sleep=instant_sleep,
    )


def _write(data: bytes) -> _WriteMsg:
    return _WriteMsg(data)


def _read(n: int) -> _ReadMsg:
    return _ReadMsg(n)


# --------------------------------------------------------------------------- #
# Spec-fidelity unit tests for TunnelEmulator
# --------------------------------------------------------------------------- #

class TestEmulatorSpec:
    """Verify TunnelEmulator implements I2C-TUNNEL.md §2/§3 faithfully.

    These tests exercise only the register machine directly (no asyncio,
    no I2cTransport) so each spec property is isolated.
    """

    # -- probe: WHOAMI + VERSION via auto-increment ------------------------- #

    def test_probe_whoami_and_version(self) -> None:
        """W[0x00] R[2] → 0x56 then 0x01 via auto-increment."""
        em = TunnelEmulator()
        r = _read(2)
        em.i2c_rdwr(_write(b"\x00"), r)
        assert bytes(r) == b"\x56\x01"

    # -- auto-increment 0x00–0x06 ------------------------------------------ #

    def test_auto_increment_traverses_0_to_6(self) -> None:
        """Reading 7 bytes from 0x00 gives WHOAMI..FLAGS in order."""
        em = TunnelEmulator()
        r = _read(7)
        em.i2c_rdwr(_write(b"\x00"), r)
        data = bytes(r)
        assert data[0] == 0x56   # WHOAMI
        assert data[1] == 0x01   # VERSION
        # regs 2-5 are TXA/RXF; 6 is FLAGS — all zero for a fresh emulator
        assert data[2] == 0x00   # TXA_L (empty TX FIFO)
        assert data[6] == 0x00   # FLAGS (no overflows)

    def test_auto_increment_does_not_wrap_past_0x06(self) -> None:
        """After reading FLAGS (0x06), pointer advances to 0x07 (unknown → 0x00)
        and stays there (does not keep auto-incrementing)."""
        em = TunnelEmulator()
        r = _read(3)
        em.i2c_rdwr(_write(b"\x06"), r)   # start at FLAGS
        data = bytes(r)
        assert data[0] == 0x00   # FLAGS (empty)
        assert data[1] == 0x00   # reg 0x07 → unknown → 0x00
        assert data[2] == 0x00   # still reg 0x07 (ptr stuck, no increment)
        assert em._reg_ptr == 0x07

    # -- TXA latch: reading TXA_L latches TXA_H ----------------------------- #

    def test_txa_latch_atomically_captures_count(self) -> None:
        """W[0x02] R[2] → lo|hi reconstructs the TX FIFO depth correctly."""
        em = TunnelEmulator()
        em._tx_fifo.extend(b"x" * 300)   # 300 B > 255: hi byte is non-zero

        r = _read(2)
        em.i2c_rdwr(_write(b"\x02"), r)
        lo, hi = bytes(r)
        assert (lo | (hi << 8)) == 300

    def test_txa_h_returns_latched_value_not_current_count(self) -> None:
        """TXA_H reflects the count latched when TXA_L was last read,
        even if the TX FIFO depth has changed since."""
        em = TunnelEmulator()
        em._tx_fifo.extend(b"x" * 300)

        # Read TXA_L+H (latches 300)
        r1 = _read(2)
        em.i2c_rdwr(_write(b"\x02"), r1)
        assert em._txa_latch == 300

        # Drain the TX FIFO — current depth is now 0
        em._tx_fifo.clear()

        # Reading TXA_H again via pointer 0x03 should still give latched hi byte
        r2 = _read(1)
        em.i2c_rdwr(_write(b"\x03"), r2)
        hi = bytes(r2)[0]
        lo = bytes(r1)[0]
        assert (lo | (hi << 8)) == 300   # latched value, not current 0

    # -- DATA does not auto-increment --------------------------------------- #

    def test_data_no_auto_increment(self) -> None:
        """Consecutive bytes from W[0x10] R[N] all come from the DATA FIFO."""
        em = TunnelEmulator()
        em._tx_fifo.extend(b"ABCDE")

        r = _read(5)
        em.i2c_rdwr(_write(b"\x10"), r)
        assert bytes(r) == b"ABCDE"
        # Pointer must remain at DATA (no increment)
        assert em._reg_ptr == 0x10

    # -- 0x00 filler on empty DATA ------------------------------------------ #

    def test_data_filler_when_tx_fifo_empty(self) -> None:
        """Reading DATA past available bytes returns 0x00 filler per spec."""
        em = TunnelEmulator()
        r = _read(4)
        em.i2c_rdwr(_write(b"\x10"), r)
        assert bytes(r) == b"\x00\x00\x00\x00"

    def test_data_partial_fill_then_filler(self) -> None:
        """Bytes 0..n-1 come from the FIFO; bytes n.. are 0x00 filler."""
        em = TunnelEmulator()
        em._tx_fifo.extend(b"AB")

        r = _read(4)
        em.i2c_rdwr(_write(b"\x10"), r)
        assert bytes(r) == b"AB\x00\x00"

    # -- FLAGS clear-on-read ------------------------------------------------ #

    def test_flags_clear_on_read(self) -> None:
        """FLAGS returns the current value and resets to 0x00 on the same read."""
        em = TunnelEmulator()
        em._flags = FLAG_RX_OVERFLOW | FLAG_TX_OVERFLOW

        r1 = _read(1)
        em.i2c_rdwr(_write(b"\x06"), r1)
        assert bytes(r1) == bytes([FLAG_RX_OVERFLOW | FLAG_TX_OVERFLOW])

        r2 = _read(1)
        em.i2c_rdwr(_write(b"\x06"), r2)
        assert bytes(r2) == b"\x00"   # cleared after first read

    def test_flags_clear_does_not_affect_introspection_property(self) -> None:
        """The .flags property shows the PRE-read value (no side-effect)."""
        em = TunnelEmulator()
        em._flags = FLAG_RX_OVERFLOW
        assert em.flags == FLAG_RX_OVERFLOW

        # Simulate what the transport does: W[0x06] R[1]
        r = _read(1)
        em.i2c_rdwr(_write(b"\x06"), r)

        # The flags register is now cleared
        assert em._flags == 0

    # -- writes to read-only registers are ignored -------------------------- #

    def test_write_to_ro_reg_ignored(self) -> None:
        """Writing 0xFF to VERSION (read-only, reg 0x01) leaves it unchanged."""
        em = TunnelEmulator()
        em.i2c_rdwr(_write(b"\x01\xFF"))   # set ptr to VERSION, try to write

        r = _read(1)
        em.i2c_rdwr(_write(b"\x01"), r)
        assert bytes(r) == b"\x01"   # VERSION is still 0x01

    def test_write_to_whoami_ignored(self) -> None:
        """Writing to WHOAMI (0x00) is silently ignored."""
        em = TunnelEmulator()
        em.i2c_rdwr(_write(b"\x00\xDE\xAD"))

        r = _read(1)
        em.i2c_rdwr(_write(b"\x00"), r)
        assert bytes(r) == b"\x56"

    # -- TX FIFO all-or-nothing overflow ------------------------------------ #

    def test_tx_overflow_line_dropped_and_flag_set(self) -> None:
        """A line that won't fit is dropped entirely; FLAG_TX_OVERFLOW is set."""
        em = TunnelEmulator()
        # Fill TX FIFO to within 3 bytes of capacity
        em._tx_fifo.extend(b"Z" * (_TX_FIFO_CAP - 3))
        depth_before = len(em._tx_fifo)

        # Try to queue a line whose frame (line + \r\n) is > 3 bytes
        result = em._queue_tx_line("OVERFLOW_LINE")   # 13+2 = 15 bytes; won't fit
        assert result is False
        assert em._flags & FLAG_TX_OVERFLOW
        # FIFO must be exactly as before (all-or-nothing)
        assert len(em._tx_fifo) == depth_before

    def test_tx_fits_exactly_at_capacity(self) -> None:
        """A line whose frame exactly fills the remaining space succeeds."""
        em = TunnelEmulator()
        # Pre-fill to leave room for exactly "A\r\n" (3 bytes)
        em._tx_fifo.extend(b"Z" * (_TX_FIFO_CAP - 3))

        result = em._queue_tx_line("A")   # 1 char + \r\n = 3 bytes
        assert result is True
        assert len(em._tx_fifo) == _TX_FIFO_CAP
        assert not (em._flags & FLAG_TX_OVERFLOW)

    def test_tx_one_byte_too_big_overflows(self) -> None:
        """A frame that is one byte too large for the remaining space overflows."""
        em = TunnelEmulator()
        em._tx_fifo.extend(b"Z" * (_TX_FIFO_CAP - 3))   # 3 bytes remaining

        result = em._queue_tx_line("AB")   # 2+2 = 4 bytes; won't fit in 3
        assert result is False
        assert em._flags & FLAG_TX_OVERFLOW

    # -- RX FIFO overflow --------------------------------------------------- #

    def test_rx_overflow_flag_set_on_overfill(self) -> None:
        """Writing more bytes than RX_FIFO_CAP via DATA sets FLAG_RX_OVERFLOW."""
        em = TunnelEmulator()
        # Payload: 1 (ptr byte) + 300 (data bytes); RX FIFO cap is 256.
        em.i2c_rdwr(_write(bytes([_REG_DATA]) + b"\x00" * 300))
        assert em._flags & FLAG_RX_OVERFLOW
        assert len(em._rx_fifo) <= _RX_FIFO_CAP

    def test_rx_overflow_partial_data_accepted(self) -> None:
        """The first RX_FIFO_CAP bytes are accepted even on overflow."""
        em = TunnelEmulator()
        # Fill almost to cap first
        em._rx_fifo.extend(b"\x00" * (_RX_FIFO_CAP - 5))
        # Now write 10 bytes; only 5 fit
        em.i2c_rdwr(_write(bytes([_REG_DATA]) + b"ABCDEFGHIJ"))
        assert len(em._rx_fifo) == _RX_FIFO_CAP
        assert em._flags & FLAG_RX_OVERFLOW

    # -- line responder ----------------------------------------------------- #

    def test_cmd_line_received_and_reply_queued(self) -> None:
        """Valid CMD line → recorded in received_lines, A reply in TX FIFO."""
        em = TunnelEmulator()
        cmd = append_crc("CMD 128 F 50 7")
        em.i2c_rdwr(_write(bytes([_REG_DATA]) + cmd.encode() + b"\r\n"))

        assert len(em.received_lines) == 1
        payload, crc_ok = strip_verify_crc(em.received_lines[0])
        assert crc_ok is True
        assert payload == "CMD 128 F 50 7"

        # TX FIFO must have an A reply echoing seq=7
        assert len(em._tx_fifo) > 0
        tx_text = bytes(em._tx_fifo).decode("ascii")
        assert tx_text.startswith("A")
        assert " 7" in tx_text   # seq echo

    def test_bad_crc_goes_to_rejected_lines(self) -> None:
        """A CMD line with a wrong CRC is placed in rejected_lines, no reply."""
        em = TunnelEmulator()
        bad = "CMD 128 F 50*00"   # 0x00 is certainly incorrect
        em.i2c_rdwr(_write(bytes([_REG_DATA]) + bad.encode() + b"\r\n"))

        assert len(em.rejected_lines) == 1
        assert len(em.received_lines) == 0
        assert len(em._tx_fifo) == 0   # no reply for rejected lines

    def test_no_crc_suffix_accepted_not_rejected(self) -> None:
        """A CMD line without *HH suffix (older firmware) is accepted, not rejected.

        strip_verify_crc returns (payload, None) for no-CRC lines, meaning
        "unknown, treat as valid" — matching the spec's backward-compat rule.
        """
        em = TunnelEmulator()
        em.i2c_rdwr(_write(bytes([_REG_DATA]) + b"CMD 0 F 0\r\n"))

        assert len(em.rejected_lines) == 0
        assert len(em.received_lines) == 1

    def test_seq_minus_one_when_no_heartbeat(self) -> None:
        """A CMD line without a seq field → reply echoes -1."""
        em = TunnelEmulator()
        cmd = append_crc("CMD 0 F 0")   # no seq field
        em.i2c_rdwr(_write(bytes([_REG_DATA]) + cmd.encode() + b"\r\n"))

        assert len(em.received_lines) == 1
        tx_text = bytes(em._tx_fifo).decode("ascii")
        # A line ends with " -1*HH\r\n"
        assert " -1" in tx_text

    def test_line_split_across_two_writes(self) -> None:
        """A CMD line split across two DATA writes is reassembled correctly."""
        em = TunnelEmulator()
        cmd = append_crc("CMD 255 R -100 3")
        half = len(cmd) // 2
        first_half = cmd[:half].encode()
        second_half = cmd[half:].encode() + b"\r\n"

        em.i2c_rdwr(_write(bytes([_REG_DATA]) + first_half))
        assert len(em.received_lines) == 0   # not complete yet

        em.i2c_rdwr(_write(bytes([_REG_DATA]) + second_half))
        assert len(em.received_lines) == 1
        payload, _ = strip_verify_crc(em.received_lines[0])
        assert payload == "CMD 255 R -100 3"


# --------------------------------------------------------------------------- #
# End-to-end integration tests
# --------------------------------------------------------------------------- #

class TestI2cMotorE2e:
    """SerialMotorController ↔ I2cTransport ↔ TunnelEmulator: full-loop tests.

    All tests use instant_sleep so no wall-clock time is spent; the asyncio
    event loop is yielded as needed to let background tasks run.
    """

    async def test_flush_sends_crc_valid_cmd(self) -> None:
        """(a) flush() → emulator sees a CRC-valid CMD with correct pwm/dir/steer."""
        em = TunnelEmulator()
        t = _make_transport(em)
        ctrl = SerialMotorController(t, sleep=instant_sleep)

        # thrust=0.5 → pwm=round(0.5*255)=128, dir=F; steering=0.25 → steer=25
        ctrl.apply(MotorCommand(thrust=0.5, steering=0.25))
        await ctrl.start()
        await ctrl.flush()

        # A few yields ensure the write coroutine completes inside to_thread
        for _ in range(10):
            await asyncio.sleep(0)

        try:
            assert len(em.received_lines) == 1, (
                f"expected 1 received CMD line; got {em.received_lines}"
            )
            payload, crc_ok = strip_verify_crc(em.received_lines[0])
            assert crc_ok is True, "CRC must be valid"
            parts = payload.split()
            assert parts[0] == "CMD"
            assert parts[1] == "128"   # pwm = round(0.5 * 255) = 128
            assert parts[2] == "F"     # forward direction
            assert parts[3] == "25"    # steer = round(0.25 * 100) = 25
        finally:
            await ctrl.stop()

    async def test_a_reply_populates_last_feedback(self) -> None:
        """(b) Tunneled A reply lands in controller.last_feedback — full loop."""
        em = TunnelEmulator()
        t = _make_transport(em)
        ctrl = SerialMotorController(t, sleep=instant_sleep)

        ctrl.apply(MotorCommand(thrust=0.0, steering=0.0))
        await ctrl.start()
        await ctrl.flush()

        try:
            await eventually(lambda: ctrl.last_feedback is not None)

            fb = ctrl.last_feedback
            assert fb is not None
            assert fb.angle_deg == em._fixed_angle   # 45.0
            assert fb.ok is True
        finally:
            await ctrl.stop()

    async def test_heartbeat_seq_round_trip(self) -> None:
        """(c) Heartbeat: arm seq, flush → A echoes seq → controller health acks."""
        em = TunnelEmulator()
        t = _make_transport(em)
        ctrl = SerialMotorController(t, sleep=instant_sleep, heartbeat=True)

        ctrl.apply(MotorCommand(thrust=0.0, steering=0.0))
        await ctrl.start()
        await ctrl.flush()   # seq increments to 1 on first flush

        try:
            await eventually(lambda: ctrl.last_acked_seq is not None)

            assert ctrl.last_acked_seq == 1, (
                f"expected acked seq=1, got {ctrl.last_acked_seq}"
            )
            assert ctrl.healthy, "controller must be healthy after ack"

            # Cross-check: seq in the CMD line matches what was acked
            assert len(em.received_lines) >= 1
            cmd_payload, _ = strip_verify_crc(em.received_lines[0])
            cmd_parts = cmd_payload.split()
            assert len(cmd_parts) >= 5, "CMD line must carry seq field"
            seq_sent = int(cmd_parts[4])
            assert ctrl.last_acked_seq == seq_sent
        finally:
            await ctrl.stop()

    async def test_emulator_death_triggers_reconnect(self) -> None:
        """(d) Emulator dies → EOFError → supervisor calls transport.open() again.

        Mirrors the reconnect assertion in test_i2c_link.TestPersistentFailure
        but goes through the full SerialMotorController → I2cTransport stack.
        """
        em = TunnelEmulator()
        open_calls: list[int] = []

        def factory(bus_num: int) -> tuple:
            open_calls.append(bus_num)
            return em, lambda data: _WriteMsg(data), lambda n: _ReadMsg(n)

        t = I2cTransport(3, smbus_factory=factory, sleep=instant_sleep)
        ctrl = SerialMotorController(
            t,
            sleep=instant_sleep,
            backoff_start=0.001,
            backoff_max=0.001,
        )

        await ctrl.start()
        assert len(open_calls) == 1, "factory must be called exactly once on start"

        # Kill the emulator: all subsequent i2c_rdwr calls raise OSError.
        em.kill()

        # Wait for the supervisor to detect failure and attempt reconnect.
        # The poll loop needs _POLL_ERR_LIMIT (5) consecutive errors before
        # signalling EOF; the supervisor then calls transport.close() +
        # transport.open() (→ factory call #2+).
        try:
            await eventually(lambda: len(open_calls) >= 2, timeout=10.0)
            assert len(open_calls) >= 2, (
                "smbus_factory must be called again during reconnect; "
                f"call count = {len(open_calls)}"
            )
        finally:
            await ctrl.stop()
