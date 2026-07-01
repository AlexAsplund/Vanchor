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
