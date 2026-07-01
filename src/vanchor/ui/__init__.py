"""Web UI: the FastAPI server + the static PWA.

``server.py`` wraps a ``Runtime`` in the REST + WebSocket API (live telemetry
over ``/ws``, commands + config over ``/api/*``) and serves the vanilla-JS +
Leaflet single-page app in ``static/``. The app is an installable, offline-capable
PWA (service worker–cached shell) with no build step.
"""
