"""An NMEA-0183-over-TCP server so phone nav apps can talk to Vanchor-NG.

Apps such as Navionics, iNavX and SignalK speak NMEA over a plain TCP socket
(the de-facto "TCP/IP NMEA" gateway, conventionally on port 10110). This module
exposes a tiny :class:`asyncio` server that:

* accepts any number of concurrent clients;
* forwards every inbound line that looks like NMEA (starts with ``$`` or ``!``)
  onto the event bus as ``nmea.in``, so the navigator consumes phone-sourced
  fixes/headings exactly like serial ones;
* broadcasts outbound sentences to all connected clients, both via
  :meth:`broadcast` and automatically by subscribing to the ``nmea.out`` topic
  (the controller/simulator can publish there to feed the phone its position).

The server stays decoupled from everything else: it only knows the bus.
"""

from __future__ import annotations

import asyncio
import logging

from ..core import events
from ..core.events import EventBus

logger = logging.getLogger("vanchor.nmea_net")

#: Topic the server listens on for sentences to push to connected clients.
NMEA_OUT = "nmea.out"


class NmeaTcpServer:
    """A multi-client NMEA-0183 TCP gateway bound to the event bus."""

    def __init__(
        self, bus: EventBus, host: str = "0.0.0.0", port: int = 10110
    ) -> None:
        self.bus = bus
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        bus.subscribe(NMEA_OUT, self._on_nmea_out)

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
        return len(self._writers)

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
        # Drop every connected client.
        for writer in list(self._writers):
            await self._close_writer(writer)
        self._writers.clear()
        logger.info("NMEA TCP server stopped")

    async def broadcast(self, sentence: str) -> None:
        """Send ``sentence`` (a single NMEA line) to all connected clients.

        A trailing CR/LF is appended if missing. Clients that error out are
        dropped silently."""
        if not self._writers:
            return
        line = sentence if sentence.endswith("\r\n") else sentence.rstrip("\r\n") + "\r\n"
        data = line.encode("ascii", "ignore")
        dead: list[asyncio.StreamWriter] = []
        for writer in list(self._writers):
            try:
                writer.write(data)
                await writer.drain()
            except Exception:
                logger.debug("dropping NMEA client during broadcast")
                dead.append(writer)
        for writer in dead:
            await self._close_writer(writer)

    async def _on_nmea_out(self, sentence: str) -> None:
        await self.broadcast(sentence)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("NMEA client connected: %s", peer)
        self._writers.add(writer)
        try:
            while True:
                raw = await reader.readline()
                if not raw:  # EOF: client closed the connection
                    break
                line = raw.decode("ascii", "ignore").strip()
                if not line:
                    continue
                if line[0] in ("$", "!"):
                    await self.bus.publish(events.NMEA_IN, line)
                else:
                    logger.debug("ignoring non-NMEA line from %s: %r", peer, line)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except Exception:  # pragma: no cover - defensive
            logger.exception("error serving NMEA client %s", peer)
        finally:
            self._writers.discard(writer)
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
