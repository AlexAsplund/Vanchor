# Writing a device driver

> Part of the `docs/llms/` guide. How to add support for a new piece of hardware
> (a compass/IMU, GPS, depth sounder, motor board, …) **without editing the
> runtime's build seam**. If you add or change the driver registry, the device
> seam, or the `device_menu` contract, update this file.

## The one invariant to respect

Every sensor — simulated or real — **emits NMEA sentences onto the event bus**
(topic `events.NMEA_IN` = `"nmea.in"`), and the **navigator is the single parse
point**. Nothing above the device layer knows whether data came from the
simulator, a serial NMEA receiver, or your driver. Your driver's job is: read the
hardware, produce the right NMEA sentence(s), publish them. That's it — the
controller, modes, and UI are unchanged. (Motors are the mirror: they accept a
`MotorCommand`; see `MotorController`.)

So "supporting a new compass" = "produce `HDM` or `HDT` from your device and
publish it". If your hardware applies its own declination and emits **true**
heading, emit `HDT` so the navigator does not double-correct. If it emits
**magnetic** heading, emit `HDM`; the navigator applies `magnetic_declination_deg`
from the boat config. The HWT901B driver emits `HDT` because it applies
declination internally.

## Serial auto-reconnect (`hardware/serial_link.py`)

Production serial devices should survive cable-pulls and port resets. The shared
**read supervisor** in `serial_link.py` handles this:

- On an EOF or read error the supervisor closes the port and waits with
  **exponential backoff** before re-opening.
- Two pollable attributes are available on any driver using the supervisor:
  - `healthy: bool` — `True` while data is flowing; `False` after the first
    missed deadline or while reconnecting.
  - `last_data_monotonic: float | None` — `time.monotonic()` stamp of the most
    recent good read, or `None` before the first.
- The runtime's `_device_health()` reads these to populate the `"devices"` sub-
  block in the telemetry `"health"` field (sim devices don't expose them, so the
  block is absent in pure-sim mode).
- `motor.flush()` no longer raises when the link is down — it returns silently so
  a partially-connected state never kills the control loop.

**Through-zero reverse interlock.** Both the safety governor
(`controller/safety.py`) and the serial motor driver carry a **sticky
applied-direction memory**. When the commanded thrust crosses zero, the driver
holds at zero for `reverse_delay_s` before applying the opposite direction.  The
fix prevents the interlock from being bypassed by a sequence of commands that
each cross zero independently.

## NMEA checksum strictness

`nav/nmea.py` `parse()` rejects:
- A sentence that has `*XX` but the checksum is **empty, non-hex, or wrong** —
  always rejected, regardless of `require_checksum`.
- A sentence with **no** `*XX` at all — rejected when `require_checksum=True`
  (used for inbound TCP lines to prevent spoof/garbage injection).

Do not weaken these — the old lenient behaviour allowed garbage-in from network
sources to corrupt navigator state.

## How drivers are registered (no `app.py` edits)

Drivers are **self-registering plugins**. You drop a module in
`src/vanchor/hardware/drivers/`; at import it calls `register_driver(...)`; and
that's the entire wiring:

- `hardware/registry.py` holds the registry. `register_driver(kind, source, build, *, label, menu=None)`.
- `hardware/drivers/__init__.py`'s `load_drivers()` imports every module in the
  package (auto-discovery), so your module runs and registers itself.
- `app.py` calls `load_drivers()` once at import, then **builds, validates, and
  lists** device sources *from the registry*. Adding a driver never touches the
  build seam or a source list.

`kind` is the device slot (`"compass"`, `"gps"`, `"depth"`, `"motor"`); `source`
is the value a user selects (`compass_source="hwt901b"`); `build(runtime, cfg)`
returns the device.

> The built-in `sim` / `serial` devices stay inline in `app.py` (they're the
> baseline, tightly bound to the simulator). The registry is for everything else.

## Anatomy of a driver (worked example: `hwt901b.py`)

The HWT901B AHRS driver is the reference — copy its shape. Key behaviours
specific to that device (don't regress them):

- **Emits `HDT` (true heading)**, not `HDM`. The driver applies declination
  internally (auto, manual, or off) before publishing, so the navigator never
  double-corrects.
- **Auto-declination estimator** needs ≥ 10 consistent `cog − magnetic_heading`
  samples over ≥ 30 s with a low residual spread before it settles; it rejects
  motion with low SOG or high yaw rate to avoid noise contamination.
- **`hz` setting applies live** — `apply_setting("hz", value)` changes the
  sample loop period on a running device without a restart.
- **`device_menu()`** exposes `declination_mode` (auto / manual / off),
  `manual_declination_deg` (shown only when mode = manual), and `hz`, plus
  actions for sensor profiling and magnetometer calibration.

**1. The device class** implements the `Sensor` ABC (`start`/`stop`) and runs a
background loop that publishes NMEA:

```python
from ..interfaces import Sensor
from ...core import events

class MyCompass(Sensor):
    def __init__(self, sensor, bus=None, *, hz=5.0):
        self._sensor, self.bus, self.hz = sensor, bus, hz
        self._task = None

    async def start(self):
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self):
        if self._task: self._task.cancel(); self._task = None
        # close the port off the event loop if the read is blocking:
        await asyncio.get_event_loop().run_in_executor(None, self._sensor.close)

    async def _loop(self):
        period = 1.0 / self.hz
        while True:
            # Blocking serial reads go in a thread so they never stall the loop:
            heading = await asyncio.get_event_loop().run_in_executor(
                None, self._sensor.read_heading)
            if self.bus:
                await self.bus.publish(events.NMEA_IN, nmea.encode_hdm(heading))
            await asyncio.sleep(period)
```

Rules that bite:
- **Never block the event loop.** `pyserial` reads are blocking → `run_in_executor`.
- **Publish valid NMEA** (`nav/nmea.encode_hdm/encode_hdt/encode_rmc/...`); the
  navigator validates. Don't set `state` directly — that breaks the invariant.
- **Keep the loop alive**: catch read/timeout errors, log at debug, continue.

**2. A build hook** the registry calls, wired to the runtime:

```python
def _build(runtime, cfg):
    hw = cfg.hardware
    saved = (getattr(hw, "device_settings", None) or {}).get("compass", {})  # persisted menu choices
    return open_my_compass(hw.compass_port, hw.baudrate, runtime.bus,
                           hz=float(saved.get("hz", cfg.sensors.compass_hz)))
```

`build(runtime, cfg)` may read `runtime.bus`, `runtime.state` (e.g. GPS
course/speed), and `cfg`. Read persisted menu choices from
`cfg.hardware.device_settings[<kind>]` and apply them (see "Device menu"). Do
heavy/optional imports **inside** the factory (see "Optional dependencies").

Building is **crash-safe**: if `_build` raises (missing lib, no serial port), the
runtime logs it and runs without that device — startup never dies. Don't swallow
the error yourself; let it propagate.

**3. Register it** at module top level, with an optional `menu` schema:

```python
from ..registry import register_driver
register_driver("compass", "hwt901b", _build,
                label="WitMotion HWT901B AHRS", menu=default_menu())
```

Done — `compass_source="hwt901b"` now builds, validates, appears in the device
options, and (via `menu=`) shows its settings **the moment it's selected**. No
other file changes. Wired drivers automatically get the serial-port field in the
UI (any source that isn't sim/nmea/auto is treated as wired).

## Optional dependencies (don't break the core install)

A driver for exotic hardware must not force its dependency on everyone:
- The driver **module** must import cleanly with only the stdlib + vanchor (so
  `load_drivers()` can import it to register). Put the third-party import **inside
  the factory**, lazily, with a clear error:
  ```python
  def open_my_compass(...):
      try:
          from mylib import Device
      except ImportError as exc:
          raise RuntimeError("compass_source='mycompass' needs mylib: "
                             "pip install 'vanchor[mycompass]'") from exc
      ...
  ```
- Declare an **extra** in `pyproject.toml` (`[project.optional-dependencies]`),
  e.g. `mycompass = ["mylib>=1.0"]`. The core install and the simulator never pull it.

## Device-specific settings menu (`device_menu`)

A driver can expose its own settings + actions that the UI renders generically —
no bespoke UI per device. Define the schema **once** so it works both as the
registered default (shown on selection) and live on the instance:

```python
def _menu_schema(declination_mode, hz):
    return {
        "device": "compass",
        "title": "Compass — My AHRS",
        "settings": [
            {"key": "declination_mode", "label": "Declination", "type": "select",
             "options": ["auto", "manual", "off"], "value": declination_mode},
            {"key": "hz", "label": "Update rate", "type": "number",
             "min": 1, "max": 50, "step": 1, "unit": "Hz", "value": hz},
        ],
        "actions": [
            {"name": "profile", "label": "Sensor status", "help": "…"},
            {"name": "calibrate_mag", "label": "Calibrate magnetometer", "help": "…"},
        ],
    }

def default_menu():                 # factory defaults, passed to register_driver(menu=)
    return _menu_schema("auto", 5.0)

class MyCompass(Sensor):
    def device_menu(self):          # live values from the running instance
        return _menu_schema(self.declination_mode, self.hz)
    def apply_setting(self, key, value) -> dict:   # {"ok": bool, "message"?: str}
    def run_action(self, name, params=None) -> dict:
```

**Where the schema surfaces** in `GET /api/config/devices`:
- `driver_menus` — `{source: schema}` from every registered driver's `menu=`,
  with **saved values overlaid**. The UI renders this the moment you *select* the
  source, before any instance exists.
- `menus` — the live menu of each *running* device (live values, e.g. a learned
  offset).

**Settings persist.** `apply_setting` on the instance changes live behaviour, but
the runtime also writes the value to `HardwareConfig.device_settings[<kind>]`
(persisted in `devices.json`). Your `_build` reads those back (above), so a choice
survives a restart and applies even when the device wasn't running when it was
set. The UI shows "Saved — restart to apply" when there's no live device.

Field `type`s: `select` (with `options`), `number` (min/max/step/unit), `toggle`.
`shown_when: {key: value}` conditionally shows a field. Actions that talk to
hardware (profile/calibrate) only work while the device is running. Keep menus
declarative — the UI is a generic renderer.

## Testing (stay sim-first — no serial port, no vendor lib)

Inject a **fake device** at the driver's boundary so tests need neither hardware
nor the optional lib:

```python
class _FakeSensor:
    def read_heading(self): return 123.0
    def close(self): ...

async def test_emits_hdm():
    d = MyCompass(_FakeSensor(), bus=None)
    assert await d.sample_once(0.2) == nmea.encode_hdm(123.0)
```

Split the read+encode into a `sample_once(dt)` method (like `hwt901b.py`) so it's
directly awaitable in a test without running the loop. Also assert your driver
**self-registers**: `registry.has("compass", "mysource")` after `load_drivers()`.
See `tests/test_hwt901b_compass.py`.

## Checklist

- [ ] Module in `hardware/drivers/`, imports cleanly (vendor import is lazy).
- [ ] Device class implements `Sensor` (`start`/`stop`), publishes NMEA to the bus.
- [ ] Blocking I/O runs in an executor; the read loop survives errors.
- [ ] `register_driver(kind, source, build, label=..., menu=...)` at module top level.
- [ ] Optional dep declared as an extra in `pyproject.toml`.
- [ ] (Optional) `device_menu()` / `apply_setting()` / `run_action()`, with a
      `menu=` schema for show-on-selection, and `_build` reading persisted
      `device_settings`.
- [ ] Tests with a fake device + a self-registration assertion.
- [ ] This guide + [backend.md](backend.md) updated if you changed the contract.
