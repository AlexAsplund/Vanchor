# `vanchor.ui`

<a id="vanchor.ui"></a>

# vanchor.ui


<a id="vanchor.ui.server"></a>

# vanchor.ui.server

FastAPI web UI: a smartphone-friendly map plus a telemetry/command channel.

The server is thin: it serves the static page, streams telemetry over a
WebSocket, and forwards commands to the runtime. All the interesting behaviour
lives in the controller and simulator.

