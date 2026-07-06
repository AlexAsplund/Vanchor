# `vanchor.nav`

<a id="vanchor.nav"></a>

# vanchor.nav

Navigation: from NMEA in to routes and derived geometry.

The navigator parses inbound NMEA (RMC/GGA/HDM/HDT/APB/DPT…) into the shared
``NavigationState`` — the single parse point for both simulated and real
sensors. The rest of the package builds on that: routes + waypoints,
water-following "take me here" routing, along-contour track generation, depth
maps + soundings, track/trip logging, the sensor guard, and the NMEA-over-TCP
bridge for feeding an external GPS/plotter.


<a id="vanchor.nav.contour_route"></a>

# vanchor.nav.contour\_route

Along-contour route: click a depth contour on the chart -> a track that follows
that isobath.

Imported contours arrive as many short ``{d, pts}`` polyline pieces all sharing a
handful of discrete depths. Tracing the single clicked piece would give a stub, so
the smart bit here is to **chain together the same-depth pieces** whose endpoints
coincide into one continuous line through the click, then return it as an ordered,
simplified route (closed isobaths come back as a loop). The caller windows the
contours around the click so this stays fast even with tens of thousands loaded.

Pure CPU (shapely); run it in an executor.

<a id="vanchor.nav.contour_route.contour_route_near"></a>

#### contour\_route\_near

```python
def contour_route_near(lat: float,
                       lon: float,
                       contours: list[dict],
                       *,
                       max_snap_m: float = 120.0,
                       max_waypoints: int = 80,
                       simplify_m: float = 10.0,
                       join_tol_m: float = 12.0) -> dict
```

Nearest imported contour to (lat, lon), chained with its same-depth pieces
into a continuous track and returned as an ordered route. ``contours`` are the
windowed ``[{d, pts:[[lat,lon],...]}]``. Returns
``{ok, waypoints:[{name,lat,lon}], depth_m, loop, message}``.


<a id="vanchor.nav.depth"></a>

# vanchor.nav.depth

Depth-map recorder: accumulate (position, depth) soundings as the boat moves.

This is the data behind the toggleable depth-map overlay -- a breadcrumb of
soundings that builds up automatically. (Interpolating a continuous contour
surface from these points is a future enhancement.)

<a id="vanchor.nav.depth.DepthMap"></a>

## DepthMap Objects

```python
class DepthMap()
```

<a id="vanchor.nav.depth.DepthMap.save"></a>

#### save

```python
def save(path: str) -> None
```

Persist the soundings (small; called often by the recorder).

<a id="vanchor.nav.depth.DepthMap.save_chart"></a>

#### save\_chart

```python
def save_chart(path: str) -> None
```

Persist the STATIC imported chart (hardness/contours/composition);
written only on import, not on every recorded sounding.

<a id="vanchor.nav.depth.DepthMap.as_list"></a>

#### as\_list

```python
def as_list(limit: int = 600) -> list[list[float]]
```

Most recent soundings as [[lat, lon, depth], ...] for the UI.

<a id="vanchor.nav.depth.DepthMap.contours_in"></a>

#### contours\_in

```python
def contours_in(bbox: tuple[float, float, float, float] | None = None,
                limit: int = 20000) -> list[dict]
```

Imported depth contours, windowed to a (west, south, east, north)
bbox -- a contour polyline is kept if any vertex falls inside. Capped at
``limit`` so a zoomed-out view can't ship the whole (huge) chart.

<a id="vanchor.nav.depth.DepthMap.composition_in"></a>

#### composition\_in

```python
def composition_in(bbox: tuple[float, float, float, float] | None = None,
                   limit: int = 30000) -> list[dict]
```

Imported composition polygons, windowed to a (west, south, east,
north) bbox -- a polygon is kept if any ring vertex falls inside.

<a id="vanchor.nav.depth.DepthMap.as_grid"></a>

#### as\_grid

```python
def as_grid(cell_m: float = 15.0,
            max_cells: int = 3000,
            interpolate: bool = True,
            interp_radius: int = 5,
            interp_min_dirs: int = 6,
            interp_power: float = 2.0,
            radiate: bool = True,
            radiate_radius_m: float = 30.0,
            bbox: tuple[float, float, float, float] | None = None,
            source: list[tuple[float, float, float]] | None = None) -> dict
```

Bin every sounding into a square grid (~``cell_m`` metres) and average
depth per cell, returning a compact structure for the UI to colour-scale.

Soundings span a small area, so we bin in a local metric frame using a
flat metres-per-degree conversion at the data's mean latitude (cheap and
accurate over the breadcrumb's extent -- no pyproj needed). Binning is a
single O(n) pass over the points.

The returned cell count is capped at ``max_cells`` by growing the
effective cell size (doubling until the bins fit), and the cell size
actually used is reported back so the client can label its colour scale.

Two fill passes spread the measured data into the empty cells around it,
in order of confidence:

* **Radiate (nearest-neighbour / Voronoi).** When ``radiate`` is on
  (default), each empty cell within ``radiate_radius_m`` metres of a
  measured cell is assigned the depth of the *nearest* measured cell --
  the bottom is assumed roughly constant out to the Voronoi boundary
  where a different reading becomes nearer. The radius is bounded (a
  few cells) so one ping can't paint a whole lake and can't bleed into a
  neighbouring waterbody across an empty gap wider than the radius.
  Radiated cells are confident assumptions: ``"kind": "radiated"`` and
  ``"est": false``.

* **Interpolate (enclosed-gap IDW).** When ``interpolate`` is on
  (default), *enclosed* empty cells -- holes surrounded by measured data
  in at least ``interp_min_dirs`` of the 8 compass directions within
  ``interp_radius`` cells -- get an inverse-distance-weighted estimate
  blended from the *differing* readings around them. These are genuine
  guesses between differing soundings: ``"kind": "interp"`` and
  ``"est": true``. Interp takes priority over radiate on any cell that
  qualifies, since a blend between differing readings is more honest
  there than picking the single nearest one.

Measured cells carry ``"kind": "measured"`` and ``"est": false``. The
deep middle of a sparsely-edged lake and any separate, far-away cluster
stay untouched by both passes.

Returns ``{cell_m, min_depth, max_depth, count, cells}`` where each cell
is ``{"lat", "lon", "depth", "n", "est", "kind"}`` at the cell centre.

<a id="vanchor.nav.depth.parse_depth_soundings"></a>

#### parse\_depth\_soundings

```python
def parse_depth_soundings(filename: str,
                          data: bytes) -> list[tuple[float, float, float]]
```

Parse an imported depth file into ``(lat, lon, depth_m)`` soundings.

Back-compat wrapper over :func:`parse_depth_features` returning just the
depth soundings. Supports CSV/XYZ and GeoJSON (see that function).

<a id="vanchor.nav.depth.parse_depth_features"></a>

#### parse\_depth\_features

```python
def parse_depth_features(filename: str, data: bytes) -> dict
```

Parse an imported depth file into ``{"soundings", "hardness"}``.

Supports the common OPEN formats: CSV/XYZ (one ``lat,lon,depth`` row each --
header auto-detected, else positional; ``.xyz`` treated as ``lon,lat,depth``)
and GeoJSON (Point/MultiPoint with a depth property or Z coordinate).
``soundings`` are ``(lat, lon, depth_m)`` positive-down; ``hardness`` are
``(lat, lon, index)`` from a ``hardness`` property on GeoJSON points
(bottom-hardness, raw 0..127); ``contours`` are ``{d, pts}`` (depth
+ ``[[lat, lon], ...]`` polyline) from LineString features. Unparseable
rows are skipped.


<a id="vanchor.nav.guard"></a>

# vanchor.nav.guard

Sensor-anomaly protection.

Cheap GPS/compass hardware throws occasional garbage: the heading sensor flips
180 deg in a sample, the GPS position jumps tens of metres on a poor fix. Feeding
those straight to the controller causes violent, wrong manoeuvres.

:class:`SensorGuard` is a small spike filter that sits in the navigator. It
rejects a single implausible reading (one that would imply impossible motion)
but **accepts it if the next reading confirms it** -- so a genuine large move
(or a hard turn) gets through after one sample, while an isolated glitch is
dropped. It also rejects out-of-range coordinates outright.

<a id="vanchor.nav.guard.SensorGuard"></a>

## SensorGuard Objects

```python
class SensorGuard()
```

<a id="vanchor.nav.guard.SensorGuard.check_position"></a>

#### check\_position

```python
def check_position(point: GeoPoint) -> bool
```

Return True if ``point`` should be accepted as the boat's position.


<a id="vanchor.nav.navigator"></a>

# vanchor.nav.navigator

The navigator turns raw NMEA sentences into updates on the shared state.

It is the only component that writes the *perceived* position/heading, keeping a
single, well-defined path from "bytes off the wire" to "what the controller
believes". It is driven both synchronously (``handle_sentence`` for tests) and
asynchronously (subscribed to the ``nmea.in`` topic at runtime).

<a id="vanchor.nav.navigator.Navigator"></a>

## Navigator Objects

```python
class Navigator()
```

<a id="vanchor.nav.navigator.Navigator.set_gps_offset"></a>

#### set\_gps\_offset

```python
def set_gps_offset(true_lat: float, true_lon: float) -> None
```

Set the offset so the boat's CURRENT fix maps to (true_lat, true_lon).

The offset is (true position − current corrected fix) and is applied to
every subsequent fix. If there is no current fix the offset is taken
relative to the raw (0,0) origin, which simply makes the true position
the new reported position.

<a id="vanchor.nav.navigator.Navigator.handle_sentence"></a>

#### handle\_sentence

```python
def handle_sentence(sentence: str) -> list[tuple[str, object]]
```

Parse one sentence and update state. Returns the (topic, payload)
events that should be published, so the async path can forward them and
tests can assert on them directly.


<a id="vanchor.nav.nmea"></a>

# vanchor.nav.nmea

A small, self-contained NMEA 0183 parser and encoder.

We deliberately do *not* depend on pynmea2 (as the old project did): a focused
implementation of just the sentences we use (RMC, GGA, HDM, HDT, APB) is more
testable, fully typed, and removes a dependency. Both parsing and encoding live
here so the simulator can emit exactly the sentences the navigator consumes.

<a id="vanchor.nav.nmea.checksum"></a>

#### checksum

```python
def checksum(body: str) -> str
```

XOR checksum of the characters between ``$`` and ``*``, as 2 hex digits.

<a id="vanchor.nav.nmea.Heading"></a>

## Heading Objects

```python
@dataclass(frozen=True)
class Heading()
```

<a id="vanchor.nav.nmea.Heading.reference"></a>

#### reference

"M" magnetic or "T" true

<a id="vanchor.nav.nmea.APB"></a>

## APB Objects

```python
@dataclass(frozen=True)
class APB()
```

<a id="vanchor.nav.nmea.APB.steer_to"></a>

#### steer\_to

"L" or "R"

<a id="vanchor.nav.nmea.Depth"></a>

## Depth Objects

```python
@dataclass(frozen=True)
class Depth()
```

<a id="vanchor.nav.nmea.Depth.depth_m"></a>

#### depth\_m

total water depth (relative to surface)

<a id="vanchor.nav.nmea.parse"></a>

#### parse

```python
def parse(sentence: str) -> Sentence | None
```

Parse a single NMEA sentence. Returns ``None`` for a well-formed sentence
type we don't model; raises :class:`NmeaError` for malformed input.


<a id="vanchor.nav.nmea_net"></a>

# vanchor.nav.nmea\_net

An NMEA-0183-over-TCP server so phone nav apps can talk to Vanchor-NG.

Apps such as Navionics, iNavX and SignalK speak NMEA over a plain TCP socket
(the de-facto "TCP/IP NMEA" gateway, conventionally on port 10110). This module
exposes a tiny :class:`asyncio` server that:

* accepts any number of concurrent clients;
* forwards every inbound line that looks like NMEA (starts with ``$`` or ``!``)
  onto the event bus as ``nmea.in``, so the navigator consumes phone-sourced
  fixes/headings exactly like serial ones;
* broadcasts outbound sentences to all connected clients, both via
  :meth:`broadcast` and automatically by subscribing to the ``nmea.out`` topic
  (the controller/simulator can publish there to feed the phone its position).

The server stays decoupled from everything else: it only knows the bus.

<a id="vanchor.nav.nmea_net.NMEA_OUT"></a>

#### NMEA\_OUT

Topic the server listens on for sentences to push to connected clients.

<a id="vanchor.nav.nmea_net.NmeaTcpServer"></a>

## NmeaTcpServer Objects

```python
class NmeaTcpServer()
```

A multi-client NMEA-0183 TCP gateway bound to the event bus.

<a id="vanchor.nav.nmea_net.NmeaTcpServer.bound_port"></a>

#### bound\_port

```python
@property
def bound_port() -> int | None
```

The actual port the server is listening on, or ``None`` if not
started. Useful when constructed with ``port=0`` (ephemeral port).

<a id="vanchor.nav.nmea_net.NmeaTcpServer.broadcast"></a>

#### broadcast

```python
async def broadcast(sentence: str) -> None
```

Send ``sentence`` (a single NMEA line) to all connected clients.

A trailing CR/LF is appended if missing. Clients that error out are
dropped silently.


<a id="vanchor.nav.routes"></a>

# vanchor.nav.routes

GPX route loading and saving.

A small, dependency-free reader/writer for GPX 1.1 route files built on the
stdlib :mod:`xml.etree.ElementTree`. We only care about waypoints, so both
free-standing ``<wpt>`` elements and ``<rtept>`` elements inside an ``<rte>``
are flattened into a single ordered list of :class:`~vanchor.core.models.Waypoint`.

Parsing is deliberately tolerant: the GPX default namespace is stripped so we
match by local tag name, individual points missing/holding bad coordinates are
skipped rather than aborting the whole load, and missing ``<name>`` elements are
defaulted to ``WP{i}``. Only XML that cannot be parsed at all raises a
:class:`ValueError`.

<a id="vanchor.nav.routes.parse_gpx"></a>

#### parse\_gpx

```python
def parse_gpx(text: str) -> list[Waypoint]
```

Parse GPX ``text`` into an ordered list of waypoints.

Reads free-standing ``<wpt>`` elements first, then ``<rtept>`` elements
inside each ``<rte>``. Tolerant of the GPX default ``xmlns``. Individual
points that lack coordinates are skipped; only XML that fails to parse at
all raises :class:`ValueError`.

<a id="vanchor.nav.routes.serialize_gpx"></a>

#### serialize\_gpx

```python
def serialize_gpx(waypoints: list[Waypoint], name: str = "route") -> str
```

Serialize ``waypoints`` to a valid GPX 1.1 document as a string.

The points are written as a single ``<rte>`` named ``name`` containing one
``<rtept>`` per waypoint. Missing names are defaulted to ``WP{i}``.


<a id="vanchor.nav.routing"></a>

# vanchor.nav.routing

Smart "Take me here" water routing.

Given a start and a destination, plan a route that stays on **water only**
(never crossing land or islands) and return it as a short list of waypoints for
the UI's route editor to load *unstarted* (the skipper reviews before pressing
Go). Two modes:

- ``fastest``  -- the shortest navigable water path. Computed as the exact
  shortest obstacle-avoiding path with a **visibility graph** over the water
  polygon (bends occur only at shore/island vertices), via shapely + networkx.
- ``shoreline`` -- head to the nearest shore (ending ``shoreline_offset_m`` m
  off it), hug that offset ring toward the destination, then cut straight in as
  soon as there is clear open-water line-of-sight.

All geometry maths happens in a metric UTM CRS (see :mod:`.water`); only the
final waypoints are converted back to lat/lon.

<a id="vanchor.nav.routing.MAX_PLAN_VERTS"></a>

#### MAX\_PLAN\_VERTS

boundary-vertex cap fed to the planners

<a id="vanchor.nav.routing.MAX_RING_PTS"></a>

#### MAX\_RING\_PTS

cap on shoreline-walk ring points

<a id="vanchor.nav.routing.MIN_CORRIDOR_M"></a>

#### MIN\_CORRIDOR\_M

route-corridor half-width bounds (excludes far water)

<a id="vanchor.nav.routing.RouteResult"></a>

## RouteResult Objects

```python
@dataclass
class RouteResult()
```

<a id="vanchor.nav.routing.RouteResult.waypoints"></a>

#### waypoints

{name, lat, lon}

<a id="vanchor.nav.routing.RoutePlanCancelled"></a>

## RoutePlanCancelled Objects

```python
class RoutePlanCancelled(Exception)
```

Raised internally when a caller cancels an in-progress plan (`54`).

<a id="vanchor.nav.routing.plan_route"></a>

#### plan\_route

```python
def plan_route(*,
               start_lat: float,
               start_lon: float,
               dest_lat: float,
               dest_lon: float,
               water_ll: MultiPolygon,
               mode: str = "fastest",
               shoreline_offset_m: float = 25.0,
               cancelled: Callable[[], bool] | None = None) -> RouteResult
```

Plan a water-only route over an already-assembled water polygon.

``water_ll`` is a lon/lat polygon (as produced by :mod:`.water`). This is
pure CPU work (shapely + networkx); run it in an executor.

``cancelled`` is an optional predicate polled periodically during the heavy
visibility-graph build; if it returns True the plan aborts and returns a
cancelled result (`54`).

<a id="vanchor.nav.routing.plan_island_loop"></a>

#### plan\_island\_loop

```python
def plan_island_loop(click_lat: float,
                     click_lon: float,
                     water_ll: MultiPolygon,
                     *,
                     boat_lat: float,
                     boat_lon: float,
                     offset_m: float = 20.0) -> RouteResult
```

Plan a closed loop track that encircles the island under the click.

The boat's basin is the water body it occupies; an **island** is one of that
basin's interior rings (land ringed by routable water). We find the island
whose polygon contains the click, buffer it outward by ``offset_m``, take
that offset ring and clip it to the navigable water (the basin minus its
other islands) so the whole loop stays on the water. If the offset ring
can't stay in water all the way around, the offset is shrunk; if even a small
offset won't fit, the request is rejected.

Pure CPU work (shapely); run it from an executor.


<a id="vanchor.nav.survey"></a>

# vanchor.nav.survey

Area survey route planning -- "map mode" boustrophedon coverage (`47`).

Given a closed area polygon (lat/lon), compute a back-and-forth *lawnmower*
coverage route that surveys the whole area with a settable spacing between
parallel passes. The classic agricultural / robotics pattern for this is the
**boustrophedon decomposition**: sweep a set of parallel lines across the area,
clip each line to the polygon, then walk them in alternating direction so the
end of one pass connects to the start of the next (a continuous "ox-plough"
path -- Greek *boustrophedon*, "as the ox turns").

All geometry maths happens in a metric UTM CRS (see :mod:`.water`); only the
final waypoints are converted back to lat/lon.

The default sweep direction is the polygon's **longest axis** (so passes run the
long way and there are fewer expensive turns), unless an explicit ``angle_deg``
is given. The number of passes is capped so a tiny spacing on a big area can't
produce thousands of waypoints; when the cap is hit a clear message is returned.

<a id="vanchor.nav.survey.SurveyResult"></a>

## SurveyResult Objects

```python
@dataclass
class SurveyResult()
```

<a id="vanchor.nav.survey.SurveyResult.waypoints"></a>

#### waypoints

{name, lat, lon}

<a id="vanchor.nav.survey.plan_survey"></a>

#### plan\_survey

```python
def plan_survey(polygon_latlon: list,
                spacing_m: float,
                angle_deg: float | None = None) -> SurveyResult
```

Plan a boustrophedon coverage route over a closed area polygon.

Parameters
----------
polygon_latlon:
    The survey area as a list of ``[lat, lon]`` vertices (a closed ring; the
    ring need not repeat its first point).
spacing_m:
    Distance between parallel passes, in metres.
angle_deg:
    Sweep direction in **compass-ish math degrees in the metric frame**; if
    omitted the polygon's longest axis is used (fewest, longest passes).

Returns ordered waypoints ``[{name, lat, lon}]`` (``WP1``.. / ``DEST``).
Pure CPU work (shapely); run it in an executor.

<a id="vanchor.nav.survey.plan_work_spots"></a>

#### plan\_work\_spots

```python
def plan_work_spots(polygon_latlon: list, spacing_m: float) -> SurveyResult
```

Even grid of work spots inside an area polygon (``[[lat, lon], ...]`` ring),
spaced ~``spacing_m`` apart and ordered serpentine (lawnmower) so the boat
works them in a tidy sweep. Returns ``SurveyResult`` with waypoints
``[{name, lat, lon}]``. Pure CPU (shapely); run in an executor. Water-clipping
is applied by the caller (``Runtime.plan_work_spots``).


<a id="vanchor.nav.track"></a>

# vanchor.nav.track

Track recording: breadcrumb the boat's path, then replay or retrace it.

This mirrors the "record-a-track" / track retrace feature of GPS trolling motors
(Minn Kota iTracks, MotorGuide routes). A :class:`TrackRecorder` samples the
boat's position into a list of :class:`Waypoint`s while recording; replaying
feeds those points straight into :class:`~vanchor.controller.modes.WaypointMode`
(forward), and the retrace command feeds them reversed.

<a id="vanchor.nav.track.TrackRecorder"></a>

## TrackRecorder Objects

```python
class TrackRecorder()
```

Records a breadcrumb track of GeoPoints, one every ``min_distance_m``.

<a id="vanchor.nav.track.TrackRecorder.start"></a>

#### start

```python
def start(seed: GeoPoint | None = None) -> None
```

Begin a fresh recording (optionally seeded with the current point).

<a id="vanchor.nav.track.TrackRecorder.maybe_record"></a>

#### maybe\_record

```python
def maybe_record(point: GeoPoint | None) -> None
```

Append ``point`` if recording and it is far enough from the last one.


<a id="vanchor.nav.trip"></a>

# vanchor.nav.trip

Trip log: record each outing's track, distance, duration and speed stats.

A *trip* is one continuous outing -- from the moment the boat starts making way
until it goes idle (or the helmsman stops it). While a trip is active the
:class:`TripLog` samples the boat's position into a breadcrumb track (min-distance
filtered like :class:`~vanchor.nav.track.TrackRecorder`), integrates the distance
travelled as the sum of segment lengths, and tracks the max speed-over-ground.

The log can **auto-start** a trip when the boat first makes way (SOG over a small
threshold) and **auto-stop** it after a stretch of idleness; a manual start/stop
always works and overrides the automatic behaviour. Finished trips are persisted
to ``<data_dir>/trips/<id>.json`` and can be listed, fetched, exported as GPX or
deleted.

All time comes in through ``now`` arguments (the Runtime feeds its injectable
``_now_fn``), so the auto-start/stop logic is fully deterministic in tests.

<a id="vanchor.nav.trip.Trip"></a>

## Trip Objects

```python
@dataclass
class Trip()
```

One outing's recorded track and summary statistics.

<a id="vanchor.nav.trip.Trip.summary"></a>

#### summary

```python
def summary(now: float) -> dict
```

Summary fields only (no point array) for the list endpoint.

<a id="vanchor.nav.trip.Trip.to_dict"></a>

#### to\_dict

```python
def to_dict(now: float) -> dict
```

Full record including the track points.

<a id="vanchor.nav.trip.TripLog"></a>

## TripLog Objects

```python
class TripLog()
```

Records the current outing and persists finished trips to disk.

Call :meth:`update` on every telemetry tick with the boat's current position
and speed-over-ground (knots) plus the current time; it handles breadcrumb
sampling, distance/max-speed accumulation and the auto-start/stop state
machine. :meth:`start`/:meth:`stop` give the helmsman manual control.

<a id="vanchor.nav.trip.TripLog.start"></a>

#### start

```python
def start(name: str | None, now: float, *, auto: bool = False) -> Trip
```

Begin a fresh trip, finalizing any trip already in progress.

<a id="vanchor.nav.trip.TripLog.stop"></a>

#### stop

```python
def stop(now: float) -> Trip | None
```

Finalize the active trip and persist it. Returns the saved trip.

<a id="vanchor.nav.trip.TripLog.update"></a>

#### update

```python
def update(position: GeoPoint | None, sog_kn: float, now: float) -> None
```

Advance the trip log one tick.

Records a breadcrumb + integrates distance/max-speed for the active trip,
and runs the auto-start/stop machine when ``auto`` is enabled.

<a id="vanchor.nav.trip.TripLog.snapshot"></a>

#### snapshot

```python
def snapshot(now: float) -> dict
```

The CURRENT trip's live stats for telemetry (zeros when idle).

<a id="vanchor.nav.trip.TripLog.list_trips"></a>

#### list\_trips

```python
def list_trips() -> list[dict]
```

Summaries of all saved trips, newest first.

<a id="vanchor.nav.trip.TripLog.get_trip"></a>

#### get\_trip

```python
def get_trip(trip_id: str) -> dict | None
```

Full saved trip (including points), or None if absent.

<a id="vanchor.nav.trip.TripLog.delete_trip"></a>

#### delete\_trip

```python
def delete_trip(trip_id: str) -> bool
```

Remove a saved trip. Returns True if it existed.

<a id="vanchor.nav.trip.TripLog.gpx"></a>

#### gpx

```python
def gpx(trip_id: str) -> str | None
```

Export a saved trip as a GPX ``<trk>``, or None if absent.

<a id="vanchor.nav.trip.trip_to_gpx"></a>

#### trip\_to\_gpx

```python
def trip_to_gpx(trip: dict) -> str
```

Render a trip dict (as persisted) into a GPX 1.1 document string.


<a id="vanchor.nav.water"></a>

# vanchor.nav.water

Water geometry: fetch, assemble, project and cache navigable-water polygons.

The smart router needs a polygon of *navigable water* (lake/sea minus islands)
to plan a route that never crosses land. The authoritative free source is
OpenStreetMap, queried through the Overpass API.

Two non-obvious things this module gets right (both verified on the sim area):

1. **Relation assembly.** Many lakes (including the sim's lake *Visten*, OSM
   relation 287548) are stored as ``natural=water`` *multipolygon relations*,
   not as single closed ways. A naive "closed ways only" extractor finds zero
   ways containing the boat and wrongly reports it as *not in water*. We stitch
   each relation's ``outer`` member ways into rings with
   :func:`shapely.ops.polygonize`, and subtract the ``inner`` rings (islands).

2. **Metric projection.** All routing maths (buffering, distances, simplify)
   happens in a metre-based UTM CRS, never in degrees.

A successfully assembled polygon is cached as WKB under
``<data_dir>/water_cache/`` so the boat can plan routes offline after a single
online fetch ("fetch at the dock, run on the water").

<a id="vanchor.nav.water.overpass_endpoints"></a>

#### overpass\_endpoints

```python
def overpass_endpoints() -> tuple[str, ...]
```

The Overpass endpoints to try, in order.

Reads ``VANCHOR_OVERPASS_URLS`` (comma-separated) at call time, falling back
to the built-in :data:`OVERPASS_ENDPOINTS` when it is unset/empty.

<a id="vanchor.nav.water.user_agent"></a>

#### user\_agent

```python
def user_agent() -> str
```

The HTTP User-Agent for Overpass requests.

Reads ``VANCHOR_USER_AGENT`` at call time, falling back to the built-in
:data:`USER_AGENT`.

<a id="vanchor.nav.water.utm_epsg_for"></a>

#### utm\_epsg\_for

```python
def utm_epsg_for(lon: float, lat: float) -> int
```

EPSG code of the UTM zone containing ``(lon, lat)``.

<a id="vanchor.nav.water.Projection"></a>

## Projection Objects

```python
@dataclass
class Projection()
```

A reusable lat/lon <-> metric transform pair around an area of interest.

<a id="vanchor.nav.water.overpass_query"></a>

#### overpass\_query

```python
def overpass_query(south: float, west: float, north: float,
                   east: float) -> str
```

Overpass QL fetching water ways + relations (and coastline) in a bbox.

<a id="vanchor.nav.water.assemble_water"></a>

#### assemble\_water

```python
def assemble_water(elements: list[dict]) -> MultiPolygon
```

Assemble a navigable-water polygon from raw Overpass elements.

Handles closed ways directly, stitches multipolygon relations from their
``outer`` member ways (the critical step -- see module docstring), and
subtracts island (``inner`` / standalone) rings as holes.

<a id="vanchor.nav.water.fetch_overpass"></a>

#### fetch\_overpass

```python
def fetch_overpass(south: float,
                   west: float,
                   north: float,
                   east: float,
                   *,
                   timeout: float = 60.0) -> list[dict]
```

Fetch raw water elements from Overpass (tries endpoints in order).

<a id="vanchor.nav.water.bbox_around"></a>

#### bbox\_around

```python
def bbox_around(a_lat: float,
                a_lon: float,
                b_lat: float,
                b_lon: float,
                *,
                pad_m: float = 2000.0) -> tuple[float, float, float, float]
```

A padded (south, west, north, east) bbox covering both points.

Padding grows with the point separation (so a long route has room to go
around obstacles), capped so we never request an enormous area.

<a id="vanchor.nav.water.WaterCache"></a>

## WaterCache Objects

```python
class WaterCache()
```

Persists assembled water polygons (lon/lat WGS84) as WKB on disk.

A cache entry covers a bbox; a lookup succeeds when a cached polygon's bbox
covers the requested bbox, so a single dock-side fetch serves many routes.

<a id="vanchor.nav.water.WaterCache.find_covering"></a>

#### find\_covering

```python
def find_covering(
        bbox: tuple[float, float, float, float]) -> BaseGeometry | None
```

Return a cached polygon whose bbox covers ``bbox``, else None.

<a id="vanchor.nav.water.load_geojson"></a>

#### load\_geojson

```python
def load_geojson(path: str | Path) -> MultiPolygon
```

Load a water polygon saved as GeoJSON (used by tests / fixtures).

