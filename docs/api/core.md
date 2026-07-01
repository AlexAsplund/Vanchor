# `vanchor.core`

<a id="vanchor.core"></a>

# vanchor.core

Core domain types and shared services.

The vocabulary every other layer speaks, with no dependency on the sim, hardware
or UI layers: the immutable-ish data models (``GeoPoint``, ``BoatState``,
``MotorCommand``, setpoints), the mutable ``NavigationState``, the async event
bus, application config + persisted boat profiles, geodesy helpers, the reusable
PID controller, and observability (the in-memory log ring behind "View logs" and
the telemetry recorder).


<a id="vanchor.core.backup"></a>

# vanchor.core.backup

Versioned backup / restore of all persistent Vanchor state.

A *backup* is a single in-memory ZIP archive bundling everything worth keeping
from the runtime's ``data_dir`` plus the small slice of client state the web UI
keeps in ``localStorage``. It is self-describing: a ``manifest.json`` at the
root records the format, schema version, app version, creation time and the list
of contained entries, so a restore can validate it and (in the future) migrate
older layouts forward.

Archive layout
--------------
::

    manifest.json        # see below
    client.json          # the UI's localStorage dict (keys prefixed "vanchor-")
    boats.json           # boat profiles            (if present in data_dir)
    depthmap.json        # accumulated depth soundings (if present)
    devices.json         # persisted device/hardware config (if present)
    trips/<id>.json      # per-outing trip logs     (every file under trips/)

Deliberately EXCLUDED: ``water_cache/`` and ``debug/`` -- both are large and
fully regenerable, so they would only bloat the archive.

Manifest shape
--------------
::

    {
        "format": "vanchor-backup",   # constant magic; restore rejects anything else
        "schema_version": 1,           # == SCHEMA_VERSION at creation time
        "app_version": "0.1.0",       # the package version that wrote it
        "created_at": "2026-06-26T12:00:00Z",  # ISO8601, PASSED IN (never datetime.now())
        "contents": ["boats.json", "depthmap.json", "trips/trip-...json", ...]
    }

Versioning + migration
----------------------
``SCHEMA_VERSION`` is the on-disk layout version. Bump it whenever the set of
files, their names, or their internal shape changes in a way a plain extract
can't handle. For each bump add a migration step keyed on the *source* version
inside :func:`_migrate` -- that function is the single, explicit extension point
for "convert old backups": it receives the parsed manifest (and the open zip)
and returns a possibly-rewritten manifest before extraction. Today it is a
no-op pass-through; future versions chain ``v1 -> v2 -> ...`` steps there.

A backup whose ``schema_version`` is NEWER than this build's ``SCHEMA_VERSION``
is still restored best-effort (unknown files are simply ignored) with a warning,
so a downgrade never hard-fails.

<a id="vanchor.core.backup.create_backup"></a>

#### create\_backup

```python
def create_backup(data_dir: str,
                  client: dict | None = None,
                  app_version: str | None = None,
                  *,
                  created_at: str = "1970-01-01T00:00:00Z") -> bytes
```

Build a versioned backup ZIP of ``data_dir`` (+ ``client`` state) in memory.

``client`` is the UI's ``localStorage`` slice (keys prefixed ``vanchor-``);
``None`` is stored as an empty object. ``created_at`` is an ISO8601 string
the *caller* supplies (the endpoint passes the request time) -- this module
never calls ``datetime.now()`` so backups are reproducible/testable.
``app_version`` defaults to the installed package version.

Returns the raw ``.zip`` bytes.

<a id="vanchor.core.backup.restore_backup"></a>

#### restore\_backup

```python
def restore_backup(data_dir: str, zip_bytes: bytes) -> dict
```

Restore a backup ZIP into ``data_dir`` (overwriting existing files).

Validates the manifest (rejecting anything whose ``format`` is not
:data:`FORMAT`). A backup from a NEWER schema is restored best-effort with a
warning; an OLDER one is run through :func:`_migrate` first. Known data files
(``boats.json``, ``depthmap.json``, ``devices.json``, and everything under
``trips/``) are extracted; the ``trips/`` dir is created as needed. Entries
with absolute or ``..`` paths (zip-slip) are ignored.

Returns ``{ok, schema_version, app_version, created_at, restored, client,
warnings}``. Raises :class:`ValueError` (mapped to HTTP 400 by the endpoint)
on a corrupt zip or a missing/invalid manifest.


<a id="vanchor.core.boat_profiles"></a>

# vanchor.core.boat\_profiles

Named boat profiles: persisted, selectable sets of :class:`BoatConfig` specs.

A *profile* is one named bundle of the editable boat specs (the same fields the
Init-boat wizard edits -- length, mass, thrust, steering geometry, sonar cone,
etc.). The :class:`BoatProfileStore` keeps several of them on disk so a helmsman
can switch between, say, a light kayak and a heavier skiff, and have the live
physics + telemetry follow the selection.

The store is a thin, deterministic, file-backed thing: it owns the JSON at
``<data_dir>/boats.json`` of the shape::

    {
        "active_id": "<id>",
        "profiles": {
            "<id>": {"name": "<str>", "specs": { ...BoatConfig fields... }},
            ...
        }
    }

On first run (no file) it seeds a small set of ready-to-pick presets (`89`) --
bow/stern trolling motors, an off-centre bow motor, and a 15 HP stern outboard
-- with the bow trolling motor marked active. If handed an explicit
:class:`BoatConfig` ``seed`` instead, it falls back to a single ``"default"``
profile built from it. Either way it never clobbers an existing ``boats.json``.

IDs are derived deterministically from the profile name (a slug) plus an
incrementing counter on collision -- never from the wall clock -- so tests are
fully reproducible. The store knows nothing about the live runtime; applying a
profile to the simulator is the Runtime's job.

<a id="vanchor.core.boat_profiles.specs_from_boat"></a>

#### specs\_from\_boat

```python
def specs_from_boat(boat: BoatConfig) -> dict[str, Any]
```

Snapshot a :class:`BoatConfig` into a plain specs dict.

<a id="vanchor.core.boat_profiles.BoatProfileStore"></a>

## BoatProfileStore Objects

```python
class BoatProfileStore()
```

Persistent set of named boat profiles with an active selection.

<a id="vanchor.core.boat_profiles.BoatProfileStore.list"></a>

#### list

```python
def list() -> list[dict[str, Any]]
```

All profiles as ``[{id, name, ...specs}, ...]`` (insertion order).

<a id="vanchor.core.boat_profiles.BoatProfileStore.get"></a>

#### get

```python
def get(profile_id: str) -> dict[str, Any] | None
```

One profile as ``{id, name, specs:{...}}`` (None if unknown).

<a id="vanchor.core.boat_profiles.BoatProfileStore.create"></a>

#### create

```python
def create(name: str, specs: dict[str, Any] | None = None) -> str
```

Create a new profile; returns its id. Specs default to BoatConfig
defaults for any field not provided.

<a id="vanchor.core.boat_profiles.BoatProfileStore.save"></a>

#### save

```python
def save(profile_id: str, name: str | None,
         specs: dict[str, Any] | None) -> bool
```

Update an existing profile's name and/or specs. Returns False if the
id is unknown. Specs are merged onto the existing ones (partial update).

<a id="vanchor.core.boat_profiles.BoatProfileStore.delete"></a>

#### delete

```python
def delete(profile_id: str) -> bool
```

Delete a profile. Refuses to delete the last remaining one (returns
False). If the active profile is deleted, the active selection falls
back to the first remaining profile.

<a id="vanchor.core.boat_profiles.BoatProfileStore.set_active"></a>

#### set\_active

```python
def set_active(profile_id: str) -> bool
```

Mark a profile active. Returns False if the id is unknown.

<a id="vanchor.core.boat_profiles.BoatProfileStore.to_dict"></a>

#### to\_dict

```python
def to_dict() -> dict[str, Any]
```

REST shape: ``{active_id, profiles:[{id,name,...specs}, ...]}``.


<a id="vanchor.core.config"></a>

# vanchor.core.config

Typed, nested, file-backed application configuration.

The whole controller is configured from a single :class:`AppConfig` tree of
small dataclasses. Each sub-config maps onto the constructors of an existing
component (the simulator, sensors, controller, control modes, helm and the
environment) so the integrator can wire things up by reading fields straight
off the config rather than threading loose keyword arguments around.

Configs can be loaded from a YAML (``.yaml``/``.yml``) or JSON (``.json``)
file. Unknown keys are ignored and any missing key falls back to its default,
so partial config files are always valid. With no path (or a missing file) the
built-in defaults are returned unchanged.

<a id="vanchor.core.config.SimConfig"></a>

## SimConfig Objects

```python
@dataclass
class SimConfig()
```

Simulator world + physics settings.

Maps onto ``Simulator(physics_hz, time_scale)`` and seeds the boat's
starting position.

<a id="vanchor.core.config.SimConfig.start_lat"></a>

#### start\_lat

59°39'45.9"N (Lake Vänern, Karlstad)

<a id="vanchor.core.config.SimConfig.start_lon"></a>

#### start\_lon

13°19'20.9"E

<a id="vanchor.core.config.SimConfig.model"></a>

#### model

"fossen" (3-DOF, bow-mount aware) or "simple"

<a id="vanchor.core.config.BoatConfig"></a>

## BoatConfig Objects

```python
@dataclass
class BoatConfig()
```

Physical boat + trolling-motor geometry.

Feeds the ``fossen`` 3-DOF physics model (which is bow-mount aware) and the
``simple`` model's speed/turn limits. ``thruster_mount`` captures *where* the
steerable trolling motor sits: a bow mount pulls the bow around, a stern
mount pushes it -- the sign of the resulting yaw is opposite, which the
model accounts for via the longitudinal offset from the centre of gravity.

<a id="vanchor.core.config.BoatConfig.max_thrust_n"></a>

#### max\_thrust\_n

~55 lbf trolling motor

<a id="vanchor.core.config.BoatConfig.reverse_efficiency"></a>

#### reverse\_efficiency

reverse prop thrust as a fraction of forward

<a id="vanchor.core.config.BoatConfig.thruster_mount"></a>

#### thruster\_mount

"bow" | "stern" | "center"

<a id="vanchor.core.config.BoatConfig.thruster_offset_m"></a>

#### thruster\_offset\_m

explicit CG->thruster (+fwd); overrides mount

<a id="vanchor.core.config.BoatConfig.thruster_y_m"></a>

#### thruster\_y\_m

lateral CG->thruster offset (+ = starboard)

<a id="vanchor.core.config.BoatConfig.thrust_yaw_ff_trim"></a>

#### thrust\_yaw\_ff\_trim

calibration refinement (radians) on the FF angle

<a id="vanchor.core.config.BoatConfig.max_steer_angle_deg"></a>

#### max\_steer\_angle\_deg

full mechanical swing (manual reaches this)

<a id="vanchor.core.config.BoatConfig.autopilot_steer_deg"></a>

#### autopilot\_steer\_deg

authority the autopilot actually uses

<a id="vanchor.core.config.BoatConfig.max_steer_rate_dps"></a>

#### max\_steer\_rate\_dps

how fast the steering head can rotate (deg/s)

<a id="vanchor.core.config.BoatConfig.max_turn_rate_deg"></a>

#### max\_turn\_rate\_deg

used by the kinematic "simple" model

<a id="vanchor.core.config.BoatConfig.steer_range_deg"></a>

#### steer\_range\_deg

+/- cable-wrap mechanical limit of the head

<a id="vanchor.core.config.BoatConfig.steer_reduction"></a>

#### steer\_reduction

pinion->ring reduction

<a id="vanchor.core.config.BoatConfig.thruster_x_m"></a>

#### thruster\_x\_m

```python
def thruster_x_m() -> float
```

Signed longitudinal distance from CG to the thruster (+ = forward).

The mount fixes the thruster's position relative to the hull's geometric
centre (bow ~+0.42L, stern ~-0.42L); the CG sits ``cg_aft_frac`` of a
length BEHIND that centre, so the CG->thruster arm is the gap between
them. An explicit ``thruster_offset_m`` already encodes the CG distance
and is used verbatim.

<a id="vanchor.core.config.BoatConfig.thrust_yaw_ff_angle"></a>

#### thrust\_yaw\_ff\_angle

```python
def thrust_yaw_ff_angle() -> float
```

Feed-forward steering deflection (radians) that cancels the straight-
thrust yaw of a laterally-offset thruster.

A thruster at ``(x, y)`` making forward thrust ``F`` and steering-induced
lateral force produces yaw ``N = x*F_lat - y*F_fwd``. Deflecting the motor
by ``delta`` gives ``F_fwd = F*cos(delta)``, ``F_lat = F*sin(delta)`` so
``N = F*(x*sin(delta) - y*cos(delta))`` which is zero when
``x*sin(delta) = y*cos(delta)`` => ``delta = atan2(y, |x|)`` -- independent
of thrust magnitude. ``thrust_yaw_ff`` overrides this geometric value; a
measured ``thrust_yaw_ff_trim`` is then added on top.

Note the lever arm uses ``|x|``: a stern mount (x < 0) needs the same
*physical* deflection sign as a bow mount to oppose the same lateral
offset; the bow/stern steering-authority flip is handled separately by the
helm's ``steer_sign``.

<a id="vanchor.core.config.EnvironmentConfig"></a>

## EnvironmentConfig Objects

```python
@dataclass
class EnvironmentConfig()
```

Wind and current. Maps onto
``Environment(current_speed, current_dir, wind_speed, wind_dir)``.

<a id="vanchor.core.config.EnvironmentConfig.gust_amplitude_mps"></a>

#### gust\_amplitude\_mps

gust std on top of the base wind (0 = steady)

<a id="vanchor.core.config.SensorConfig"></a>

## SensorConfig Objects

```python
@dataclass
class SensorConfig()
```

Simulated sensor rates and noise.

``gps_hz`` maps onto ``SimGps(update_hz)`` and ``compass_hz`` onto
``SimCompass(update_hz)``.

<a id="vanchor.core.config.SensorConfig.gps_hz"></a>

#### gps\_hz

a reconfigured u-blox runs at 5 Hz; far tighter station-keeping

<a id="vanchor.core.config.ControlConfig"></a>

## ControlConfig Objects

```python
@dataclass
class ControlConfig()
```

Controller loop rate and the gains for every guided behaviour.

``tick_hz`` maps onto ``Controller(tick_hz)``. The ``heading_*`` gains map
onto the helm ``PID(kp, ki, kd)``. The ``anchor_*`` gains and
``anchor_radius_m`` map onto ``modes.AnchorConfig(kp, ki, kd, ...)``. The
``waypoint_*`` fields map onto
``modes.WaypointConfig(arrival_radius_m, throttle, xte_gain)``.

<a id="vanchor.core.config.ControlConfig.heading_kp"></a>

#### heading\_kp

auto-tuned compromise (faster settle, anchor-safe)

<a id="vanchor.core.config.ControlConfig.steer_tau"></a>

#### steer\_tau

low-pass (s) on steering so the head isn't driven by noise

<a id="vanchor.core.config.ControlConfig.anchor_kp"></a>

#### anchor\_kp

thrust per metre of position error

<a id="vanchor.core.config.ControlConfig.anchor_kd"></a>

#### anchor\_kd

braking thrust per (m/s) of closing speed (reverse)

<a id="vanchor.core.config.ControlConfig.anchor_idle_deadband_m"></a>

#### anchor\_idle\_deadband\_m

idle within this band of the mark (no hunting)

<a id="vanchor.core.config.ControlConfig.jog_increment_m"></a>

#### jog\_increment\_m

Spot-Lock Jog step (~5 ft)

<a id="vanchor.core.config.ControlConfig.cruise_kp"></a>

#### cruise\_kp

Cruise Control (constant SOG) PID (auto-tuned)

<a id="vanchor.core.config.ControlConfig.track_min_distance_m"></a>

#### track\_min\_distance\_m

record a breadcrumb every N metres

<a id="vanchor.core.config.ControlConfig.auto_trip"></a>

#### auto\_trip

auto-start a trip when the boat makes way

<a id="vanchor.core.config.ControlConfig.trip_min_distance_m"></a>

#### trip\_min\_distance\_m

breadcrumb spacing for the trip track

<a id="vanchor.core.config.ControlConfig.trip_start_speed_kn"></a>

#### trip\_start\_speed\_kn

SOG over this auto-starts a trip

<a id="vanchor.core.config.ControlConfig.trip_idle_timeout_s"></a>

#### trip\_idle\_timeout\_s

idle this long below the threshold -> auto-stop

<a id="vanchor.core.config.ControlConfig.drift_kp"></a>

#### drift\_kp

Drift mode (controlled drift speed) PID

<a id="vanchor.core.config.SafetyConfig"></a>

## SafetyConfig Objects

```python
@dataclass
class SafetyConfig()
```

Limits and watchdogs that protect the boat and the motor.

<a id="vanchor.core.config.SafetyConfig.min_depth_m"></a>

#### min\_depth\_m

cut thrust below this sounded depth (0 = disabled)

<a id="vanchor.core.config.SafetyConfig.nogo_lookahead_m"></a>

#### nogo\_lookahead\_m

also stop within this distance of a no-go zone

<a id="vanchor.core.config.SafetyConfig.rtl_margin_m"></a>

#### rtl\_margin\_m

warn when range-home gets within this of battery range

<a id="vanchor.core.config.SafetyConfig.auto_rtl"></a>

#### auto\_rtl

if true, auto-engage RTL (don't just recommend)

<a id="vanchor.core.config.BatteryConfig"></a>

## BatteryConfig Objects

```python
@dataclass
class BatteryConfig()
```

Simulated/monitored battery pack (`60`).

Maps onto ``sim.battery.BatteryConfig``. On real hardware the live
SOC/voltage/current come from a battery monitor over the HAL; these fields
still size the pack for the range/time-to-empty estimates.

<a id="vanchor.core.config.BatteryConfig.capacity_ah"></a>

#### capacity\_ah

pack capacity (amp-hours)

<a id="vanchor.core.config.BatteryConfig.nominal_v"></a>

#### nominal\_v

nominal terminal voltage

<a id="vanchor.core.config.BatteryConfig.reserve_pct"></a>

#### reserve\_pct

usable-charge reserve (%) kept in hand

<a id="vanchor.core.config.ServerConfig"></a>

## ServerConfig Objects

```python
@dataclass
class ServerConfig()
```

Web UI / HTTP server bind address.

<a id="vanchor.core.config.HardwareConfig"></a>

## HardwareConfig Objects

```python
@dataclass
class HardwareConfig()
```

Real serial hardware. ``enabled`` is the master switch (False = full
simulation, the default; True = all real serial devices).

Per-device ``*_source`` overrides let you **mix** simulated and real devices
— e.g. drive a real steering servo while the boat itself is simulated, to
bench-test the servo against a realistic autopilot. Each is ``"sim"`` or
``"serial"`` (the motor also accepts ``"both"`` = drive the sim boat AND
mirror commands to the real servo). ``None`` follows ``enabled``.

<a id="vanchor.core.config.HardwareConfig.gps_source"></a>

#### gps\_source

"sim" | "serial" | "nmea"

<a id="vanchor.core.config.HardwareConfig.compass_source"></a>

#### compass\_source

"sim" | "serial" | "nmea"

<a id="vanchor.core.config.HardwareConfig.depth_source"></a>

#### depth\_source

"sim" | "nmea" (no serial depth yet)

<a id="vanchor.core.config.HardwareConfig.motor_source"></a>

#### motor\_source

"sim" | "serial" | "both"

<a id="vanchor.core.config.HardwareConfig.source"></a>

#### source

```python
def source(device: str) -> str
```

Resolve the source for ``device`` ("gps"/"compass"/"depth"/"motor"),
honouring its override else falling back to ``enabled``.

<a id="vanchor.core.config.NmeaTcpConfig"></a>

## NmeaTcpConfig Objects

```python
@dataclass
class NmeaTcpConfig()
```

Optional NMEA-over-TCP server (e.g. for OpenCPN).

<a id="vanchor.core.config.AppConfig"></a>

## AppConfig Objects

```python
@dataclass
class AppConfig()
```

The root configuration tree.

<a id="vanchor.core.config.AppConfig.data_dir"></a>

#### data\_dir

persisted depth map + debug recordings

<a id="vanchor.core.config.AppConfig.from_dict"></a>

#### from\_dict

```python
@classmethod
def from_dict(cls, data: dict[str, Any] | None) -> "AppConfig"
```

Build an :class:`AppConfig` from a (possibly partial) mapping.

Performs a deep, defensive merge: unknown keys are ignored and any key
not present keeps its default. ``None`` is treated as an empty mapping.

<a id="vanchor.core.config.AppConfig.to_dict"></a>

#### to\_dict

```python
def to_dict() -> dict[str, Any]
```

Serialize the full tree to plain nested dicts.

<a id="vanchor.core.config.load_device_overrides"></a>

#### load\_device\_overrides

```python
def load_device_overrides(data_dir: str | Path) -> dict[str, Any] | None
```

Read ``<data_dir>/devices.json`` if present, else ``None``.

Returns the parsed ``{"hardware": {...}, "nmea_tcp": {...}}`` mapping. A
missing file (the common case) or a corrupt/non-mapping file returns
``None`` so startup falls back to the base config untouched.

<a id="vanchor.core.config.apply_device_overrides"></a>

#### apply\_device\_overrides

```python
def apply_device_overrides(config: AppConfig,
                           data_dir: str | Path | None = None) -> AppConfig
```

Override ``config.hardware`` + ``config.nmea_tcp`` from a persisted
``devices.json`` (if one exists under ``data_dir``), then return ``config``.

``data_dir`` defaults to ``config.data_dir``. A field-level merge: any saved
key overrides the loaded base, missing/extra keys are tolerated. Call this
after :func:`load` and before building the runtime so a saved device config
survives restarts.

<a id="vanchor.core.config.save_device_overrides"></a>

#### save\_device\_overrides

```python
def save_device_overrides(data_dir: str | Path, hardware: HardwareConfig,
                          nmea_tcp: NmeaTcpConfig) -> dict[str, Any]
```

Persist ``hardware`` + ``nmea_tcp`` to ``<data_dir>/devices.json``.

Returns the written ``{"hardware": {...}, "nmea_tcp": {...}}`` mapping. The
directory is created if needed.

<a id="vanchor.core.config.load_dotenv"></a>

#### load\_dotenv

```python
def load_dotenv(path: str | Path | None = None) -> dict[str, str]
```

Read a ``.env`` file and set any of its keys not already in ``os.environ``.

No external dependency. Parses simple ``KEY=VALUE`` lines, ignoring blank
lines and ``#`` comments, stripping surrounding single/double quotes and a
leading ``export ``. A real environment variable always wins (existing keys
are left untouched). The path defaults to ``$VANCHOR_ENV_FILE`` else
``./.env``; a missing file is a quiet no-op. Returns the parsed mapping.

<a id="vanchor.core.config.apply_env_overrides"></a>

#### apply\_env\_overrides

```python
def apply_env_overrides(config: AppConfig) -> AppConfig
```

Override config fields from ``VANCHOR_*`` env vars (in place) and return it.

Only set variables are applied (unset ones leave the loaded/default value
untouched); each is coerced to the field's type. Booleans use
:func:`_parse_bool` ("1/true/yes/on").

<a id="vanchor.core.config.load"></a>

#### load

```python
def load(path: str | Path | None) -> AppConfig
```

Load an :class:`AppConfig` from a ``.yaml``/``.yml``/``.json`` file.

Returns the built-in defaults when ``path`` is None or the file does not
exist. Raises ``ValueError`` for an unsupported extension and propagates
parse errors from the underlying loader.

A ``.env`` file is read first (see :func:`load_dotenv`) and the resulting
``VANCHOR_*`` environment variables are applied *after* the file/defaults so
the environment always wins over the YAML/JSON and the built-in defaults.


<a id="vanchor.core.debug_recorder"></a>

# vanchor.core.debug\_recorder

Debug session recorder + replay.

Records EVERYTHING that happens during a session -- telemetry snapshots, raw
NMEA, commands, and log lines -- to a gzipped NDJSON file on the server, so a
real-boat session can be downloaded and replayed to see exactly what happened.

Each NDJSON line is ``{"t": <unix seconds>, "kind": "telemetry|nmea|command|log",
"data": ...}``. Replay feeds the recorded telemetry frames back through the live
telemetry channel at their original cadence, so the existing UI just plays it.

<a id="vanchor.core.debug_recorder.DebugRecorder"></a>

## DebugRecorder Objects

```python
class DebugRecorder()
```

<a id="vanchor.core.debug_recorder.DebugRecorder.path_for"></a>

#### path\_for

```python
def path_for(file_name: str) -> str | None
```

Resolve a download path, guarding against traversal.

<a id="vanchor.core.debug_recorder.ReplayPlayer"></a>

## ReplayPlayer Objects

```python
class ReplayPlayer()
```

Plays back recorded telemetry frames at their original cadence.


<a id="vanchor.core.events"></a>

# vanchor.core.events

A tiny async publish/subscribe event bus.

The whole system is wired through this bus so components stay decoupled: a
sensor publishes ``nmea.in`` without knowing the navigator exists; the UI
publishes ``command.set_mode`` without knowing the controller exists.

Handlers may be plain functions or coroutine functions. Exceptions in one
handler are logged and never break delivery to the others.

<a id="vanchor.core.events.NMEA_IN"></a>

#### NMEA\_IN

a raw NMEA sentence arrived from a sensor

<a id="vanchor.core.events.IMU_IN"></a>

#### IMU\_IN

a raw ImuSample (accel+gyro) arrived from an AHRS device

<a id="vanchor.core.events.NAV_FIX"></a>

#### NAV\_FIX

navigator produced a fresh GpsFix

<a id="vanchor.core.events.NAV_HEADING"></a>

#### NAV\_HEADING

navigator produced a fresh heading

<a id="vanchor.core.events.NAV_APB"></a>

#### NAV\_APB

navigator parsed an APB autopilot sentence

<a id="vanchor.core.events.MOTOR_COMMAND"></a>

#### MOTOR\_COMMAND

controller emitted a MotorCommand

<a id="vanchor.core.events.TELEMETRY"></a>

#### TELEMETRY

periodic snapshot for the UI

<a id="vanchor.core.events.EventBus"></a>

## EventBus Objects

```python
class EventBus()
```

<a id="vanchor.core.events.EventBus.subscribe_all"></a>

#### subscribe\_all

```python
def subscribe_all(
        handler: Callable[[str, Any], Awaitable[None] | None]) -> None
```

Receive ``(topic, payload)`` for every published event (for logging
and observability).

<a id="vanchor.core.events.run_soon"></a>

#### run\_soon

```python
def run_soon(coro: Awaitable[None]) -> "asyncio.Task[None]"
```

Schedule a coroutine on the running loop, keeping a reference so it is
not garbage-collected mid-flight.


<a id="vanchor.core.geo"></a>

# vanchor.core.geo

Pure geodesy helpers.

All functions are side-effect free and fully unit-testable. Distances are in
metres, bearings in degrees clockwise from true north. We use a spherical-Earth
model which is accurate to well within a metre over the short distances
(tens to hundreds of metres) relevant to anchoring and close-quarters steering.

<a id="vanchor.core.geo.normalize_deg"></a>

#### normalize\_deg

```python
def normalize_deg(angle: float) -> float
```

Wrap an angle into the range [0, 360).

<a id="vanchor.core.geo.angle_difference"></a>

#### angle\_difference

```python
def angle_difference(from_deg: float, to_deg: float) -> float
```

Shortest signed difference ``to - from`` in the range (-180, 180].

Positive means ``to`` is clockwise (to starboard) of ``from``.

<a id="vanchor.core.geo.haversine_m"></a>

#### haversine\_m

```python
def haversine_m(a: GeoPoint, b: GeoPoint) -> float
```

Great-circle distance between two points, in metres.

<a id="vanchor.core.geo.initial_bearing"></a>

#### initial\_bearing

```python
def initial_bearing(a: GeoPoint, b: GeoPoint) -> float
```

Initial great-circle bearing from ``a`` to ``b``, degrees [0, 360).

<a id="vanchor.core.geo.destination_point"></a>

#### destination\_point

```python
def destination_point(start: GeoPoint, distance_m: float,
                      bearing_deg: float) -> GeoPoint
```

Point reached by travelling ``distance_m`` from ``start`` on ``bearing``.

<a id="vanchor.core.geo.cross_track"></a>

#### cross\_track

```python
def cross_track(start: GeoPoint, end: GeoPoint,
                point: GeoPoint) -> CrossTrackError
```

Signed cross-track distance of ``point`` from the ``start``->``end`` leg.

Positive distance => the boat is to the right (starboard) of the track and
must steer left ("L") to return; negative => steer right ("R").

<a id="vanchor.core.geo.offset_meters"></a>

#### offset\_meters

```python
def offset_meters(point: GeoPoint, east_m: float, north_m: float) -> GeoPoint
```

Shift ``point`` by a local east/north offset in metres (equirectangular).

Accurate for the small per-tick displacements used by the simulator.


<a id="vanchor.core.models"></a>

# vanchor.core.models

Typed data models shared across the whole system.

These are deliberately small, immutable-ish dataclasses. They are the common
vocabulary spoken by sensors, the navigator, control modes, the helm and the
motor controller, so that real and simulated devices are interchangeable.

<a id="vanchor.core.models.ControlModeName"></a>

## ControlModeName Objects

```python
class ControlModeName(str, Enum)
```

The high level steering behaviours the controller can be in.

<a id="vanchor.core.models.ControlModeName.ANCHOR_ML"></a>

#### ANCHOR\_ML

learned (tiny-NN) station-keeper

<a id="vanchor.core.models.ControlModeName.WORK_AREA"></a>

#### WORK\_AREA

visit spots, hold at each, advance (timed/manual)

<a id="vanchor.core.models.GeoPoint"></a>

## GeoPoint Objects

```python
@dataclass(frozen=True)
class GeoPoint()
```

A WGS84 latitude/longitude in decimal degrees.

<a id="vanchor.core.models.GeoPoint.is_null"></a>

#### is\_null

```python
def is_null() -> bool
```

True for the conventional ``(0, 0)`` "no fix" sentinel.

<a id="vanchor.core.models.GpsFix"></a>

## GpsFix Objects

```python
@dataclass(frozen=True)
class GpsFix()
```

A parsed position fix (from an RMC/GGA sentence or a simulated GPS).

<a id="vanchor.core.models.GpsFix.sog_knots"></a>

#### sog\_knots

speed over ground

<a id="vanchor.core.models.GpsFix.cog_deg"></a>

#### cog\_deg

course over ground

<a id="vanchor.core.models.ImuSample"></a>

## ImuSample Objects

```python
@dataclass(frozen=True)
class ImuSample()
```

A raw AHRS/IMU sample in the boat's body frame.

Auxiliary telemetry, populated only when a compass/AHRS driver that exposes
an IMU is active (e.g. the HWT901B); ``None`` otherwise. Accelerations are in
m/s^2, angular rates in deg/s (``gz`` is the yaw rate), roll/pitch in degrees.
Not consumed by the controller yet -- surfaced for logging / debugging and
future sensor fusion (see docs/roadmap.md). ``source`` names the producer
("hwt901b" / "sim").

<a id="vanchor.core.models.ImuSample.gz"></a>

#### gz

yaw rate

<a id="vanchor.core.models.HeadingReading"></a>

## HeadingReading Objects

```python
@dataclass(frozen=True)
class HeadingReading()
```

A compass heading sample in degrees (0..360, magnetic or true).

<a id="vanchor.core.models.MotorCommand"></a>

## MotorCommand Objects

```python
@dataclass(frozen=True)
class MotorCommand()
```

The actuator-level command sent to the motor controller.

``thrust`` is the normalized forward drive (-1 reverse .. 1 full ahead).
``steering`` is the normalized turn command (-1 hard port .. 1 hard
starboard). A trolling motor realizes ``steering`` by physically rotating;
a rudder boat would realize it with a rudder. The abstraction is the same.

<a id="vanchor.core.models.ManualSetpoint"></a>

## ManualSetpoint Objects

```python
@dataclass(frozen=True)
class ManualSetpoint()
```

Mode output: drive the motor directly.

<a id="vanchor.core.models.GuidedSetpoint"></a>

## GuidedSetpoint Objects

```python
@dataclass(frozen=True)
class GuidedSetpoint()
```

Mode output: hold a target heading; the helm derives the steering.

<a id="vanchor.core.models.CrossTrackError"></a>

## CrossTrackError Objects

```python
@dataclass(frozen=True)
class CrossTrackError()
```

Cross-track error relative to a leg. ``distance_m`` is signed: positive
means the boat is to starboard (right) of the intended track.

<a id="vanchor.core.models.CrossTrackError.steer_to"></a>

#### steer\_to

"L" or "R" -- the direction to steer to get back on track

<a id="vanchor.core.models.Environment"></a>

## Environment Objects

```python
@dataclass
class Environment()
```

Wind and current acting on the boat. Directions are *toward* which the
flow pushes, in degrees. Speeds are in m/s.

<a id="vanchor.core.models.Environment.drift_vector"></a>

#### drift\_vector

```python
def drift_vector() -> tuple[float, float]
```

Net environmental drift as an (east, north) velocity in m/s.

<a id="vanchor.core.models.BoatState"></a>

## BoatState Objects

```python
@dataclass
class BoatState()
```

Ground-truth physical state of the (simulated) boat.

<a id="vanchor.core.models.BoatState.heading_deg"></a>

#### heading\_deg

the way the bow points

<a id="vanchor.core.models.BoatState.speed_mps"></a>

#### speed\_mps

forward speed through the water

<a id="vanchor.core.models.BoatState.ground_ve"></a>

#### ground\_ve

east

<a id="vanchor.core.models.BoatState.ground_vn"></a>

#### ground\_vn

north


<a id="vanchor.core.observability"></a>

# vanchor.core.observability

Logging setup, telemetry recording, and an event-bus wiretap.

Three small, independent observability helpers:

* :func:`setup_logging` configures the root logger consistently for the app
  and tests.
* :class:`TelemetryRecorder` keeps an in-memory ring of recent telemetry
  snapshots and (optionally) appends each one as a JSON line to a file, so a
  run can be replayed or inspected after the fact.
* :func:`wiretap` attaches a wildcard subscriber to the :class:`EventBus` that
  logs every ``(topic, payload)`` at DEBUG -- a cheap, central trace of the
  whole system's message flow.
* :class:`DecisionLog` is an optional ring buffer answering "why did the
  controller do that" by recording human-readable reasons with fields.

Everything here is deliberately defensive: recording must never crash the
control loop, so serialization failures and file errors are swallowed and
logged rather than propagated.

<a id="vanchor.core.observability.RingLogHandler"></a>

## RingLogHandler Objects

```python
class RingLogHandler(logging.Handler)
```

Keep the most recent log records in memory for the "View logs" UI.

A bounded ring of decoded records (time/level/logger/message). Cheap and
crash-safe; :func:`log_ring` returns the process-wide singleton so the buffer
survives repeated :func:`setup_logging` calls.

<a id="vanchor.core.observability.RingLogHandler.dump"></a>

#### dump

```python
def dump(min_levelno: int = 0,
         limit: int = 500,
         contains: str | None = None) -> list[dict]
```

Newest-last records at/above ``min_levelno``, optionally text-filtered.

<a id="vanchor.core.observability.log_ring"></a>

#### log\_ring

```python
def log_ring() -> RingLogHandler
```

The process-wide in-memory log ring (created on first use).

<a id="vanchor.core.observability.setup_logging"></a>

#### setup\_logging

```python
def setup_logging(level: str = "INFO", fmt: str | None = None) -> None
```

Configure the root logger with a single stream handler.

Safe to call more than once: existing handlers are cleared first so we do
not accumulate duplicate handlers (and duplicate log lines) across calls.

``level`` is a standard level name (``"DEBUG"``, ``"INFO"`` ...). Unknown
names fall back to ``INFO``.

<a id="vanchor.core.observability.TelemetryRecorder"></a>

## TelemetryRecorder Objects

```python
class TelemetryRecorder()
```

Keep recent telemetry snapshots in memory and optionally on disk.

Each snapshot is a plain ``dict`` (typically ``NavigationState.to_dict()``).
Calling :meth:`record` always appends to an in-memory ring buffer of at most
``ring_size`` entries, and -- when a ``path`` is configured and the file is
open -- also writes the snapshot as one JSON line (JSONL).

The recorder is usable with ``path=None`` for memory-only operation, and is
a no-op-safe context-manager-free design: call :meth:`start` to open the
file, :meth:`stop`/:meth:`close` to flush and close it.

<a id="vanchor.core.observability.TelemetryRecorder.start"></a>

#### start

```python
def start() -> None
```

Open the backing file for appending (no-op when memory-only).

<a id="vanchor.core.observability.TelemetryRecorder.record"></a>

#### record

```python
def record(snapshot: dict) -> None
```

Append ``snapshot`` to the ring buffer and, if open, to the file.

<a id="vanchor.core.observability.TelemetryRecorder.recent"></a>

#### recent

```python
def recent(n: int = 50) -> list[dict]
```

Return up to the last ``n`` recorded snapshots (oldest first).

<a id="vanchor.core.observability.TelemetryRecorder.stop"></a>

#### stop

```python
def stop() -> None
```

Flush and close the backing file (no-op when memory-only).

<a id="vanchor.core.observability.TelemetryRecorder.close"></a>

#### close

```python
def close() -> None
```

Close the backing file; the ring buffer is left intact.

<a id="vanchor.core.observability.wiretap"></a>

#### wiretap

```python
def wiretap(bus: EventBus, logger: logging.Logger | None = None) -> None
```

Log every event published on ``bus`` at DEBUG level.

Attaches a wildcard subscriber via :meth:`EventBus.subscribe_all`. Cheap
when DEBUG is disabled because the handler short-circuits before formatting
the payload.

<a id="vanchor.core.observability.DecisionLog"></a>

## DecisionLog Objects

```python
@dataclass
class DecisionLog()
```

A small ring buffer of controller decisions for after-the-fact debugging.

The controller calls :meth:`record` with a short human-readable ``reason``
and any structured ``fields``; :meth:`recent` returns the most recent
entries (as dicts) for display in the UI or an API endpoint.


<a id="vanchor.core.pid"></a>

# vanchor.core.pid

A small, dependency-free PID controller.

Kept deliberately simple and explicit so its behaviour is easy to reason about
and to unit-test. Supports output clamping with integral anti-windup.

<a id="vanchor.core.pid.PID"></a>

## PID Objects

```python
@dataclass
class PID()
```

<a id="vanchor.core.pid.PID.update"></a>

#### update

```python
def update(measurement: float, dt: float) -> float
```

Standard form: error = setpoint - measurement.

<a id="vanchor.core.pid.PID.update_error"></a>

#### update\_error

```python
def update_error(error: float, dt: float) -> float
```

Drive the loop from a pre-computed error.

Useful for headings, where the error must be the *shortest* angular
difference rather than a naive subtraction.


<a id="vanchor.core.state"></a>

# vanchor.core.state

The single shared navigation state.

This replaces the old project's stringly-typed nested-dict "DataNode". It is a
plain typed object that the navigator writes to and that control modes read
from. ``to_dict`` produces the telemetry payload streamed to the UI.

<a id="vanchor.core.state.NavigationState"></a>

## NavigationState Objects

```python
@dataclass
class NavigationState()
```

Everything the controller knows about the world *as reported by sensors*.

Crucially this is the boat's *perceived* state (from GPS/compass), not the
simulator's ground truth -- the controller only ever steers on what the
sensors tell it, exactly as it would with real hardware.

<a id="vanchor.core.state.NavigationState.fix_seq"></a>

#### fix\_seq

bumped by the navigator on every fresh fix (freshness)

<a id="vanchor.core.state.NavigationState.heading_deg"></a>

#### heading\_deg

latest compass heading

<a id="vanchor.core.state.NavigationState.sog_knots"></a>

#### sog\_knots

speed over ground from GPS

<a id="vanchor.core.state.NavigationState.depth_m"></a>

#### depth\_m

water depth under the boat (from a depth sounder)

<a id="vanchor.core.state.NavigationState.anchor_heading"></a>

#### anchor\_heading

heading to hold while station-keeping

<a id="vanchor.core.state.NavigationState.drift_target_knots"></a>

#### drift\_target\_knots

target speed-over-ground for Drift mode

<a id="vanchor.core.state.NavigationState.contour_side"></a>

#### contour\_side

"deep" | "shallow" -- which way to turn

<a id="vanchor.core.state.NavigationState.orbit_direction"></a>

#### orbit\_direction

"cw" | "ccw"

<a id="vanchor.core.state.NavigationState.route_on_arrival"></a>

#### route\_on\_arrival

"anchor" | "stop" | "none" when route done

<a id="vanchor.core.state.NavigationState.work_holding"></a>

#### work\_holding

currently spot-locked at a spot

<a id="vanchor.core.state.NavigationState.work_dwell_remaining_s"></a>

#### work\_dwell\_remaining\_s

countdown to auto-advance (timed mode)

<a id="vanchor.core.state.NavigationState.work_next_requested"></a>

#### work\_next\_requested

transient: the "next spot" button press

<a id="vanchor.core.state.NavigationState.launch"></a>

#### launch

first good fix, or set via set_launch

<a id="vanchor.core.state.NavigationState.rtl_recommended"></a>

#### rtl\_recommended

battery can *just* make it home -> UI prompt

<a id="vanchor.core.state.NavigationState.est_drift_dir"></a>

#### est\_drift\_dir

degrees the drift pushes toward

