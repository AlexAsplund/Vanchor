"""The ``vanchor`` console-script entry point.

Relocated out of the monolithic ``vanchor.app`` (issue #80). ``vanchor.app.main``
re-exports this for back-compat; the ``vanchor`` console script points here.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from ..core.config import apply_device_overrides, load
from .demo import apply_demo_mode
from .runtime import Runtime

logger = logging.getLogger("vanchor.app")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Vanchor-NG server")
    parser.add_argument("--config", default=None, help="YAML/JSON config file")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--model", default=None, choices=["simple", "fossen"])
    parser.add_argument("--hardware", action="store_true", help="use real serial devices")
    parser.add_argument("--nmea-tcp", action="store_true", help="accept NMEA over TCP")
    parser.add_argument("--demo", action="store_true",
                        help="demo mode: forced sim on the charted lake, seeded "
                             "moving scenario, ephemeral data dir, DEMO badge")
    parser.add_argument("--demo-readonly", action="store_true",
                        help="demo mode + every client pinned to observer (hosted demo)")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args(argv)

    config = load(args.config)
    demo = args.demo or args.demo_readonly
    if demo:
        apply_demo_mode(config, readonly=args.demo_readonly)
    else:
        # A saved device config (devices.json under data_dir) overrides the loaded
        # base hardware/nmea_tcp config before the runtime builds any device, so an
        # API-edited setup survives restarts. CLI flags below still win.
        apply_device_overrides(config)
    if args.host:
        config.server.host = args.host
    if args.port:
        config.server.port = args.port
    if args.model:
        config.sim.model = args.model
    if args.hardware:
        if demo:
            logger.warning("--hardware ignored in demo mode (forced sim)")
        else:
            config.hardware.enabled = True
    if args.nmea_tcp:
        if demo:
            logger.warning("--nmea-tcp ignored in demo mode (forced sim)")
        else:
            config.nmea_tcp.enabled = True
    if args.log_level:
        config.log_level = args.log_level

    import time as _time

    import uvicorn

    from ..ui.server import create_app

    # Boot-phase timings at INFO so a slow start (seen on the Pi without a
    # network connection) shows WHERE the time goes in the journal.
    _t0 = _time.monotonic()
    runtime = Runtime(config)
    logger.info("boot: Runtime constructed in %.1fs", _time.monotonic() - _t0)
    _t1 = _time.monotonic()
    app = create_app(runtime)
    logger.info("boot: app created in %.1fs", _time.monotonic() - _t1)

    # Optional HTTPS listener on a second port: secure-context browser APIs
    # (Screen Wake Lock, full PWA installs) need it. Best-effort -- a busy port
    # or missing cert/openssl logs a warning and plain HTTP is unaffected.
    tls_pair = None
    if config.server.https_port:
        from ..tls import ensure_tls_cert, port_free
        if not port_free(config.server.host, config.server.https_port):
            logger.warning("HTTPS port %d is in use; HTTPS disabled",
                           config.server.https_port)
        else:
            _t2 = _time.monotonic()
            tls_pair = ensure_tls_cert(config.data_dir,
                                       config.server.ssl_certfile,
                                       config.server.ssl_keyfile)
            logger.info("boot: TLS cert ready in %.1fs", _time.monotonic() - _t2)

    # Advertise over mDNS so a phone/PWA finds vanchor.local without an IP.
    advert = None
    if config.server.mdns:
        from .. import __version__
        from ..discovery import advertise
        props = {"version": __version__}
        if tls_pair:
            props["https_port"] = str(config.server.https_port)
        _t3 = _time.monotonic()
        advert = advertise(config.server.port, config.server.host, properties=props)
        logger.info("boot: mDNS advertise done in %.1fs", _time.monotonic() - _t3)

    log_level = (args.log_level or "info").lower()
    # WS keepalive tuned for a phone on the boat AP: uvicorn's defaults
    # (ping 20 s / timeout 20 s) DISCONNECT a client whose WiFi power-naps long
    # enough to delay one pong -- on an iPhone (esp. Low Power Mode) that made
    # the server close the socket every ~45 s ("data stale" flashes; confirmed
    # in a debug recording: ws error/close/open cycles at +0.6 s and +46.3 s
    # with the server perfectly healthy). Ping less often and tolerate slow
    # pongs; the app has its OWN ping + data-stale detection for real losses.
    _ws_keepalive = {"ws_ping_interval": 25.0, "ws_ping_timeout": 60.0}
    servers = [uvicorn.Server(uvicorn.Config(
        app, host=config.server.host, port=config.server.port, log_level=log_level,
        **_ws_keepalive))]
    if tls_pair:
        cert, key = tls_pair
        servers.append(uvicorn.Server(uvicorn.Config(
            app, host=config.server.host, port=config.server.https_port,
            log_level=log_level, ssl_certfile=cert, ssl_keyfile=key,
            **_ws_keepalive)))
        logger.info("HTTPS listening on port %d (cert: %s)",
                    config.server.https_port, cert)

    async def _serve_all() -> None:
        # One event loop for every listener (the Runtime's tasks/bus live on it).
        # We own the signal handling: uvicorn's per-server handlers would clobber
        # each other, leaving all but the last server unstoppable on Ctrl-C.
        import signal as _signal
        for srv in servers:
            srv.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        loop = asyncio.get_running_loop()

        def _stop() -> None:
            for srv in servers:
                srv.should_exit = True
        for sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except NotImplementedError:  # pragma: no cover - non-unix
                pass
        await asyncio.gather(*(srv.serve() for srv in servers))

    try:
        asyncio.run(_serve_all())
    finally:
        if advert is not None:
            advert.close()


if __name__ == "__main__":
    main()
