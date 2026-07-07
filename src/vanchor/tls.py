"""TLS certificate plumbing for the optional HTTPS listener.

HTTPS matters on the boat because browsers gate *secure-context* APIs on it —
the Screen Wake Lock API and full service-worker/PWA installs don't work over
the plain-HTTP LAN default. vanchor therefore serves the same app on a second
HTTPS port (see ``ServerConfig.https_port``).

Certificate resolution, in order:
1. ``server.ssl_certfile`` / ``server.ssl_keyfile`` configured -> used verbatim
   (bring your own cert, e.g. one your devices already trust).
2. Otherwise a self-signed certificate with **CN=vanchor.local** (SANs:
   ``vanchor.local``, ``localhost``, ``127.0.0.1``) is auto-generated ONCE into
   ``<data_dir>/tls/`` via the ``openssl`` binary and reused across restarts —
   so after you accept (or install) it on a device once, it stays valid.

Everything is best-effort: no openssl, unreadable files, or a busy port merely
logs a warning and skips HTTPS — plain HTTP is never affected.
"""
from __future__ import annotations

import logging
import socket
import subprocess
from pathlib import Path

logger = logging.getLogger("vanchor.tls")

CERT_NAME = "vanchor.crt"
KEY_NAME = "vanchor.key"
_SUBJECT_CN = "vanchor.local"
_DAYS = 3650  # ~10 years; it's a LAN cert, rotation is a re-generate away


def ensure_tls_cert(data_dir: str | Path,
                    certfile: str = "", keyfile: str = "") -> tuple[str, str] | None:
    """Return ``(certfile, keyfile)`` paths ready for the HTTPS listener.

    Configured paths win (both must exist). Otherwise generate-or-reuse the
    self-signed pair under ``<data_dir>/tls/``. Returns ``None`` (with a logged
    warning) when no usable pair can be produced.
    """
    if certfile or keyfile:
        c, k = Path(certfile), Path(keyfile)
        if c.is_file() and k.is_file():
            return (str(c), str(k))
        logger.warning("configured TLS files missing (cert=%s key=%s); HTTPS disabled",
                       certfile, keyfile)
        return None

    tls_dir = Path(data_dir) / "tls"
    cert, key = tls_dir / CERT_NAME, tls_dir / KEY_NAME
    if cert.is_file() and key.is_file():
        return (str(cert), str(key))

    tls_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert),
        "-days", str(_DAYS), "-subj", f"/CN={_SUBJECT_CN}",
        "-addext", f"subjectAltName=DNS:{_SUBJECT_CN},DNS:localhost,IP:127.0.0.1",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    except FileNotFoundError:
        logger.warning("openssl not found; cannot auto-generate a TLS cert -> HTTPS disabled")
        return None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        logger.warning("TLS cert generation failed (%s); HTTPS disabled: %s",
                       exc, detail.decode(errors="replace")[:200])
        return None
    try:
        key.chmod(0o600)
    except OSError:  # pragma: no cover - permissions best-effort
        pass
    logger.info("generated self-signed TLS cert (CN=%s) at %s", _SUBJECT_CN, tls_dir)
    return (str(cert), str(key))


def port_free(host: str, port: int) -> bool:
    """True if we can bind (host, port) right now — used to skip HTTPS
    gracefully when the port is already taken."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
        return True
    except OSError:
        return False
