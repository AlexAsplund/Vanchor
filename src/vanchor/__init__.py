"""Vanchor-NG — a trolling-motor autopilot.

An asyncio + FastAPI backend driving a Fossen 3-DOF boat model (or real serial
hardware behind the same interfaces) from a vanilla-JS + Leaflet PWA. The data
flow is one loop: devices emit NMEA, the navigator parses it into a shared
``NavigationState``, control modes turn that state into motor setpoints, and the
motor (simulated or wired) applies them.

Subpackages:

- :mod:`vanchor.core` — domain models, config, state, event bus, PID, geodesy
- :mod:`vanchor.sim` — the boat physics simulator + simulated NMEA sensors
- :mod:`vanchor.hardware` — real serial devices + the pluggable driver registry
- :mod:`vanchor.nav` — NMEA parsing, routing, tracks, depth
- :mod:`vanchor.controller` — control modes, PID, calibration, safety
- :mod:`vanchor.ui` — the FastAPI server + the static PWA
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:  # the single source of truth is pyproject's [project] version
    __version__ = _pkg_version("vanchor-ng")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
