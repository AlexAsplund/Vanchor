"""Serial transport abstraction for real-hardware drivers.

The drivers in :mod:`vanchor.hardware.serial_devices` talk to physical GPS,
compass and motor controllers over a serial line. To keep them testable with
*no* physical port (and to keep the import graph free of a hard ``pyserial``
dependency), all byte-level I/O goes through a small line-oriented transport
abstraction:

  SerialTransport       -- the interface: open/close + read_line/write_line
  FakeSerialTransport   -- in-memory transport for tests; push inbound lines,
                           inspect outbound lines
  PySerialTransport     -- real transport backed by ``serial_asyncio``; the
                           import is guarded so importing this module never
                           requires the ``serial`` extra to be installed

Lines are newline-delimited UTF-8 strings; the transport strips the trailing
newline on read and appends ``\\r\\n`` on write (standard for NMEA / Arduino
serial protocols).
"""

from __future__ import annotations

import abc
import asyncio
import logging

logger = logging.getLogger("vanchor.hardware.serial")


class SerialTransport(abc.ABC):
    """A line-oriented, asynchronous serial transport.

    Implementations move whole text lines to and from some underlying byte
    stream. Drivers depend only on this interface, so the same driver code runs
    against a real port (:class:`PySerialTransport`) or an in-memory fake
    (:class:`FakeSerialTransport`).
    """

    @abc.abstractmethod
    async def open(self) -> None:
        """Open/connect the underlying stream (idempotent)."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Close the underlying stream (idempotent)."""

    @abc.abstractmethod
    async def read_line(self) -> str:
        """Read one line, with the line terminator stripped.

        Blocks (asynchronously) until a line is available. May raise
        :class:`asyncio.CancelledError` when the awaiting task is cancelled, or
        ``EOFError`` when the stream is closed.
        """

    @abc.abstractmethod
    async def write_line(self, line: str) -> None:
        """Write one line; the terminator (``\\r\\n``) is appended here."""


class FakeSerialTransport(SerialTransport):
    """In-memory transport for deterministic tests.

    Tests push inbound lines with :meth:`feed` (which a reader picks up via
    :meth:`read_line`) and inspect everything a driver wrote via the
    :attr:`written` list.
    """

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[str | None] = asyncio.Queue()
        self.written: list[str] = []
        self.opened: bool = False
        self.closed: bool = False

    # -- test helpers ----------------------------------------------------- #
    def feed(self, line: str) -> None:
        """Make ``line`` available to the next :meth:`read_line` call."""
        self._inbound.put_nowait(line.rstrip("\r\n"))

    def feed_eof(self) -> None:
        """Signal end-of-stream; a pending/next :meth:`read_line` raises EOF."""
        self._inbound.put_nowait(None)

    # -- SerialTransport -------------------------------------------------- #
    async def open(self) -> None:
        self.opened = True
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def read_line(self) -> str:
        line = await self._inbound.get()
        if line is None:
            raise EOFError("fake serial transport closed")
        return line

    async def write_line(self, line: str) -> None:
        self.written.append(line)


class PySerialTransport(SerialTransport):
    """Real serial transport backed by ``pyserial-asyncio``.

    The ``serial_asyncio`` import is deferred to :meth:`open` so that merely
    importing this module never requires the optional ``serial`` extra or any
    physical hardware. Construct it with a device ``port`` (e.g.
    ``"/dev/ttyUSB0"``) and a ``baudrate``.
    """

    def __init__(self, port: str, baudrate: int = 4800) -> None:
        self.port = port
        self.baudrate = baudrate
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def open(self) -> None:
        if self._reader is not None:
            return
        try:
            import serial_asyncio  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - needs missing extra
            raise RuntimeError(
                "PySerialTransport requires the 'serial' extra "
                "(pip install pyserial-asyncio)"
            ) from exc
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self.port, baudrate=self.baudrate
        )
        logger.info("opened serial port %s @ %d baud", self.port, self.baudrate)

    async def close(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:  # pragma: no cover - needs real port
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # pragma: no cover - defensive
                logger.debug("error while closing serial port %s", self.port)

    async def read_line(self) -> str:  # pragma: no cover - needs real port
        if self._reader is None:
            raise RuntimeError("transport not open")
        raw = await self._reader.readline()
        if not raw:
            raise EOFError(f"serial port {self.port} closed")
        return raw.decode("ascii", errors="replace").rstrip("\r\n")

    async def write_line(self, line: str) -> None:  # pragma: no cover - real port
        if self._writer is None:
            raise RuntimeError("transport not open")
        self._writer.write((line + "\r\n").encode("ascii", errors="replace"))
        await self._writer.drain()
