# `vanchor.ui`

<a id="vanchor.ui"></a>

# vanchor.ui

Web UI: the FastAPI server + the static PWA.

``server.py`` wraps a ``Runtime`` in the REST + WebSocket API (live telemetry
over ``/ws``, commands + config over ``/api/*``) and serves the vanilla-JS +
Leaflet single-page app in ``static/``. The app is an installable, offline-capable
PWA (service worker–cached shell) with no build step.


<a id="vanchor.ui.server"></a>

# vanchor.ui.server

FastAPI web UI: a smartphone-friendly map plus a telemetry/command channel.

The server is thin: it serves the static page, streams telemetry over a
WebSocket, and forwards commands to the runtime. All the interesting behaviour
lives in the controller and simulator.

