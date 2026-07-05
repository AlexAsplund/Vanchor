"""NMEA-TCP connector — the reference connector wrapping :class:`NmeaTcpServer`.

This connector is the back-compat bridge between the legacy ``cfg.nmea_tcp``
config block and the new connector framework. The Runtime auto-arms it when
``cfg.nmea_tcp.enabled`` is set and no explicit grant exists in
``connectors.json`` (legacy consent migration, Task 2).

The server itself is unchanged. Wiring goes THROUGH the
:class:`~vanchor.connectors.context.ConnectorContext` so enforcement is real:

* **Consumes** – a ``ctx.subscribe("nmea.out", ...)`` call fires the allowlist
  check (raises if the manifest omits ``nmea.out``).  The
  :class:`~vanchor.nav.nmea_net.NmeaTcpServer` already subscribed to the real
  bus in ``__init__``; the context subscription is the *enforcement gate*.
* **Produces** – inbound client lines that the server would normally publish
  directly onto the bus are intercepted via a thin bus proxy and routed through
  ``ctx.publish("nmea.in", …)`` so the produces ∩ ingress allowlist check fires.

Settings: ``host`` (default ``"0.0.0.0"``), ``port`` (default ``10110``).
"""

from __future__ import annotations

import logging
from typing import Any

from ..core import events
from ..nav.nmea_net import NmeaTcpServer
from .base import Connector, ConnectorManifest
from .context import ConnectorContext
from .registry import register_connector

logger = logging.getLogger("vanchor.connectors.nmea_tcp")

# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #

MANIFEST = ConnectorManifest(
    name="nmea-tcp",
    label="NMEA-TCP Bridge",
    description=(
        "Exposes the NMEA-0183 stream over TCP (default port 10110) so phone "
        "navigation apps (Navionics, iNavX, OpenCPN) can connect to Vanchor-NG. "
        "It reads outbound NMEA sentences and forwards inbound sentences from "
        "connected clients back onto the internal bus."
    ),
    consumes=("nmea.out",),
    produces=("nmea.in",),
    control=False,
    grant_lines=(
        "Read outbound NMEA-0183 sentences (GPS, heading, depth) and stream "
        "them to connected clients",
        "Forward inbound NMEA lines from clients (checksummed only) back onto "
        "the internal bus",
    ),
)


# --------------------------------------------------------------------------- #
# Connector
# --------------------------------------------------------------------------- #


class NmeaTcpConnector(Connector):
    """A :class:`~vanchor.connectors.base.Connector` that wraps
    :class:`~vanchor.nav.nmea_net.NmeaTcpServer`.

    The server is wired THROUGH the :class:`ConnectorContext`:

    * ``nmea.out`` outbound sentences reach all clients via the server's direct
      bus subscription (installed in ``NmeaTcpServer.__init__``).  The connector
      calls ``ctx.subscribe("nmea.out", …)`` solely for the *enforcement gate*:
      this raises :exc:`PermissionError` if ``nmea.out`` is not in ``consumes``.
    * ``nmea.in`` inbound sentences from clients: the server's bus reference is
      replaced with a thin proxy that intercepts ``nmea.in`` publishes and routes
      them through ``ctx.publish`` so the produces ∩ ingress allowlist check
      fires (Global Constraint 6).
    """

    manifest = MANIFEST

    def __init__(self, host: str = "0.0.0.0", port: int = 10110) -> None:
        self._host = host
        self._port = port
        self._server: NmeaTcpServer | None = None
        self._ctx: ConnectorContext | None = None

    async def start(self, ctx: ConnectorContext) -> None:
        """Start the TCP server and wire it through the context."""
        self._ctx = ctx
        bus = ctx._bus

        # Create the server on the real bus. NmeaTcpServer.__init__ subscribes
        # to nmea.out directly; that subscription is the delivery path for
        # outbound sentences. We DO NOT replicate it via ctx — the enforcement
        # gate (below) is the extra ctx.subscribe call.
        self._server = NmeaTcpServer(bus, host=self._host, port=self._port)

        # Enforcement gate for the consumes side: raises PermissionError if
        # "nmea.out" is not in the manifest's consumes list, so a mis-declared
        # manifest fails loudly at start time rather than silently passing data.
        ctx.subscribe("nmea.out", lambda _sentence: None)

        # Produces enforcement: intercept the server's inbound-publish path and
        # route it through ctx.publish so the produces ∩ INGRESS allowlist check
        # fires.  Only nmea.in is intercepted; any other publish falls through to
        # the real bus unchanged.
        _ctx_ref = ctx

        class _InboundProxy:
            """Thin shim: routes ``nmea.in`` through the connector context;
            everything else (including future bus methods) delegates to the
            real EventBus so NmeaTcpServer stays functional."""

            def subscribe(self, topic: str, handler: Any) -> None:
                bus.subscribe(topic, handler)

            async def publish(self, topic: str, payload: Any) -> None:
                if topic == events.NMEA_IN:
                    # Enforcement: raises PermissionError if "nmea.in" is not in
                    # produces ∩ INGRESS_TOPICS (which it is for this manifest).
                    await _ctx_ref.publish(topic, payload)
                else:
                    await bus.publish(topic, payload)

        self._server.bus = _InboundProxy()  # type: ignore[assignment]

        await self._server.start()
        logger.info(
            "NMEA-TCP connector started on %s:%s",
            self._host,
            self._server.bound_port,
        )

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop()
            self._server = None
        self._ctx = None
        logger.info("NMEA-TCP connector stopped")

    def status(self) -> dict:
        if self._server is None:
            return {"running": False, "port": None, "clients": 0}
        return {
            "running": True,
            "port": self._server.bound_port,
            "clients": self._server.client_count,
        }

    def debug(self) -> str:
        if self._server is None:
            return "NmeaTcpConnector: not started"
        srv = self._server
        return (
            f"NmeaTcpConnector\n"
            f"  bound_port : {srv.bound_port}\n"
            f"  clients    : {srv.client_count}\n"
            f"  total_drops: {srv.total_drops}\n"
        )


# --------------------------------------------------------------------------- #
# Factory + registration
# --------------------------------------------------------------------------- #


def _build(settings: dict) -> Connector:
    """Factory: build an :class:`NmeaTcpConnector` from persisted settings."""
    host = str(settings.get("host", "0.0.0.0"))
    port = int(settings.get("port", 10110))
    return NmeaTcpConnector(host=host, port=port)


register_connector(
    "nmea-tcp",
    _build,
    label="NMEA-TCP Bridge",
)
