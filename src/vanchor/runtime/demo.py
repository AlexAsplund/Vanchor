"""Demo-mode helpers: seed waypoints and config posture override.

``apply_demo_mode`` forces the forced-sim / ephemeral-data-dir posture that
``main()`` uses for the ``--demo`` flag, and that ``Runtime.__init__`` re-applies
when ``demo.enabled`` is set via YAML/env without the CLI flag.
``demo_route_waypoints`` returns the triangle route seeded into a fresh demo
session.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.config import HardwareConfig, NmeaTcpConfig

if TYPE_CHECKING:
    from ..core.config import AppConfig

logger = logging.getLogger("vanchor.app")


def demo_route_waypoints(lat: float, lon: float) -> list[dict]:
    """A small ~600 m looping triangle NE of the start (fits the charted lake).
    Offsets match the README screenshot route (scripts/take_screenshots.py)."""
    return [
        {"name": "Demo 1", "lat": lat + 0.0022, "lon": lon + 0.0018},
        {"name": "Demo 2", "lat": lat + 0.0034, "lon": lon + 0.0075},
        {"name": "Demo 3", "lat": lat + 0.0012, "lon": lon + 0.0110},
    ]


def apply_demo_mode(config: "AppConfig", *, readonly: bool = False,
                    data_dir: str | None = None) -> "AppConfig":
    """Force the demo posture onto ``config`` (in place) and return it.

    - hardware OFF, every device source pinned to the simulator: no real
      serial/i2c device is ever probed, regardless of devices.json or env.
    - ephemeral data dir (``data_dir`` arg, else a fresh mkdtemp) so a demo
      never writes the operator's vanchor_data/. If the CWD has the repo's
      imported depth chart, symlink it (read-only) into the demo dir so the
      charted lake renders; a chartless install still works (sim depth builds
      the live map).
    - boat starts on the charted demo lake; time_scale stays 1.0.
    """
    config.demo.enabled = True
    config.demo.readonly = bool(readonly)
    # Forced sim: overwrite the whole hardware block (nothing may probe a port).
    config.hardware = HardwareConfig()          # enabled=False, all sources None -> "sim"
    config.hardware.battery_source = "sim"
    config.nmea_tcp = NmeaTcpConfig()           # enabled=False
    config.watchdog.enabled = False
    # World: charted demo lake, real-time physics.
    config.sim.start_lat = config.demo.start_lat
    config.sim.start_lon = config.demo.start_lon
    config.sim.time_scale = 1.0
    # Ephemeral data dir + best-effort chart seeding (mirrors take_screenshots).
    src_dir = Path(config.data_dir)             # usually ./vanchor_data
    config.data_dir = data_dir or tempfile.mkdtemp(prefix="vanchor-demo-")
    dst = Path(config.data_dir)
    for name in ("depthchart.npz", "depthmap.json"):
        s = src_dir / name
        if s.exists() and not (dst / name).exists():
            try:
                (dst / name).symlink_to(s.resolve())
            except OSError:
                pass  # best-effort: chartless demo still works
    wc_src = src_dir / "water_cache"
    if wc_src.is_dir():
        wc = dst / "water_cache"
        wc.mkdir(exist_ok=True)
        for f in wc_src.iterdir():
            if not (wc / f.name).exists():
                try:
                    (wc / f.name).symlink_to(f.resolve())
                except OSError:
                    pass  # best-effort
    logger.info("DEMO MODE: sim-only, data dir %s (ephemeral)", config.data_dir)
    return config
