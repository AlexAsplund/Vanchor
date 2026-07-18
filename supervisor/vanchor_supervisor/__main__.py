"""Supervisor daemon entry point.

Usage:
    python -m vanchor_supervisor
    PYTHONPATH=/opt/vanchor-supervisor/current python -m vanchor_supervisor

Startup sequence:
    1. Load settings from /etc/vanchor-supervisor/config.json
    2. Check for pending.json and handle (clear if matches version, warn otherwise)
    3. Ensure state dir and token exist
    4. Bootstrap containers.json if missing
    5. Create SupervisorCore + CliDockerBackend
    6. Start API server thread
    7. Send sd_notify READY=1
    8. Main loop: send WATCHDOG=1 every 10 s
"""
from __future__ import annotations

import logging
import sys
import threading
import time

# Configure logging early so all modules pick it up
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("vanchor_supervisor")


def main() -> None:
    from . import SUPERVISOR_VERSION
    from .api import provision_token, serve
    from .backends import CliDockerBackend
    from .config import load_settings
    from .core import SupervisorCore
    from .selfupdate import clear_pending, read_pending
    from . import sdnotify

    log.info("vanchor-supervisor %s starting", SUPERVISOR_VERSION)

    settings = load_settings()
    log.info(
        "Settings loaded: listen=%s:%d state=%s",
        settings.listen_host, settings.listen_port, settings.state_dir,
    )

    # Handle pending.json (boot-count rollback outcome)
    import os
    from pathlib import Path
    install_root = Path(settings.install_root)
    pending = read_pending(install_root)
    if pending is not None:
        pending_version = pending.get("target")
        if pending_version == SUPERVISOR_VERSION:
            log.info(
                "Startup OK: running as expected version %s — clearing pending.json",
                SUPERVISOR_VERSION,
            )
            clear_pending(install_root)
        else:
            log.warning(
                "Pending version %r does not match running version %r — "
                "guard.py may have already rolled back; leaving pending.json",
                pending_version,
                SUPERVISOR_VERSION,
            )

    # Provision token
    backend = CliDockerBackend()
    try:
        volume_mp = backend.volume_mountpoint(settings.data_volume)
    except Exception as exc:
        log.warning("Could not get volume mountpoint for token provisioning: %s", exc)
        volume_mp = "/tmp/vanchor-data"  # fallback for dev

    try:
        provision_token(settings.state_dir, volume_mp)
    except Exception as exc:
        log.error("Token provisioning failed: %s", exc)
        sys.exit(1)

    # Create core
    core = SupervisorCore(settings=settings, backend=backend)
    log.info("SupervisorCore ready — %d container(s) configured", len(core.containers()))

    # Ensure containers with restart policies are running (first-boot reconcile).
    try:
        core.ensure_running()
    except Exception as exc:
        log.error("ensure_running failed: %s", exc)

    # Start API server
    server = serve(core, settings)
    server_thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="api-server",
    )
    server_thread.start()
    log.info("API listening on %s:%d", settings.listen_host, settings.listen_port)

    # Signal systemd: ready
    sdnotify.ready()

    # Main loop
    log.info("Entering watchdog loop")
    try:
        while True:
            time.sleep(10)
            sdnotify.watchdog()
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down")
    finally:
        server.shutdown()
        log.info("Supervisor stopped")


if __name__ == "__main__":
    main()
