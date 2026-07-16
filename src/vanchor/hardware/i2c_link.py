"""I²C transport for the helm-Pico motor controller tunnel.

Implements :class:`~vanchor.hardware.serial_link.SerialTransport` over the I²C
register map defined in
``../vanchor-pcb/firmware/helm-pico/docs/I2C-TUNNEL.md``.

The tunnel carries the existing vanchor-ng ASCII line protocol (CMD/STEERD/
THRUST out, A/E/C in, CRC-8 ``*HH``) byte-identically through two FIFOs in the
helm Pico.  From the motor controller's perspective this transport is
indistinguishable from :class:`~.serial_link.PySerialTransport`.

Blocking I²C calls
~~~~~~~~~~~~~~~~~~
All smbus2 calls block the calling thread.  To keep the asyncio event loop
responsive, every bus operation runs via :func:`asyncio.to_thread` and all
access to the bus handle is serialized by a single :class:`asyncio.Lock`.
The poll task and :meth:`I2cTransport.write_line` share the lock; neither can
interleave a partial transaction.

smbus2 optional dependency
~~~~~~~~~~~~~~~~~~~~~~~~~~
``smbus2`` is imported lazily inside :meth:`I2cTransport.open` so that importing
this module never requires the ``i2c`` extra to be installed.  Sim-only installs
work without smbus2.  If the extra is absent when :meth:`~I2cTransport.open` is
called, a clear :class:`RuntimeError` is raised::

    pip install vanchor[i2c]

Test seam (``smbus_factory``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The constructor accepts an optional *smbus_factory* callable.  When given, it is
called with the integer bus number and must return a 3-tuple::

    (bus_obj, write_msg_fn, read_msg_fn)

where:

* ``bus_obj`` has ``i2c_rdwr(*msgs) -> None`` and ``close() -> None``.
* ``write_msg_fn(data: bytes) -> msg`` constructs a write message; the bus
  sends ``bytes(msg)`` in the next ``i2c_rdwr`` call.
* ``read_msg_fn(n: int) -> msg`` constructs a read message; after
  ``i2c_rdwr`` fills it, ``bytes(msg)`` returns the received bytes.

When *smbus_factory* is ``None`` the real ``smbus2.SMBus`` / ``i2c_msg`` are
used instead.

The module exports :class:`_WriteMsg` and :class:`_ReadMsg` shims (prefixed
``_`` by convention, but importable by tests) so a test's fake bus can use
plain ``bytes``/``bytearray`` without requiring smbus2 to be installed.

BENCH-VERIFY
~~~~~~~~~~~~
No physical helm PCB existed as of 2026-07-16.  This transport implements the
wire spec in ``I2C-TUNNEL.md`` exactly, including the WHOAMI/VERSION probe, the
TXA latch protocol, and the FLAGS polling.  It **MUST** be bench-verified against
real firmware (see §4 of I2C-TUNNEL.md for the one-liner test session) before
boat deployment.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from typing import Any, Callable

from .serial_link import SerialTransport

logger = logging.getLogger("vanchor.hardware.i2c")

# --------------------------------------------------------------------------- #
# Register map  (I2C-TUNNEL.md §2)
# --------------------------------------------------------------------------- #
_REG_WHOAMI = 0x00   # constant 0x56 ('V')
_REG_TXA_L  = 0x02   # feedback bytes available, low byte (latches the pair)
_REG_FLAGS  = 0x06   # bit0=RX overflow, bit1=TX overflow; clears on read
_REG_DATA   = 0x10   # the FIFO: write=command bytes in, read=feedback bytes out

_WHOAMI_EXPECTED  = 0x56   # ASCII 'V'
_VERSION_EXPECTED = 0x01

_POLL_ERR_LIMIT = 5     # consecutive poll errors before marking transport dead
_FLAGS_INTERVAL = 5.0   # seconds between FLAGS register polls


# --------------------------------------------------------------------------- #
# Message shims (used by the test fake bus; real code uses smbus2.i2c_msg)
# --------------------------------------------------------------------------- #

class _WriteMsg:
    """Minimal write-message shim, mirroring the ``smbus2.i2c_msg.write`` API.

    The fake bus reads ``bytes(msg)`` to inspect what the transport sent.
    """

    __slots__ = ("_data",)

    def __init__(self, data: bytes | bytearray | list[int]) -> None:
        self._data = bytes(data)

    def __bytes__(self) -> bytes:
        return self._data

    def __iter__(self):          # smbus2 iterates messages in i2c_rdwr
        return iter(self._data)


class _ReadMsg:
    """Minimal read-message shim, mirroring the ``smbus2.i2c_msg.read`` API.

    The fake bus fills :attr:`_buf` during ``i2c_rdwr``; the transport then
    calls ``bytes(msg)`` to retrieve the received data.
    """

    __slots__ = ("length", "_buf")

    def __init__(self, length: int) -> None:
        self.length = length
        self._buf = bytearray(length)    # filled by the fake bus

    def __bytes__(self) -> bytes:
        return bytes(self._buf)

    def __iter__(self):
        return iter(self._buf)


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #

class I2cTransport(SerialTransport):
    """SerialTransport backed by the helm-Pico I²C tunnel.

    Parameters
    ----------
    bus:
        Linux I²C bus number (the ``N`` in ``/dev/i2c-N``).
    addr:
        7-bit I²C slave address; ``0x42`` as wired on the helm PCB.
    poll_hz:
        Inbound feedback poll frequency (default 20 Hz ≈ 50 ms period).
        The Pico emits ``A`` lines at ~10 Hz; 20 Hz yields roughly one idle
        poll between lines, keeping latency low.  Do not exceed ~50 Hz.
    smbus_factory:
        Optional test seam (see module docstring).  ``None`` = real smbus2.
    sleep:
        Injected sleep coroutine; default :func:`asyncio.sleep`.  Tests
        substitute an instant-return fake so no wall-clock time is spent.
    """

    def __init__(
        self,
        bus: int,
        addr: int = 0x42,
        poll_hz: float = 20.0,
        *,
        smbus_factory: Callable[[int], tuple[Any, Callable, Callable]] | None = None,
        sleep: Callable = asyncio.sleep,
    ) -> None:
        self._bus_num = bus
        self._addr = addr
        self._poll_interval = 1.0 / max(1.0, poll_hz)
        self._smbus_factory = smbus_factory
        self._sleep = sleep

        # Set in open(); cleared in close().
        self._bus: Any = None
        self._write_msg: Callable | None = None   # write_msg_fn(data: bytes) -> msg
        self._read_msg: Callable | None = None    # read_msg_fn(n: int) -> msg

        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._poll_task: asyncio.Task | None = None
        self._buf = bytearray()

    # ----------------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------------- #

    async def open(self) -> None:
        """Open the bus, probe WHOAMI/VERSION, start the inbound poll task.

        Raises :class:`OSError` on I²C NAK or identity mismatch.
        Raises :class:`RuntimeError` when smbus2 is not installed.
        Idempotent: a second call while already open is a no-op.
        """
        if self._bus is not None:
            return

        # Drain any EOF sentinel left over from a previous persistent failure
        # so that read_line() works normally on reconnect.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._buf = bytearray()

        if self._smbus_factory is not None:
            bus, write_fn, read_fn = self._smbus_factory(self._bus_num)
        else:
            try:
                from smbus2 import SMBus, i2c_msg   # type: ignore[import-untyped]
            except ImportError as exc:
                raise RuntimeError(
                    "I2cTransport requires the 'i2c' extra "
                    "(pip install vanchor[i2c])"
                ) from exc
            addr = self._addr
            bus = SMBus(self._bus_num)
            write_fn = lambda data: i2c_msg.write(addr, list(data))  # noqa: E731
            read_fn  = lambda n:    i2c_msg.read(addr, n)            # noqa: E731

        # Wire up message factories before _probe runs (it uses them).
        self._write_msg = write_fn
        self._read_msg  = read_fn
        self._bus       = bus

        try:
            await asyncio.to_thread(self._probe)
        except Exception:
            # Probe failed — tear down cleanly so a subsequent open() retries.
            self._bus = self._write_msg = self._read_msg = None
            try:
                bus.close()
            except Exception:
                pass
            raise

        self._poll_task = asyncio.ensure_future(self._poll_loop())
        logger.debug(
            "I2cTransport open: bus=%d addr=0x%02X poll_hz=%.0f",
            self._bus_num, self._addr, 1.0 / self._poll_interval,
        )

    async def close(self) -> None:
        """Cancel the poll task and close the bus (idempotent)."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None

        if self._bus is not None:
            bus, self._bus = self._bus, None
            self._write_msg = self._read_msg = None
            try:
                await asyncio.to_thread(bus.close)
            except Exception:   # pragma: no cover - defensive
                logger.debug("I2C bus %d: error on close", self._bus_num)

    # ----------------------------------------------------------------------- #
    # Line I/O
    # ----------------------------------------------------------------------- #

    async def read_line(self) -> str:
        """Dequeue the next inbound line (blocking).

        Returns the line with its ``\\r\\n`` terminator stripped.
        Raises :class:`EOFError` when the transport has been marked dead after
        :data:`_POLL_ERR_LIMIT` consecutive bus errors — the existing
        :class:`~.serial_devices._SerialReadSupervisor` reconnect loop handles
        this the same way it handles a yanked USB cable.
        """
        item = await self._queue.get()
        if item is None:
            raise EOFError(
                f"I2C bus {self._bus_num}: transport dead after "
                f"{_POLL_ERR_LIMIT} consecutive errors"
            )
        return item

    async def write_line(self, line: str) -> None:
        """Write one line to the Pico's RX FIFO in a **single** i2c_rdwr call.

        Encodes *line* as ASCII, appends ``\\r\\n``, prepends the DATA register
        address (``0x10``), and sends the whole payload in one ``i2c_rdwr``
        transaction under the bus lock (run via :func:`asyncio.to_thread`).

        Lines are ≤ 48 bytes per the protocol spec, so one transaction always
        suffices (no chunking needed).  Bus errors propagate to the caller
        (which already handles transport exceptions).
        """
        payload = bytes([_REG_DATA]) + line.encode("ascii", errors="replace") + b"\r\n"
        async with self._lock:
            await asyncio.to_thread(self._write_bus, payload)

    # ----------------------------------------------------------------------- #
    # Binary I/O (ABC requires these; the motor stack does not use them)
    # ----------------------------------------------------------------------- #

    async def read(self, n: int = 4096) -> bytes:
        """Drain up to *n* bytes directly from the DATA FIFO via I²C.

        The motor stack does **not** call this method — it uses
        :meth:`read_line` (the poll task accumulates and line-frames the
        inbound stream).  Provided only because the
        :class:`~.serial_link.SerialTransport` ABC requires it.
        """
        async with self._lock:
            avail = await asyncio.to_thread(self._read_txa)
            if avail == 0:
                return b""
            count = min(n, avail)
            return await asyncio.to_thread(self._read_data_raw, count)

    async def write(self, data: bytes) -> None:
        """Write raw bytes to the DATA FIFO via I²C.

        The motor stack does **not** call this method — it uses
        :meth:`write_line`.  Provided only because the ABC requires it.
        """
        payload = bytes([_REG_DATA]) + data
        async with self._lock:
            await asyncio.to_thread(self._write_bus, payload)

    # ----------------------------------------------------------------------- #
    # Blocking bus helpers (called via asyncio.to_thread)
    # ----------------------------------------------------------------------- #

    def _probe(self) -> None:
        """Verify WHOAMI==0x56 and VERSION==0x01; raises OSError on failure.

        Reads both registers in a single combined write-then-read i2c_rdwr
        call: ``W[0x00]`` sets the pointer to WHOAMI; the subsequent ``R[2]``
        reads WHOAMI then VERSION via auto-increment.
        """
        w = self._write_msg(bytes([_REG_WHOAMI]))
        r = self._read_msg(2)
        self._bus.i2c_rdwr(w, r)
        data = bytes(r)
        whoami, version = data[0], data[1]
        if whoami != _WHOAMI_EXPECTED:
            raise OSError(
                f"helm-Pico probe: WHOAMI=0x{whoami:02X} "
                f"(expected 0x{_WHOAMI_EXPECTED:02X}) "
                f"at bus={self._bus_num} addr=0x{self._addr:02X}"
            )
        if version != _VERSION_EXPECTED:
            raise OSError(
                f"helm-Pico probe: VERSION={version} "
                f"(expected {_VERSION_EXPECTED}) "
                f"at bus={self._bus_num} addr=0x{self._addr:02X}"
            )
        logger.info(
            "helm-Pico identified: bus=%d addr=0x%02X WHOAMI=0x%02X VERSION=%d",
            self._bus_num, self._addr, whoami, version,
        )

    def _read_txa(self) -> int:
        """Read TXA_L + TXA_H at register 0x02; return available byte count.

        A single ``i2c_rdwr(W[0x02], R[2])`` call reads both bytes atomically.
        TXA_L is read first (which latches TXA_H per spec), then TXA_H follows
        via auto-increment.
        """
        w = self._write_msg(bytes([_REG_TXA_L]))
        r = self._read_msg(2)
        self._bus.i2c_rdwr(w, r)
        lo, hi = bytes(r)
        return lo | (hi << 8)

    def _read_data_raw(self, n: int) -> bytes:
        """Read exactly *n* bytes from the DATA register (0x10)."""
        w = self._write_msg(bytes([_REG_DATA]))
        r = self._read_msg(n)
        self._bus.i2c_rdwr(w, r)
        return bytes(r)

    def _read_flags(self) -> int:
        """Read the FLAGS register (0x06); it clears on read per spec."""
        w = self._write_msg(bytes([_REG_FLAGS]))
        r = self._read_msg(1)
        self._bus.i2c_rdwr(w, r)
        return bytes(r)[0]

    def _write_bus(self, payload: bytes) -> None:
        """Send *payload* as a single i2c_rdwr write transaction."""
        if self._bus is None:
            raise OSError("I2C transport not open")
        self._bus.i2c_rdwr(self._write_msg(payload))

    # ----------------------------------------------------------------------- #
    # Poll task
    # ----------------------------------------------------------------------- #

    async def _poll_loop(self) -> None:
        """Drain inbound feedback from the Pico at ``poll_hz``.

        On each iteration:
        1. Read TXA (2 bytes at 0x02, TXA_L latches TXA_H).
        2. If bytes are available, read them from DATA (0x10), append to
           the accumulator buffer, and split off complete ``\\n``-terminated
           lines (``\\r`` and NUL bytes stripped before queuing).
        3. Every :data:`_FLAGS_INTERVAL` seconds, read FLAGS (0x06) and log
           a rate-limited warning if any overflow bit is set (not fatal).

        After :data:`_POLL_ERR_LIMIT` consecutive bus errors, places a
        ``None`` sentinel in the queue (so :meth:`read_line` raises
        :class:`EOFError`) and exits, leaving reconnect to the supervisor.
        """
        consecutive_errors = 0
        last_flags_t = _time.monotonic()

        while True:
            try:
                await self._sleep(self._poll_interval)
            except asyncio.CancelledError:
                return

            try:
                async with self._lock:
                    # 1. TXA: one 2-byte read starting at 0x02.
                    n = await asyncio.to_thread(self._read_txa)

                    # 2. Drain DATA if bytes are available.
                    if n > 0:
                        raw = await asyncio.to_thread(self._read_data_raw, n)
                        self._buf.extend(raw)
                        self._flush_lines()

                    # 3. Periodic FLAGS health check.
                    now = _time.monotonic()
                    if now - last_flags_t >= _FLAGS_INTERVAL:
                        flags = await asyncio.to_thread(self._read_flags)
                        last_flags_t = now
                        if flags:
                            logger.warning(
                                "helm-Pico bus %d FLAGS=0x%02X "
                                "(rx_overflow=%s tx_overflow=%s)",
                                self._bus_num, flags,
                                bool(flags & 0x01), bool(flags & 0x02),
                            )

                consecutive_errors = 0

            except asyncio.CancelledError:
                return
            except Exception as exc:
                consecutive_errors += 1
                logger.warning(
                    "I2C bus %d poll error %d/%d: %s",
                    self._bus_num, consecutive_errors, _POLL_ERR_LIMIT, exc,
                )
                if consecutive_errors >= _POLL_ERR_LIMIT:
                    logger.error(
                        "I2C bus %d: %d consecutive errors; transport is dead",
                        self._bus_num, consecutive_errors,
                    )
                    self._queue.put_nowait(None)   # read_line() will raise EOFError
                    return

    def _flush_lines(self) -> None:
        """Split ``_buf`` on ``\\n`` and push complete lines to the queue.

        Each line has ``\\r`` and NUL bytes (``0x00`` filler) stripped before
        being enqueued.  Empty results (e.g. a bare ``\\r\\n``) are discarded.
        """
        while b"\n" in self._buf:
            raw_line, self._buf = self._buf.split(b"\n", 1)
            text = (
                raw_line
                .replace(b"\x00", b"")
                .decode("ascii", errors="replace")
                .rstrip("\r")
            )
            if text:
                self._queue.put_nowait(text)


# --------------------------------------------------------------------------- #
# Transport factory
# --------------------------------------------------------------------------- #

def make_motor_transport(port: str, **serial_kwargs) -> SerialTransport:
    """Return the right transport for *port*.

    * ``i2c:<bus>`` or ``i2c:<bus>:<addr>`` → :class:`I2cTransport`.
    * Anything else → :class:`~.serial_link.PySerialTransport` (with
      *serial_kwargs* forwarded verbatim).

    *bus* must be a non-negative integer (the ``N`` in ``/dev/i2c-N``).
    *addr* may be given in hex (``0x42``) or decimal (``66``); the default
    when omitted is ``0x42`` (the helm PCB wiring).

    Raises :class:`ValueError` for a malformed ``i2c:`` scheme — e.g.
    ``i2c:`` (missing bus), ``i2c:abc`` (non-integer bus), ``i2c:3:xyz``
    (non-integer addr), ``i2c:-1`` (negative bus).

    Serial kwargs (``baudrate``, ``bytesize``, ``parity``, ``stopbits``) are
    silently ignored for ``i2c:`` ports; a single DEBUG message is emitted so
    the caller can confirm the kwargs were intentionally dropped.
    """
    from .serial_link import PySerialTransport

    if not port.startswith("i2c:"):
        return PySerialTransport(port, **serial_kwargs)

    # ---- parse i2c:<bus>[:<addr>] ----------------------------------------- #
    rest = port[len("i2c:"):]
    parts = rest.split(":", 1)
    if not parts[0]:
        raise ValueError(
            f"malformed i2c port {port!r}: expected i2c:<bus> or i2c:<bus>:<addr> "
            f"(e.g. i2c:3 or i2c:3:0x42)"
        )
    try:
        bus = int(parts[0])
    except ValueError:
        raise ValueError(
            f"malformed i2c port {port!r}: bus must be a non-negative integer "
            f"(e.g. i2c:3 or i2c:3:0x42)"
        ) from None
    if bus < 0:
        raise ValueError(
            f"malformed i2c port {port!r}: bus must be non-negative (got {bus})"
        )

    if len(parts) == 2 and parts[1]:
        try:
            addr = int(parts[1], 0)   # accepts 0x42 (hex) or 66 (decimal)
        except ValueError:
            raise ValueError(
                f"malformed i2c port {port!r}: addr must be an integer "
                f"(e.g. 0x42 or 66)"
            ) from None
    else:
        addr = 0x42  # helm PCB default

    if serial_kwargs:
        logger.debug(
            "make_motor_transport: i2c: port %r — serial kwargs %s are ignored",
            port, sorted(serial_kwargs),
        )

    return I2cTransport(bus=bus, addr=addr)
