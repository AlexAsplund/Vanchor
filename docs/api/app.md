# `vanchor.app`

<a id="vanchor.app"></a>

# vanchor.app

Application wiring and entrypoint.

``Runtime`` builds the whole event-driven system from interchangeable parts and
starts every async loop. It is driven by an :class:`AppConfig` so the same code
runs the simulator, real serial hardware, or a network-fed setup -- selected by
configuration, not by code changes.

Run it with::

    python -m vanchor.app                       # serve the UI on :8000 (sim)
    python -m vanchor.app --config my.yaml       # load a config file
    python -m vanchor.app --hardware             # use real serial devices
    python -m vanchor.app --nmea-tcp             # also accept phone NMEA over TCP

<a id="vanchor.app.Runtime"></a>

## Runtime Objects

```python
class Runtime()
```

Owns every component and the background tasks that drive them.

<a id="vanchor.app.Runtime.update_boat"></a>

#### update\_boat

```python
def update_boat(fields: dict) -> dict
```

Update the boat profile and apply what can change live.

Also persists the change back into the active named profile (`75`) so the
existing ``POST /api/boat`` path and the profile store stay in sync.

<a id="vanchor.app.Runtime.boat_profiles_list"></a>

#### boat\_profiles\_list

```python
def boat_profiles_list() -> dict
```

``{active_id, profiles:[{id,name,...specs}, ...]}``.

<a id="vanchor.app.Runtime.boat_profiles_create"></a>

#### boat\_profiles\_create

```python
def boat_profiles_create(name: str, specs: dict | None = None) -> dict
```

Create a profile (specs default to the current active boat). Returns
``{id, ...}`` of the new profile.

<a id="vanchor.app.Runtime.boat_profiles_update"></a>

#### boat\_profiles\_update

```python
def boat_profiles_update(profile_id: str,
                         name: str | None = None,
                         specs: dict | None = None) -> dict | None
```

Update a profile's name/specs. If the edited profile is the active
one, also apply the new specs live. Returns the updated profile or None
if the id is unknown.

<a id="vanchor.app.Runtime.boat_profiles_activate"></a>

#### boat\_profiles\_activate

```python
def boat_profiles_activate(profile_id: str) -> dict | None
```

Make a profile active and apply its specs to the live sim. Returns
the applied boat profile dict, or None if the id is unknown.

<a id="vanchor.app.Runtime.boat_profiles_delete"></a>

#### boat\_profiles\_delete

```python
def boat_profiles_delete(profile_id: str) -> bool
```

Delete a profile (refuses the last one). If the deleted profile was
active, apply whatever profile is active afterwards.

<a id="vanchor.app.Runtime.create_backup"></a>

#### create\_backup

```python
def create_backup(client: dict | None = None,
                  *,
                  created_at: str | None = None) -> bytes
```

Build a versioned backup ZIP of this runtime's ``data_dir`` (boats,
depth map, devices, trips) plus the UI's ``client`` localStorage slice.

``created_at`` is an ISO8601 string the caller supplies (the endpoint
passes the request time); when omitted we use the injected clock to make
a UTC timestamp -- the backup module itself never calls ``datetime.now``.
Returns the raw ``.zip`` bytes.

<a id="vanchor.app.Runtime.restore_backup"></a>

#### restore\_backup

```python
def restore_backup(zip_bytes: bytes) -> dict
```

Restore a backup ZIP into ``data_dir`` and reload what it can LIVE.

Extracts the archive (overwriting the on-disk files), then refreshes the
in-memory state it can without a restart: re-loads the boat profiles +
the depth map from disk and re-applies the active profile, and reloads
the device config. Anything that can't be refreshed live sets
``restart_required``. Returns the backup-module result dict plus
``restart_required``. Raises :class:`ValueError` (-> 400) on a bad zip.

<a id="vanchor.app.Runtime.device_config"></a>

#### device\_config

```python
def device_config() -> dict
```

Current device/hardware config + the selectable options.

Shape matches what :meth:`set_device_config` persists, plus ``options``
(for the UI's selects) and ``restart_required`` (always ``False`` on a
plain read; a POST returns ``True`` because devices are rebuilt only on
restart, not hot-swapped).

<a id="vanchor.app.Runtime.set_device_config"></a>

#### set\_device\_config

```python
def set_device_config(payload: dict) -> dict
```

Validate, persist, and apply a device-config edit.

``payload`` is ``{"hardware": {...}, "nmea_tcp": {...}}`` (either key
optional). Validates source values + field types, writes
``devices.json``, and updates the in-memory ``config.hardware`` /
``config.nmea_tcp`` so a subsequent read reflects it. Devices are NOT
hot-swapped; the change applies on the next restart, so the returned
``restart_required`` is ``True``. Raises :class:`ValueError` on a bad
payload (the endpoint maps it to a 400).

<a id="vanchor.app.Runtime.apply_device_setting"></a>

#### apply\_device\_setting

```python
def apply_device_setting(kind: str, key: str, value) -> dict
```

Persist a device-menu setting for ``kind`` and apply it live if the
device is running. Persisted settings are read when the device is
(re)built, so a choice sticks even when the device isn't active yet.

<a id="vanchor.app.Runtime.run_device_action"></a>

#### run\_device\_action

```python
def run_device_action(kind: str,
                      name: str,
                      params: dict | None = None) -> dict
```

Run a device-menu action on the active device of ``kind``.

<a id="vanchor.app.Runtime.reload_devices"></a>

#### reload\_devices

```python
async def reload_devices() -> dict
```

Rebuild the device set LIVE (no process restart) so a device-config
change applies immediately. Builds + starts the NEW set first, and only
stops the old + swaps in if that succeeds — so a bad serial port leaves
the current devices running and the autopilot uninterrupted. Returns
``{applied: bool, error?: str}``.

<a id="vanchor.app.Runtime.return_to_launch"></a>

#### return\_to\_launch

```python
def return_to_launch() -> dict
```

Plan a water route from the boat to its launch point and follow it,
anchoring on arrival.

Returns the plan result dict. Synchronous + CPU/IO-heavy (water fetch +
routing); call it from an executor on the live path.

<a id="vanchor.app.Runtime.trip_start"></a>

#### trip\_start

```python
def trip_start(name: str | None = None) -> dict
```

Manually start a trip (overrides/replaces any active one).

<a id="vanchor.app.Runtime.trip_stop"></a>

#### trip\_stop

```python
def trip_stop() -> dict
```

Manually stop + persist the active trip. No-op when none is active.

<a id="vanchor.app.Runtime.battery_snapshot"></a>

#### battery\_snapshot

```python
def battery_snapshot() -> dict
```

Battery telemetry. From the sim battery, or zeros if none (hardware
battery monitor over the HAL will populate this later).

<a id="vanchor.app.Runtime.client_connected"></a>

#### client\_connected

```python
def client_connected() -> None
```

A UI client connected; clear any link failsafe.

<a id="vanchor.app.Runtime.client_activity"></a>

#### client\_activity

```python
def client_activity() -> None
```

Mark the link alive (any inbound client traffic).

<a id="vanchor.app.Runtime.client_disconnected"></a>

#### client\_disconnected

```python
def client_disconnected() -> None
```

A UI client disconnected.

<a id="vanchor.app.Runtime.evaluate_link_failsafe"></a>

#### evaluate\_link\_failsafe

```python
def evaluate_link_failsafe(now: float | None = None) -> bool
```

Engage hold-position if no UI client has been seen for the timeout
while underway. Returns True if it engaged on this call. Idempotent and
clock-injectable (pass ``now`` in tests).

<a id="vanchor.app.Runtime.evaluate_rtl_recommend"></a>

#### evaluate\_rtl\_recommend

```python
def evaluate_rtl_recommend() -> bool
```

Set ``state.rtl_recommended`` when the battery range has dropped to
within ``rtl_margin_m`` of the distance home (so the boat can *just* make
it back). If ``auto_rtl`` is set, engage RTL. Returns the new flag.

<a id="vanchor.app.Runtime.plan_route"></a>

#### plan\_route

```python
def plan_route(dest_lat: float,
               dest_lon: float,
               mode: str = "fastest",
               offset_m: float = 25.0) -> dict
```

Plan a water-only route from the boat's current position.

Synchronous and CPU/IO-heavy (Overpass fetch + shapely/networkx); the UI
endpoint calls it in an executor. Returns the API contract dict. Does NOT
start navigation.

<a id="vanchor.app.Runtime.cancel_route_plan"></a>

#### cancel\_route\_plan

```python
def cancel_route_plan() -> None
```

Request that an in-progress route plan abort ASAP (`54`).

<a id="vanchor.app.Runtime.plan_island_loop"></a>

#### plan\_island\_loop

```python
def plan_island_loop(click_lat: float,
                     click_lon: float,
                     offset_m: float = 20.0) -> dict
```

Plan a closed loop route encircling the island under ``(lat, lon)``.

Uses the same offline water chart/cache as :meth:`plan_route` (fetches
once if not cached). The boat's current position (or the sim start)
decides which water body is the basin. Does NOT start navigation -- it
returns waypoints for the route editor. Synchronous + CPU/IO-heavy; the
UI endpoint calls it in an executor. Returns
``{ok, waypoints, loop, message}``.

<a id="vanchor.app.Runtime.plan_survey"></a>

#### plan\_survey

```python
def plan_survey(polygon_latlon: list,
                spacing_m: float,
                angle_deg: float | None = None) -> dict
```

Plan a boustrophedon coverage route over a closed area polygon.

Pure CPU work (shapely); the UI endpoint calls it in an executor. Does
NOT start navigation -- it returns waypoints for the route editor.

<a id="vanchor.app.Runtime.plan_work_spots"></a>

#### plan\_work\_spots

```python
def plan_work_spots(polygon_latlon: list, spacing_m: float) -> dict
```

Generate Work Area spots: an even serpentine grid over a drawn area,
clipped to water (spots on land are dropped). Pure CPU (shapely) + the
offline water cache; the UI endpoint calls it in an executor. Returns
``{ok, waypoints, message}`` -- the UI loads these as the Work Area spots.

<a id="vanchor.app.Runtime.contour_route"></a>

#### contour\_route

```python
def contour_route(lat: float, lon: float, window_m: float = 700.0) -> dict
```

Build a route that follows the imported depth contour nearest
(lat, lon), chaining same-depth pieces into a continuous track (a closed
isobath comes back as a loop). Pure CPU (shapely); the UI endpoint calls it
in an executor. Returns ``{ok, waypoints, depth_m, loop, message}`` -- the
UI loads the waypoints as a route (patrol optional).

<a id="vanchor.app.Runtime.prefetch_chart"></a>

#### prefetch\_chart

```python
def prefetch_chart(bbox: list) -> dict
```

Fetch + cache the water polygon for a bbox so the boat can route
offline later. ``bbox`` is ``[south, west, north, east]``.

Synchronous and IO-heavy (Overpass fetch); call it in an executor.
Handles network failure gracefully.

<a id="vanchor.app.Runtime.list_charts"></a>

#### list\_charts

```python
def list_charts() -> dict
```

List cached chart bboxes + on-disk sizes (for the UI to show/manage).

<a id="vanchor.app.Runtime.clear_charts"></a>

#### clear\_charts

```python
def clear_charts() -> dict
```

Delete every cached chart. Returns how many were removed.

<a id="vanchor.app.Runtime.apply_tuned_gains"></a>

#### apply\_tuned\_gains

```python
def apply_tuned_gains(job: str, params: dict) -> None
```

Apply auto-tuned gains to the live controller (used by /api/tune).

<a id="vanchor.app.Runtime.depth_grid"></a>

#### depth\_grid

```python
def depth_grid(cell_m: float = 15.0, bbox=None, field: str = "depth") -> dict
```

Server-side gridded chart: bins soundings into ~``cell_m`` metre cells
averaging the value per cell, so the UI can paint an averaged colour chart
instead of 100k individual dots. ``cell_m`` is clamped to 2..200.

``bbox`` = (west, south, east, north) limits the grid to that viewport
window (Tier-1 windowing) so a large chart only ships what's on screen.
``field`` selects the layer: ``"depth"`` (default) or ``"hardness"``
(bottom-hardness, raw 0..127) -- same gridding, different source.

Returns ``{ok, field, cell_m, min_depth, max_depth, count, cells}``; the
chart changes slowly, so the UI polls this rather than the 5 Hz telemetry.

<a id="vanchor.app.Runtime.depth_contours"></a>

#### depth\_contours

```python
def depth_contours(bbox=None, limit: int = 20000) -> dict
```

Imported depth contours (isobath polylines) windowed to a
(west, south, east, north) bbox. Returns ``{ok, count, contours}`` where
each contour is ``{d: depth_m, pts: [[lat, lon], ...]}``.

<a id="vanchor.app.Runtime.depth_composition"></a>

#### depth\_composition

```python
def depth_composition(bbox=None, limit: int = 30000) -> dict
```

Imported bottom-composition polygons, windowed to a
(west, south, east, north) bbox. Returns ``{ok, count, polygons}`` where
each is ``{pct: 0..100, ring: [[lat, lon], ...]}`` -- rendered FILLED
(a vector polygon layer; not rasterised).

<a id="vanchor.app.Runtime.water_polygon"></a>

#### water\_polygon

```python
def water_polygon(bbox) -> dict
```

OSM water polygon(s) for a (west, south, east, north) bbox, used to
CLIP the depth overlays to water (don't draw composition over land). Uses
the same offline WaterCache as routing; fetches from Overpass + caches if
absent (so offline it needs the area pre-downloaded). Returns
``{ok, water}`` where water is GeoJSON-style MultiPolygon coords
``[[[ [lon,lat], ... ]=exterior, [ ... ]=hole, ... ], ...]`` (empty if none).

<a id="vanchor.app.Runtime.import_depth_map"></a>

#### import\_depth\_map

```python
def import_depth_map(filename: str,
                     data: bytes,
                     replace: bool = False) -> dict
```

Import soundings from an uploaded open-format depth file (CSV/XYZ or
GeoJSON). ``replace`` swaps the whole chart; otherwise the soundings are
merged in. Persists to ``depthmap.json`` so the import survives restarts.

