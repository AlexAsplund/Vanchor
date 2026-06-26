# Routing & Weather backend API

The backend contracts for the smart "take me here" water router and the realistic
variable-weather (wind / current / gust) model. The web UI builds against these.

---

## `POST /api/route/plan` — smart water routing

Plans a **water-only** route (avoiding land/islands) from the boat's **current
position** to a destination and returns waypoints. It does **not** start
navigation — the UI loads the waypoints into the route editor *unstarted* for the
skipper to review/edit and then press the existing **Go** (`goto`).

The start point is the boat's current position (`runtime.state.position`),
falling back to the configured sim start (`sim.start_lat/lon`) when there's no
fix.

### Request body

```json
{
  "dest_lat": 59.6643,
  "dest_lon": 13.3687,
  "mode": "fastest",
  "shoreline_offset_m": 25.0
}
```

| field                | type                         | notes                                              |
| -------------------- | ---------------------------- | -------------------------------------------------- |
| `dest_lat`           | float (required)             | destination latitude                               |
| `dest_lon`           | float (required)             | destination longitude                              |
| `mode`               | `"fastest"` \| `"shoreline"` | default `"fastest"`                                |
| `shoreline_offset_m` | float                        | metres off shore to hug in `shoreline` mode (def 25) |

### Response

```json
{
  "ok": true,
  "waypoints": [
    {"name": "WP1", "lat": 59.66275, "lon": 13.32247},
    {"name": "WP2", "lat": 59.6623,  "lon": 13.3555},
    {"name": "DEST","lat": 59.6643,  "lon": 13.3687}
  ],
  "message": "Planned fastest route with 3 waypoints."
}
```

- `ok` — `true` on success.
- `waypoints` — ordered `{name, lat, lon}`; first is `WP1`, last is `DEST`.
- `message` — human-readable status (also carries error/fallback text).

On failure `ok` is `false`, `waypoints` is `[]`, and `message` explains why, e.g.:

- `"dest_lat and dest_lon are required."`
- `"Destination is on land or outside known water."`
- `"No water route to the destination."`
- `"No offline chart for this area; connect once to download it."` (offline, no cache)

### Modes

- **`fastest`** — exact shortest navigable water path via a visibility graph over
  the water polygon (shapely + networkx). Bends occur only at shore/island
  vertices, so the route is naturally a short list of waypoints.
- **`shoreline`** — heads to the nearest shore (ending `shoreline_offset_m` m
  off), hugs that offset ring toward the destination, and cuts straight in once
  there's clear open-water line-of-sight. If the water is too narrow for the
  offset ring (or the entry/exit are on different basins), it **falls back to
  `fastest`** and says so in `message` (it never blocks the request).

### Data & caching

Water geometry comes from OpenStreetMap via Overpass (`natural=water`, relation
aware), projected to a metric UTM CRS and cached as WKB under
`<data_dir>/water_cache/`. The first plan for an area fetches online (a few
seconds); subsequent plans covered by the cache run fully offline. The endpoint
runs the (CPU/IO-heavy) planning in an executor so the telemetry loop isn't
blocked.

---

## Weather: presets, live tuning, slow variability (task #44)

On top of the existing static wind/current + fast OU gusts, the simulator now
adds a **slow** OU wander of wind speed **and** wind direction (and optionally
current), controlled by a `wind_variability` / `current_variability` amount in
`[0, 1]` (`0` = perfectly steady). Gusts still ride on top. The evolving values
are written live into the telemetry `environment` block.

### `GET /api/weather/presets`

```json
{
  "presets": [
    {
      "id": "lake",
      "label": "Lake (gusty wind, no current)",
      "current_speed": 0.0,
      "current_dir": 0.0,
      "wind_speed": 4.0,
      "wind_dir": 200.0,
      "gust_amplitude_mps": 1.5,
      "wind_variability": 0.5,
      "current_variability": 0.0
    }
  ]
}
```

Preset ids: `calm`, `lake`, `river`, `coastal`. Each object has exactly the
fields shown above.

### Command: `set_environment` (extended)

Sent to `POST /api/command` (or over the `/ws` socket). Now accepts the two
variability fields in addition to the existing ones; any subset may be supplied:

```json
{
  "type": "set_environment",
  "current_speed": 0.2,
  "current_dir": 90.0,
  "wind_speed": 6.0,
  "wind_dir": 220.0,
  "gust_amplitude_mps": 1.5,
  "wind_variability": 0.4,
  "current_variability": 0.1
}
```

The values supplied become the new steady **base**, and the slow wander resets to
wander around them.

### Command: `weather_preset`

Applies a named preset to the live sim environment:

```json
{ "type": "weather_preset", "id": "lake" }
```

Unknown ids are ignored (logged as a warning).

### Telemetry `environment` block

The streamed telemetry (`/api/state`, `/ws`) `environment` object now includes
the live-evolving values plus the variability amounts:

```json
"environment": {
  "current_speed": 0.0,
  "current_dir": 0.0,
  "wind_speed": 4.12,
  "wind_dir": 203.4,
  "gust_amplitude_mps": 1.5,
  "wind_variability": 0.5,
  "current_variability": 0.0,
  "wind_gust_now": 4.83
}
```

`wind_speed`, `wind_dir` and `current_speed` update live as the slow wander
evolves; `wind_gust_now` is the instantaneous gusty wind on top.

### Config

`EnvironmentConfig` (and `DEFAULT_CONFIG_YAML` / `vanchor.example.yaml`) gain
`wind_variability` and `current_variability` (both default `0.0` = steady).
