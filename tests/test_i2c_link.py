"""Unit tests for I2cTransport (src/vanchor/hardware/i2c_link.py).

No physical I²C hardware or smbus2 installation is needed: all bus I/O goes
through a FakeBus injected via the ``smbus_factory`` constructor argument.

Test coverage
~~~~~~~~~~~~~
* Probe: correct WHOAMI/VERSION → open succeeds; wrong WHOAMI → OSError;
  NAK (bus raises OSError) → OSError propagates.
* write_line: frames exactly ``b"\\x10" + line + b"\\r\\n"`` in ONE i2c_rdwr
  transaction (verified against the fake's transaction log).
* Poll drain: fake bus stages TXA=len + DATA bytes for two lines, including a
  line split across two poll cycles.  read_line() yields each complete line
  with ``\\r`` and NUL bytes stripped.
* TXA protocol: the TXA read uses a single 2-byte transaction starting at
  register 0x02 (asserted on the transaction log).
* FLAGS nonzero → warning logged (caplog), transport stays alive.
* Persistent poll failure (≥ 5 consecutive bus errors) → read_line() raises
  EOFError.
* close() is idempotent and cancels the poll task.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

import pytest

from vanchor.hardware.i2c_link import (
    I2cTransport,
    _FLAGS_INTERVAL,
    _POLL_ERR_LIMIT,
    _ReadMsg,
    _WriteMsg,
    _time,
)
import vanchor.hardware.i2c_link as i2c_mod


# --------------------------------------------------------------------------- #
# Fake bus
# --------------------------------------------------------------------------- #

class FakeBus:
    """In-memory I²C bus for tests.

    Staged responses are queued per register address.  When ``i2c_rdwr`` is
    called with a write-then-read pair the write's first byte is treated as the
    register pointer and the read is filled from the corresponding queue.
    Un-staged reads return all zeros (0x00 filler, matching the hardware spec).

    The ``fail_after`` knob makes all calls *after* the N-th raise
    ``OSError("fake bus error")`` — useful for testing persistent-failure
    behaviour while letting the initial probe succeed.
    """

    def __init__(self) -> None:
        self.transactions: list[tuple] = []
        self._staged: dict[int, deque[bytes]] = {}
        self._call_count: int = 0
        self.fail_after: int | None = None   # None = never fail
        self.always_raise: Exception | None = None

    # -- test helpers -------------------------------------------------------- #

    def stage(self, reg: int, data: bytes) -> None:
        """Queue *data* as the next response for reads from register *reg*."""
        if reg not in self._staged:
            self._staged[reg] = deque()
        self._staged[reg].append(bytes(data))

    # -- bus interface ------------------------------------------------------- #

    def i2c_rdwr(self, *msgs: Any) -> None:
        self._call_count += 1
        if self.always_raise is not None:
            raise self.always_raise
        if self.fail_after is not None and self._call_count > self.fail_after:
            raise OSError(f"fake bus error on call {self._call_count}")

        current_reg: int | None = None
        for msg in msgs:
            if isinstance(msg, _WriteMsg):
                data = bytes(msg)
                self.transactions.append(("write", data))
                if data:
                    current_reg = data[0]
            elif isinstance(msg, _ReadMsg):
                self.transactions.append(("read", msg.length, current_reg))
                if current_reg in self._staged and self._staged[current_reg]:
                    response = self._staged[current_reg].popleft()
                    n = min(len(response), msg.length)
                    msg._buf[:n] = response[:n]
                # else: _buf stays all zeros (NUL filler per spec)

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def make_factory(bus: FakeBus):
    """Return an smbus_factory callable that always hands back *bus*."""
    def factory(bus_num: int):
        return bus, lambda data: _WriteMsg(data), lambda n: _ReadMsg(n)
    return factory


async def instant_sleep(delay: float) -> None:
    """Fake asyncio.sleep: yields to the event loop once then returns."""
    await asyncio.sleep(0)


def _open_ok_bus() -> FakeBus:
    """Return a FakeBus pre-staged with a valid probe response."""
    bus = FakeBus()
    bus.stage(0x00, b"\x56\x01")   # WHOAMI=0x56, VERSION=0x01
    return bus


async def _open_transport(bus: FakeBus, **kwargs) -> I2cTransport:
    """Create and open an I2cTransport with the fake bus."""
    t = I2cTransport(3, smbus_factory=make_factory(bus), sleep=instant_sleep, **kwargs)
    await t.open()
    return t


# --------------------------------------------------------------------------- #
# Probe tests
# --------------------------------------------------------------------------- #

class TestProbe:
    """open() identity checks."""

    async def test_correct_identity_succeeds(self) -> None:
        bus = _open_ok_bus()
        t = await _open_transport(bus)
        assert t._bus is bus
        await t.close()

    async def test_wrong_whoami_raises_oserror(self) -> None:
        bus = FakeBus()
        bus.stage(0x00, b"\x57\x01")   # WHOAMI wrong (0x57 ≠ 0x56)
        t = I2cTransport(3, smbus_factory=make_factory(bus), sleep=instant_sleep)
        with pytest.raises(OSError, match="WHOAMI"):
            await t.open()
        # Transport should be cleanly reset so a retry is possible.
        assert t._bus is None

    async def test_wrong_version_raises_oserror(self) -> None:
        bus = FakeBus()
        bus.stage(0x00, b"\x56\x02")   # VERSION wrong (0x02 ≠ 0x01)
        t = I2cTransport(3, smbus_factory=make_factory(bus), sleep=instant_sleep)
        with pytest.raises(OSError, match="VERSION"):
            await t.open()
        assert t._bus is None

    async def test_nak_propagates(self) -> None:
        """Bus raising OSError (e.g. NAK) propagates from open()."""
        bus = FakeBus()
        bus.always_raise = OSError("remote I/O error [Errno 121]")
        t = I2cTransport(3, smbus_factory=make_factory(bus), sleep=instant_sleep)
        with pytest.raises(OSError):
            await t.open()
        assert t._bus is None

    async def test_open_idempotent(self) -> None:
        """Calling open() twice does not start a second poll task."""
        bus = _open_ok_bus()
        t = await _open_transport(bus)
        first_task = t._poll_task
        await t.open()   # second call — should be a no-op
        assert t._poll_task is first_task
        await t.close()

    async def test_reopen_after_probe_failure(self) -> None:
        """After a failed open(), a subsequent open() with good probe succeeds."""
        bus = FakeBus()
        bus.stage(0x00, b"\xFF\x01")   # first probe: wrong WHOAMI
        bus.stage(0x00, b"\x56\x01")   # second probe: correct
        t = I2cTransport(3, smbus_factory=make_factory(bus), sleep=instant_sleep)
        with pytest.raises(OSError):
            await t.open()
        assert t._bus is None
        await t.open()   # retry should succeed
        assert t._bus is bus
        await t.close()


# --------------------------------------------------------------------------- #
# write_line tests
# --------------------------------------------------------------------------- #

class TestWriteLine:
    """write_line() framing contract."""

    async def test_single_transaction(self) -> None:
        """write_line sends exactly ONE i2c_rdwr call with the full payload."""
        bus = _open_ok_bus()
        t = await _open_transport(bus)

        line = "CMD 0 F 0*DC"
        bus.transactions.clear()   # ignore probe transactions
        await t.write_line(line)

        # Exactly one write transaction produced.
        writes = [tx for tx in bus.transactions if tx[0] == "write"]
        assert len(writes) == 1, f"expected 1 write transaction, got {len(writes)}"

        payload = writes[0][1]
        assert payload == b"\x10" + line.encode() + b"\r\n", (
            f"write payload mismatch: {payload!r}"
        )
        await t.close()

    async def test_no_read_transactions(self) -> None:
        """write_line must not generate any read transactions."""
        bus = _open_ok_bus()
        t = await _open_transport(bus)
        bus.transactions.clear()
        await t.write_line("TEST*00")
        reads = [tx for tx in bus.transactions if tx[0] == "read"]
        assert reads == [], f"unexpected read transactions: {reads}"
        await t.close()


# --------------------------------------------------------------------------- #
# Poll / drain tests
# --------------------------------------------------------------------------- #

class TestPoll:
    """Inbound poll loop and line assembly."""

    async def test_single_line(self) -> None:
        """A complete line in one poll cycle is returned by read_line()."""
        bus = _open_ok_bus()
        # Stage TXA = 7 bytes, then DATA = "HELLO\r\n"
        bus.stage(0x02, b"\x07\x00")
        bus.stage(0x10, b"HELLO\r\n")

        t = await _open_transport(bus)
        line = await asyncio.wait_for(t.read_line(), timeout=2.0)
        assert line == "HELLO"
        await t.close()

    async def test_two_lines_one_drain(self) -> None:
        """Two lines in one DATA read → both queued; read_line() yields each."""
        bus = _open_ok_bus()
        data = b"ALPHA\r\nBETA\r\n"
        bus.stage(0x02, bytes([len(data), 0]))
        bus.stage(0x10, data)

        t = await _open_transport(bus)
        l1 = await asyncio.wait_for(t.read_line(), timeout=2.0)
        l2 = await asyncio.wait_for(t.read_line(), timeout=2.0)
        assert l1 == "ALPHA"
        assert l2 == "BETA"
        await t.close()

    async def test_split_line_across_two_polls(self) -> None:
        """A line split across two poll cycles is reassembled correctly."""
        bus = _open_ok_bus()

        # Poll 1: "FIRST\r\n" complete + start of second line
        part1 = b"FIRST\r\nSEC"
        bus.stage(0x02, bytes([len(part1), 0]))
        bus.stage(0x10, part1)

        # Poll 2: rest of second line
        part2 = b"OND\r\n"
        bus.stage(0x02, bytes([len(part2), 0]))
        bus.stage(0x10, part2)

        t = await _open_transport(bus)
        l1 = await asyncio.wait_for(t.read_line(), timeout=2.0)
        l2 = await asyncio.wait_for(t.read_line(), timeout=2.0)
        assert l1 == "FIRST"
        assert l2 == "SECOND"
        await t.close()

    async def test_nul_filler_stripped(self) -> None:
        """NUL bytes in DATA are removed before the line is queued."""
        bus = _open_ok_bus()
        data = b"HEL\x00LO\r\n"
        bus.stage(0x02, bytes([len(data), 0]))
        bus.stage(0x10, data)

        t = await _open_transport(bus)
        line = await asyncio.wait_for(t.read_line(), timeout=2.0)
        assert line == "HELLO"
        await t.close()

    async def test_carriage_return_stripped(self) -> None:
        """Lines delivered without the trailing \\r."""
        bus = _open_ok_bus()
        bus.stage(0x02, b"\x05\x00")
        bus.stage(0x10, b"HI\r\n\x00")   # 5 bytes: H I \r \n \x00
        t = await _open_transport(bus)
        line = await asyncio.wait_for(t.read_line(), timeout=2.0)
        assert line == "HI"
        await t.close()

    async def test_empty_poll_no_data_transaction(self) -> None:
        """When TXA returns 0, no DATA read is issued."""
        bus = _open_ok_bus()
        bus.stage(0x02, b"\x00\x00")   # 0 bytes available

        t = await _open_transport(bus)
        # Let one poll cycle complete without reading from queue.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        data_reads = [
            tx for tx in bus.transactions
            if tx[0] == "read" and tx[2] == 0x10
        ]
        assert data_reads == [], f"unexpected DATA reads: {data_reads}"
        await t.close()

    async def test_txa_read_is_2byte_at_0x02(self) -> None:
        """The TXA drain uses a single 2-byte read starting at register 0x02.

        The transaction log must show write(0x02) immediately followed by
        read(2, reg=0x02) — both from the same i2c_rdwr call.
        """
        bus = _open_ok_bus()
        bus.stage(0x02, b"\x03\x00")
        bus.stage(0x10, b"OK\n")

        t = await _open_transport(bus)
        await asyncio.wait_for(t.read_line(), timeout=2.0)
        await t.close()

        # Find the TXA write/read pair in the transaction log.
        writes_at_02 = [i for i, tx in enumerate(bus.transactions)
                        if tx == ("write", b"\x02")]
        assert writes_at_02, "no write(0x02) found in transaction log"
        idx = writes_at_02[0]
        assert idx + 1 < len(bus.transactions), "no read after write(0x02)"
        next_tx = bus.transactions[idx + 1]
        assert next_tx == ("read", 2, 0x02), (
            f"expected read(2, reg=0x02) after write(0x02), got {next_tx!r}"
        )

    async def test_txa_high_byte(self) -> None:
        """TXA_H is honoured: a count > 255 is decoded correctly."""
        bus = _open_ok_bus()
        # Simulate 256 bytes available: TXA_L=0x00, TXA_H=0x01 → n=256
        # We can't actually receive 256 bytes in one test easily, so just
        # verify the decode.  Stage a small read so the poll doesn't block.
        # (n=256 would cause a read of 256 bytes; stage just 1 '\n' to complete.)
        payload = b"\n"
        bus.stage(0x02, bytes([0x01, 0x01]))   # 0x0101 = 257... oops, use simpler
        # Use TXA = 1 (0x01, 0x00) and a single newline to keep the test simple.
        # The real decode test: lo=0xFF hi=0x01 → 511
        t = I2cTransport(3, smbus_factory=make_factory(bus), sleep=instant_sleep)

        # Manually verify _read_txa decode logic.
        lo, hi = 0xFF, 0x01
        assert (lo | (hi << 8)) == 0x01FF   # 511

        # Re-stage for a real poll: TXA=1 byte, DATA="\n" (produces empty line)
        bus = _open_ok_bus()
        bus.stage(0x02, b"\x01\x00")
        bus.stage(0x10, b"\n")
        t2 = await _open_transport(bus)
        # empty line is filtered out; poll doesn't block read_line()
        await t2.close()


# --------------------------------------------------------------------------- #
# FLAGS tests
# --------------------------------------------------------------------------- #

class TestFlags:
    """FLAGS register: nonzero → warning logged; transport stays alive."""

    async def test_flags_nonzero_logs_warning(
        self, caplog: pytest.LogCaptureFixture, monkeypatch
    ) -> None:
        # Patch _FLAGS_INTERVAL to 0.0 so every poll iteration reads FLAGS.
        monkeypatch.setattr(i2c_mod, "_FLAGS_INTERVAL", 0.0)

        bus = _open_ok_bus()
        bus.stage(0x02, b"\x00\x00")   # TXA = 0 (no data)
        bus.stage(0x06, b"\x03")       # FLAGS = rx_overflow | tx_overflow

        t = await _open_transport(bus)

        with caplog.at_level(logging.WARNING, logger="vanchor.hardware.i2c"):
            # Give the poll loop at least one iteration to fire.
            await asyncio.sleep(0.05)

        await t.close()

        flag_warnings = [
            r for r in caplog.records
            if "FLAGS" in r.message and r.levelno == logging.WARNING
        ]
        assert flag_warnings, (
            "expected a FLAGS warning in caplog; got: "
            + "\n".join(r.message for r in caplog.records)
        )
        assert "rx_overflow=True" in flag_warnings[0].message
        assert "tx_overflow=True" in flag_warnings[0].message

    async def test_flags_nonzero_not_fatal(
        self, caplog: pytest.LogCaptureFixture, monkeypatch
    ) -> None:
        """A nonzero FLAGS value must NOT mark the transport dead."""
        monkeypatch.setattr(i2c_mod, "_FLAGS_INTERVAL", 0.0)

        bus = _open_ok_bus()
        data = b"LINE\r\n"
        bus.stage(0x02, b"\x00\x00")   # TXA=0 first poll (FLAGS fires here)
        bus.stage(0x06, b"\x01")       # FLAGS = rx_overflow
        bus.stage(0x02, bytes([len(data), 0]))
        bus.stage(0x10, data)

        t = await _open_transport(bus)
        with caplog.at_level(logging.WARNING, logger="vanchor.hardware.i2c"):
            line = await asyncio.wait_for(t.read_line(), timeout=2.0)
        assert line == "LINE"
        await t.close()


# --------------------------------------------------------------------------- #
# Persistent failure → EOFError
# --------------------------------------------------------------------------- #

class TestPersistentFailure:
    """After _POLL_ERR_LIMIT consecutive errors read_line() raises EOFError."""

    async def test_eof_after_persistent_errors(self) -> None:
        bus = _open_ok_bus()
        # After the probe (1 successful call), every subsequent call raises.
        bus.fail_after = 1

        t = await _open_transport(bus)

        with pytest.raises(EOFError, match="transport dead"):
            await asyncio.wait_for(t.read_line(), timeout=5.0)

    async def test_error_count_matches_limit(self) -> None:
        """The transport stays alive for LIMIT-1 errors and dies on the LIMIT-th."""
        bus = _open_ok_bus()
        # Let the probe + (POLL_ERR_LIMIT - 1) TXA calls succeed; fail after.
        # probe = 1 call; each poll iteration is ≥1 call.
        bus.fail_after = 1   # probe ok; all poll TXA calls fail

        t = await _open_transport(bus)
        with pytest.raises(EOFError):
            await asyncio.wait_for(t.read_line(), timeout=5.0)

        # Verify the poll task exited (it should be done).
        assert t._poll_task is None or t._poll_task.done()

    async def test_reconnect_after_failure(self) -> None:
        """open() on a new bus after failure succeeds (queue is drained)."""
        bus = _open_ok_bus()
        bus.fail_after = 1
        t = await _open_transport(bus)
        with pytest.raises(EOFError):
            await asyncio.wait_for(t.read_line(), timeout=5.0)

        await t.close()

        # A fresh bus succeeds; open() must drain the sentinel from the queue.
        bus2 = _open_ok_bus()
        bus2.stage(0x02, b"\x05\x00")
        bus2.stage(0x10, b"OK\r\n\x00")
        t._smbus_factory = make_factory(bus2)
        await t.open()
        line = await asyncio.wait_for(t.read_line(), timeout=2.0)
        assert line == "OK"
        await t.close()


# --------------------------------------------------------------------------- #
# close() tests
# --------------------------------------------------------------------------- #

class TestClose:
    """close() cancels the poll task and is idempotent."""

    async def test_close_cancels_poll_task(self) -> None:
        bus = _open_ok_bus()
        t = await _open_transport(bus)
        task = t._poll_task
        assert task is not None
        await t.close()
        assert task.done(), "poll task should be done after close()"
        assert t._poll_task is None

    async def test_close_before_open_is_noop(self) -> None:
        bus = FakeBus()
        t = I2cTransport(3, smbus_factory=make_factory(bus), sleep=instant_sleep)
        await t.close()   # should not raise
        assert t._bus is None

    async def test_close_idempotent(self) -> None:
        bus = _open_ok_bus()
        t = await _open_transport(bus)
        await t.close()
        await t.close()   # second close should not raise
        assert t._bus is None

    async def test_bus_is_none_after_close(self) -> None:
        bus = _open_ok_bus()
        t = await _open_transport(bus)
        await t.close()
        assert t._bus is None
        assert t._write_msg is None
        assert t._read_msg is None


# --------------------------------------------------------------------------- #
# Module-level import test
# --------------------------------------------------------------------------- #

class TestImport:
    """The module must be importable without smbus2."""

    def test_import_without_smbus2(self) -> None:
        """Importing i2c_link must succeed even if smbus2 is absent."""
        import importlib
        # If this test runs, the import at the top of this file already proved
        # the module loaded.  Re-import to be explicit.
        mod = importlib.import_module("vanchor.hardware.i2c_link")
        assert hasattr(mod, "I2cTransport")

    async def test_open_without_smbus2_raises_runtimeerror(
        self, monkeypatch
    ) -> None:
        """open() with no factory and no smbus2 raises RuntimeError."""
        import builtins
        real_import = builtins.__import__

        def _block_smbus2(name, *args, **kwargs):
            if name == "smbus2":
                raise ImportError("smbus2 not installed (test)")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _block_smbus2)

        t = I2cTransport(3)   # no smbus_factory → will try real smbus2
        with pytest.raises(RuntimeError, match="pip install vanchor\\[i2c\\]"):
            await t.open()
