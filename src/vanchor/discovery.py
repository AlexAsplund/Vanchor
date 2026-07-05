"""mDNS / Bonjour advertisement so a phone or the PWA auto-discovers the boat as
``vanchor.local`` without anyone typing an IP -- the SignalK/OpenPlotter
convention. Uses python-zeroconf; if it's missing or registration fails this is a
graceful no-op (the server still runs, you just type the IP).
"""
from __future__ import annotations

import logging
import socket

logger = logging.getLogger("vanchor.discovery")


class Advertisement:
    """Handle for a live mDNS registration; call :meth:`close` to withdraw it."""

    def __init__(self, zc, info) -> None:
        self._zc = zc
        self._info = info

    def close(self) -> None:
        try:
            self._zc.unregister_service(self._info)
            self._zc.close()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass


def _primary_ip(host: str) -> str | None:
    """The LAN IP to advertise. A specific bind host is used as-is (unless it's a
    wildcard/loopback); otherwise discover the primary outbound interface IP."""
    if host and host not in ("0.0.0.0", "::", "", "127.0.0.1", "localhost"):
        return host
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packet sent; just picks the route's src IP
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return None


def advertise(port: int, host: str = "0.0.0.0", *, name: str = "Vanchor",
              hostname: str = "vanchor", properties: dict | None = None):
    """Advertise the HTTP UI over mDNS. Returns an :class:`Advertisement` (call
    ``.close()`` on shutdown) or ``None`` if discovery is unavailable."""
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except Exception as exc:  # noqa: BLE001 - zeroconf not installed
        logger.info("mDNS unavailable (%s); skipping discovery advertisement", exc)
        return None
    ip = _primary_ip(host)
    if ip is None:
        logger.info("mDNS: no routable IP found; skipping advertisement")
        return None
    try:
        props = {str(k): str(v) for k, v in (properties or {}).items()}
        info = ServiceInfo(
            "_http._tcp.local.",
            f"{name}._http._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=int(port),
            properties=props,
            server=f"{hostname}.local.",
        )
        zc = Zeroconf()
        zc.register_service(info)
        logger.info("mDNS: advertising %r at %s:%d as %s.local", name, ip, port, hostname)
        return Advertisement(zc, info)
    except Exception as exc:  # noqa: BLE001 - registration must never crash startup
        logger.warning("mDNS advertisement failed (%s); continuing without it", exc)
        return None
