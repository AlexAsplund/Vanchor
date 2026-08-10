"""Captive-portal probe responder (port 80).

The boat's AP has no internet, so iOS marks the WiFi "No Internet Connection"
and RE-PROBES connectivity every ~30-60 s (``captive.apple.com``). Each failed
probe makes iOS deprioritize / re-evaluate the WiFi (WiFi Assist, cellular
fallback), which showed up as a ~45 s cycle of WebSocket drops against the boat
-- while the same phone against a home network (probes succeed) is rock solid.

Standard offline-hotspot cure: the AP's dnsmasq aliases the OS connectivity-
check hostnames to the boat (see ``vanchor-dnsmasq.conf``), and this tiny ASGI
app answers the probes on port 80 with EXACTLY what each OS expects, so the
phone considers the network online and stops the periodic re-probing.

The responses must be byte-exact-ish: Apple in particular shows the captive-
portal sheet unless ``hotspot-detect.html`` returns its canonical Success page.
Anything that is NOT a known probe path redirects to the real UI -- a nice side
effect: typing ``10.42.0.1`` (no port) in a browser lands in the app.

BENCH-VERIFY: real-device iOS/Android behavior can only be confirmed against
actual hardware on the boat AP.
"""
from __future__ import annotations

_APPLE_SUCCESS = b"<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"

# path -> (status, content-type, body). 204s carry no body.
PROBES: dict[str, tuple[int, str, bytes]] = {
    # Apple (iOS/macOS)
    "/hotspot-detect.html": (200, "text/html", _APPLE_SUCCESS),
    "/library/test/success.html": (200, "text/html", _APPLE_SUCCESS),
    # Android
    "/generate_204": (204, "text/plain", b""),
    "/gen_204": (204, "text/plain", b""),
    # Windows
    "/connecttest.txt": (200, "text/plain", b"Microsoft Connect Test"),
    "/ncsi.txt": (200, "text/plain", b"Microsoft NCSI"),
    # GNOME/NetworkManager
    "/check_network_status.txt": (200, "text/plain", b"NetworkManager is online\n"),
}


def make_captive_app(ui_port: int = 8000):
    """A dependency-free ASGI app: answer the probe paths, redirect the rest."""

    async def app(scope, receive, send) -> None:  # noqa: ANN001 - ASGI signature
        if scope["type"] != "http":
            return
        path = scope.get("path", "/")
        probe = PROBES.get(path)
        if probe is not None:
            status, ctype, body = probe
            headers = [(b"content-type", ctype.encode())]
            if status != 204:
                headers.append((b"content-length", str(len(body)).encode()))
            await send({"type": "http.response.start", "status": status,
                        "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return
        # Not a probe: bounce to the real UI on its port, same host.
        host = "10.42.0.1"
        for name, value in scope.get("headers", []):
            if name == b"host":
                host = value.decode("latin-1").split(":")[0]
                break
        location = f"http://{host}:{ui_port}/".encode()
        await send({"type": "http.response.start", "status": 302,
                    "headers": [(b"location", location),
                                (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})

    return app
