"""systemd sd_notify: READY=1 and WATCHDOG=1 via the NOTIFY_SOCKET datagram.
Silently no-ops when NOTIFY_SOCKET is not set (tests, dev)."""
import os
import socket


def _send(msg: str) -> None:
    path = os.environ.get("NOTIFY_SOCKET", "")
    if not path:
        return
    abstract = path.startswith("@")
    addr = ("\0" + path[1:]) if abstract else path
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(msg.encode(), addr)
    except OSError:
        pass


def ready() -> None:
    _send("READY=1")


def watchdog() -> None:
    _send("WATCHDOG=1")
