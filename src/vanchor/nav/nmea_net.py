"""An NMEA-0183-over-TCP server so phone nav apps can talk to Vanchor-NG.

Apps such as Navionics, iNavX and SignalK speak NMEA over a plain TCP socket
(the de-facto "TCP/IP NMEA" gateway, conventionally on port 10110). This module
exposes a tiny :class:`asyncio` server that:

* accepts any number of concurrent clients;
* validates inbound lines (must carry a correct ``*XX`` checksum) before
  forwarding onto the event bus as ``nmea.in``, so garbage/partial lines and
  trivially-crafted injection attempts are silently dropped;
* broadcasts outbound sentences to all connected clients via a non-blocking
  per-client outbound queue, so one slow phone client cannot stall publishers
  on the ``nmea.out`` topic.

The server stays decoupled from everything else: it only knows the bus.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..core import events
from ..core.events import EventBus
from .nmea import has_valid_checksum

logger = logging.getLogger("vanchor.nmea_net")

#: Topic the server listens on for sentences to push to connected clients.
NMEA_OUT = "nmea.out"

#: Per-client outbound queue capacity.  Oldest entries are dropped when full.
_QUEUE_SIZE = 200

#: Minimum seconds between "bad checksum" warning log messages (per server).
_REJECT_LOG_INTERVAL = 10.0


class NmeaTcpServer:
    """A multi-client NMEA-0183 TCP gateway bound to the event bus."""

    def __init__(
        self, bus: EventBus, host: str = "0.0.0.0", port: int = 10110
    ) -> None:
        self.bus = bus
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None
        # writer -> (outbound_queue, writer_task)
        self._clients: dict[
            asyncio.StreamWriter,
            tuple[asyncio.Queue[str | None], asyncio.Task[None]],
        ] = {}
        # Drop accounting
        self._total_drops: int = 0
        # Rate-limited reject logging state
        self._reject_count: int = 0
        self._last_reject_log: float = 0.0
        bus.subscribe(NMEA_OUT, self._on_nmea_out)

    # ---------------------------------------------------------------------- #
    # Public API
    # ---------------------------------------------------------------------- #

    @property
    def bound_port(self) -> int | None:
        """The actual port the server is listening on, or ``None`` if not
        started. Useful when constructed with ``port=0`` (ephemeral port)."""
        if self._server is None:
            return None
        for sock in self._server.sockets:
            return int(sock.getsockname()[1])
        return None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def total_drops(self) -> int:
        """Total number of outbound lines dropped due to a full client queue."""
        return self._total_drops

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        # Reflect the real port back (matters for port=0).
        if self.bound_port is not None:
            self.port = self.bound_port
        logger.info("NMEA TCP server listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server is None:
            return
        server, self._server = self._server, None
        server.close()
        try:
            await server.wait_closed()
        except Exception:  # pragma: no cover - defensive
            logger.exception("error while closing NMEA TCP server")
        # Drop every connected client: cancel writer tasks, close transports.
        for writer, (queue, task) in list(self._clients.items()):
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
            await self._close_writer(writer)
        self._clients.clear()
        logger.info("NMEA TCP server stopped")

    async def broadcast(self, sentence: str) -> None:
        """Enqueue ``sentence`` for delivery to all connected clients.

        Returns immediately (non-blocking).  Clients whose outbound queue is
        full have the oldest queued entry dropped to make room for the new one.
        """
        if not self._clients:
            return
        line: str = (
            sentence
            if sentence.endswith("\r\n")
            else sentence.rstrip("\r\n") + "\r\n"
        )
        for queue, _ in list(self._clients.values()):
            if queue.full():
                try:
                    queue.get_nowait()  # drop oldest to make room
                    self._total_drops += 1
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(line)
            except asyncio.QueueFull:
                # Another coroutine may have filled the slot between the two
                # calls above; count and move on.
                self._total_drops += 1

    # ---------------------------------------------------------------------- #
    # Internal helpers
    # ---------------------------------------------------------------------- #

    async def _on_nmea_out(self, sentence: str) -> None:
        await self.broadcast(sentence)

    async def _writer_loop(
        self,
        writer: asyncio.StreamWriter,
        queue: asyncio.Queue[str | None],
    ) -> None:
        """Per-client task: drain the outbound queue to the TCP socket."""
        try:
            while True:
                line = await queue.get()
                if line is None:  # sentinel: stop gracefully
                    break
                try:
                    writer.write(line.encode("ascii", "ignore"))
                    await writer.drain()
                except Exception:
                    logger.debug("NMEA client write error; dropping client")
                    break
        except asyncio.CancelledError:
            pass

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("NMEA client connected: %s", peer)

        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        task = asyncio.create_task(self._writer_loop(writer, queue))
        self._clients[writer] = (queue, task)

        try:
            while True:
                raw = await reader.readline()
                if not raw:  # EOF: client closed the connection
                    break
                line = raw.decode("ascii", "ignore").strip()
                if not line:
                    continue
                if line[0] in ("$", "!"):
                    if not has_valid_checksum(line):
                        self._reject_count += 1
                        now = time.monotonic()
                        if now - self._last_reject_log >= _REJECT_LOG_INTERVAL:
                            logger.warning(
                                "Dropped %d inbound TCP line(s) with bad/missing "
                                "checksum (last from %s: %r)",
                                self._reject_count,
                                peer,
                                line,
                            )
                            self._reject_count = 0
                            self._last_reject_log = now
                        continue
                    await self.bus.publish(events.NMEA_IN, line)
                else:
                    logger.debug("ignoring non-NMEA line from %s: %r", peer, line)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except Exception:  # pragma: no cover - defensive
            logger.exception("error serving NMEA client %s", peer)
        finally:
            # Signal the writer task to stop, then wait briefly.
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
            self._clients.pop(writer, None)
            await self._close_writer(writer)
            logger.info("NMEA client disconnected: %s", peer)

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter) -> None:
        try:
            if not writer.is_closing():
                writer.close()
            await writer.wait_closed()
        except Exception:  # pragma: no cover - defensive
            pass
