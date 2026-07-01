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

So "supporting a new compass" = "produce `HDM` from your device and publish it".

## How drivers are registered (no `app.py` edits)

Drivers are **self-registering plugins**. You drop a module in
`src/vanchor/hardware/drivers/`; at import it calls `register_driver(...)`; and
that's the entire wiring:

- `hardware/registry.py` holds the registry. `register_driver(kind, source, build, *, label)`.
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

The HWT901B compass driver is the reference — copy its shape.

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
    return open_my_compass(hw.compass_port, hw.baudrate, runtime.bus,
                           hz=cfg.sensors.compass_hz)
```

`build(runtime, cfg)` may read `runtime.bus`, `runtime.state` (e.g. GPS
course/speed), and `cfg`. Do heavy/optional imports **inside** the factory (see
"Optional dependencies").

**3. Register it** at module top level:

```python
from ..registry import register_driver
register_driver("compass", "hwt901b", _build, label="WitMotion HWT901B AHRS")
```

Done — `compass_source="hwt901b"` now builds, validates, and appears in the
device options. No other file changes.

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
no bespoke UI per device. Implement three methods:

```python
def device_menu(self) -> dict:
    return {
        "device": "compass",
        "title": "Compass — My AHRS",
        "settings": [
            {"key": "declination_mode", "label": "Declination", "type": "select",
             "options": ["auto", "manual", "off"], "value": self.declination_mode},
            {"key": "hz", "label": "Update rate", "type": "number",
             "min": 1, "max": 50, "step": 1, "unit": "Hz", "value": self.hz},
        ],
        "actions": [
            {"name": "profile", "label": "Sensor status", "help": "…"},
            {"name": "calibrate_mag", "label": "Calibrate magnetometer", "help": "…"},
        ],
    }

def apply_setting(self, key, value) -> dict:  # {"ok": bool, "message"?: str}
def run_action(self, name, params=None) -> dict:
```

The runtime collects `device_menu()` from the active devices into
`GET /api/config/devices` under `"menus"`, and the UI renders the fields + action
buttons from that schema. Field `type`s: `select` (with `options`), `number`
(min/max/step/unit), `toggle`. `shown_when: {key: value}` conditionally shows a
field. Keep menus declarative — the UI is a generic renderer.

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
- [ ] `register_driver(kind, source, build, label=...)` at module top level.
- [ ] Optional dep declared as an extra in `pyproject.toml`.
- [ ] (Optional) `device_menu()` / `apply_setting()` / `run_action()`.
- [ ] Tests with a fake device + a self-registration assertion.
- [ ] This guide + [backend.md](backend.md) updated if you changed the contract.
