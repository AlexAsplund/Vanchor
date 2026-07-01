# `vanchor.hardware`

<a id="vanchor.hardware"></a>

# vanchor.hardware

Hardware abstraction: real devices + the pluggable driver registry.

``interfaces.py`` defines the ``Sensor`` / ``MotorController`` / ``Actuator``
seam that :mod:`vanchor.sim` mirrors, so nothing above the device layer can tell
simulation from hardware. ``serial_devices.py`` / ``serial_link.py`` implement
wired GPS/compass/motor over NMEA/serial. ``registry.py`` + the ``drivers/``
package are a plugin system: a driver drops a self-registering module and becomes
a selectable device source — no edit to the runtime's build seam. See
``docs/adding-a-device.md``.


<a id="vanchor.hardware.drivers"></a>

# vanchor.hardware.drivers

Pluggable device drivers.

Each module in this package registers itself with the driver registry
(:mod:`vanchor.hardware.registry`) at import. :func:`load_drivers` imports them
all, so dropping a new ``<name>.py`` here that calls ``register_driver(...)``
adds a selectable device source with NO other edits (no app.py build-seam change,
no source-list edit). A driver that fails to import is logged and skipped, never
breaking startup.

<a id="vanchor.hardware.drivers.load_drivers"></a>

#### load\_drivers

```python
def load_drivers() -> None
```

Import every driver module in this package once, so they self-register.


<a id="vanchor.hardware.drivers.hwt901b"></a>

# vanchor.hardware.drivers.hwt901b

Pluggable compass driver for the WitMotion HWT901B-TTL 9-axis AHRS.

Wraps the external ``hwt901b`` library and adapts it to vanchor's device seam: a
:class:`~vanchor.hardware.interfaces.Sensor` that emits ``HDM`` NMEA onto the bus
(``nmea.in``), exactly like ``SimCompass``/``SerialCompass`` -- so the navigator,
controller and every mode are unchanged. Registers itself as the compass source
``"hwt901b"`` (see :mod:`vanchor.hardware.registry`), so it needs no edit to
``app.py``'s build seam.

Two device-specific features:

* **Auto-declination (+ mount offset).** The magnetometer reads *magnetic*
  heading; true heading needs the local declination, and a real install has a
  fixed mount misalignment. Rather than make the skipper type a number, the
  combined offset is learned by comparing the compass heading to the GPS
  course-over-ground on straight-line runs (:class:`HeadingOffsetEstimator`) --
  no magnetic-model data to ship, and the mount error is corrected for free.
* **Device menu.** :meth:`HWT901BCompass.device_menu` advertises device-specific
  settings + actions (declination mode, magnetometer calibration, profiling) the
  UI renders generically -- the pattern any future smart device follows.

The ``hwt901b`` library is optional and imported lazily, so the core install and
the simulator never need it; the driver itself is fully testable with a fake
sensor (no serial port).

<a id="vanchor.hardware.drivers.hwt901b.default_menu"></a>

#### default\_menu

```python
def default_menu() -> dict
```

The default (factory) menu schema advertised by the registry.

<a id="vanchor.hardware.drivers.hwt901b.HeadingOffsetEstimator"></a>

## HeadingOffsetEstimator Objects

```python
class HeadingOffsetEstimator()
```

Learn the fixed offset (declination + compass mount error) between the
magnetic heading and true north, from GPS course-over-ground.

While the boat runs roughly straight above ``min_sog_mps``,
``course_over_ground - magnetic_heading`` is that offset (plus noise). We
low-pass it with a long time constant so turns and GPS jitter average out and
only sustained straight-line agreement moves the estimate.

<a id="vanchor.hardware.drivers.hwt901b.HWT901BCompass"></a>

## HWT901BCompass Objects

```python
class HWT901BCompass(Sensor)
```

HWT901B AHRS presented as a vanchor NMEA compass sensor.

``sensor`` is anything with ``read_true_heading(declination_deg, timeout)``
and ``close()`` -- the real :class:`hwt901b.HWT901B` or a fake in tests.
``motion_provider`` feeds GPS (cog, sog) for auto-declination.

<a id="vanchor.hardware.drivers.hwt901b.HWT901BCompass.sample_once"></a>

#### sample\_once

```python
async def sample_once(dt: float) -> str | None
```

Read one heading and return the HDM sentence (or None on timeout). The
blocking serial read runs in a thread so it never stalls the loop.

<a id="vanchor.hardware.drivers.hwt901b.open_hwt901b_compass"></a>

#### open\_hwt901b\_compass

```python
def open_hwt901b_compass(
        port: str,
        baudrate: int,
        bus: EventBus | None,
        *,
        hz: float = 5.0,
        motion_provider: MotionProvider | None = None,
        declination_mode: str = "auto",
        manual_declination_deg: float = 0.0) -> HWT901BCompass
```

Open the HWT901B on ``port`` and wrap it. Lazily imports the optional
``hwt901b`` library (clear error if missing).


<a id="vanchor.hardware.interfaces"></a>

# vanchor.hardware.interfaces

Hardware abstraction layer.

These abstract base classes are the seam between the controller and the
physical world. The simulator implements exactly these interfaces, so swapping
in real serial hardware later means writing new subclasses -- nothing in the
controller or control logic changes.

  Sensor          -- something that produces NMEA and pushes it onto the bus
  MotorController  -- accepts a normalized MotorCommand (thrust + steering)
  Actuator         -- a generic servo/stepper position channel (0..1 or angle)

<a id="vanchor.hardware.interfaces.Sensor"></a>

## Sensor Objects

```python
class Sensor(abc.ABC)
```

A device that emits NMEA sentences (GPS, compass, ...).

Implementations publish raw sentences onto the event bus (topic
``nmea.in``). ``start``/``stop`` manage any background polling task.

<a id="vanchor.hardware.interfaces.MotorController"></a>

## MotorController Objects

```python
class MotorController(abc.ABC)
```

Accepts actuator-level motor commands.

A real implementation translates ``thrust`` to an ESC/PWM signal and
``steering`` to a stepper/servo position over serial; the simulator applies
them to the boat physics. ``apply`` is synchronous-friendly so the control
loop can call it deterministically; ``flush`` does any async I/O.

<a id="vanchor.hardware.interfaces.MotorController.apply"></a>

#### apply

```python
@abc.abstractmethod
def apply(command: MotorCommand) -> None
```

Record/translate the latest command (must be cheap, non-blocking).

<a id="vanchor.hardware.interfaces.MotorController.flush"></a>

#### flush

```python
async def flush() -> None
```

Push the latest command to the device (no-op for the simulator).

<a id="vanchor.hardware.interfaces.Actuator"></a>

## Actuator Objects

```python
class Actuator(abc.ABC)
```

A generic single-channel actuator (servo angle or stepper position).

Provided to satisfy the "general servo/stepper motor control interface"
requirement: real steering hardware (a stepper driving the motor head, or a
servo) implements this; :mod:`vanchor.sim.devices` provides a simulated one.

<a id="vanchor.hardware.interfaces.Actuator.set_normalized"></a>

#### set\_normalized

```python
@abc.abstractmethod
def set_normalized(value: float) -> None
```

Command a position in the range [-1, 1].

<a id="vanchor.hardware.interfaces.Actuator.position"></a>

#### position

```python
@property
@abc.abstractmethod
def position() -> float
```

The actuator's current normalized position.


<a id="vanchor.hardware.registry"></a>

# vanchor.hardware.registry

Registry of pluggable device drivers (external compasses/IMUs/… hardware).

The goal is that adding a new hardware driver is NOT a `app.py` edit: you drop a
module in ``hardware/drivers/`` that calls :func:`register_driver` at import, and
it automatically becomes a selectable device *source* that the runtime can build
and the UI can list — build seam, source options, and validation all read from
here. The built-in ``sim``/``serial`` devices stay inline in ``app.py`` (they're
the baseline, tightly coupled to the simulator); this registry is for the
extensible ones.

A driver's ``build(runtime, cfg)`` returns a
:class:`~vanchor.hardware.interfaces.Sensor` (or motor) and may access
``runtime.bus`` / ``runtime.state`` for wiring. If the built device exposes a
``device_menu()`` method, the runtime surfaces it to the UI (device-specific
settings/actions).

<a id="vanchor.hardware.registry.BuildFn"></a>

#### BuildFn

(runtime, cfg) -> device

<a id="vanchor.hardware.registry.DriverSpec"></a>

## DriverSpec Objects

```python
@dataclass(frozen=True)
class DriverSpec()
```

<a id="vanchor.hardware.registry.DriverSpec.kind"></a>

#### kind

device kind: "compass" | "gps" | "depth" | "motor" | …

<a id="vanchor.hardware.registry.DriverSpec.source"></a>

#### source

the *_source value that selects it, e.g. "hwt901b"

<a id="vanchor.hardware.registry.DriverSpec.build"></a>

#### build

(runtime, cfg) -> device

<a id="vanchor.hardware.registry.DriverSpec.label"></a>

#### label

human label for the UI

<a id="vanchor.hardware.registry.DriverSpec.menu"></a>

#### menu

static device_menu() schema (defaults), so the UI

<a id="vanchor.hardware.registry.register_driver"></a>

#### register\_driver

```python
def register_driver(kind: str,
                    source: str,
                    build: BuildFn,
                    *,
                    label: str = "",
                    menu: dict | None = None) -> None
```

Register a driver as a selectable ``{kind}_source`` value. Optional
``menu`` is the driver's default device_menu() schema, so the UI can render
its settings/actions the moment the source is selected. Idempotent.

<a id="vanchor.hardware.registry.menus"></a>

#### menus

```python
def menus(kind: str) -> dict
```

``{source: menu_schema}`` for registered drivers of ``kind`` shipping a
menu -- used to render device settings on selection.

<a id="vanchor.hardware.registry.sources"></a>

#### sources

```python
def sources(kind: str) -> list[str]
```

Registered source names for a device kind (stable/sorted).


<a id="vanchor.hardware.serial_devices"></a>

# vanchor.hardware.serial\_devices

Real-hardware serial drivers (GPS, compass, motor controller).

These implement the same ABCs as the simulated devices
(:mod:`vanchor.sim.devices`), so the controller, navigator and event wiring
cannot tell them apart -- swapping ``Sim*`` for ``Serial*`` is the entirety of
"running on real hardware". All byte-level I/O goes through a
:class:`~vanchor.hardware.serial_link.SerialTransport`, so every driver here is
fully testable with :class:`~vanchor.hardware.serial_link.FakeSerialTransport`
and never opens a physical port.

  SerialGps / SerialCompass  -- read NMEA lines from a transport and republish
                                each one onto the event bus (topic ``nmea.in``)
  SerialMotorController      -- translate a :class:`MotorCommand` into a simple
                                ASCII line protocol for an Arduino-style board

<a id="vanchor.hardware.serial_devices.SteeringFeedback"></a>

## SteeringFeedback Objects

```python
@dataclass(frozen=True)
class SteeringFeedback()
```

One decoded steering-feedback report from the steering Arduino.

The firmware (``firmware/steering/steering.ino``) emits a line of the form
``A <angle_deg> <ok> <wrap_pct>`` at ~10 Hz where ``angle_deg`` is the
measured feedback azimuth, ``ok`` is a 1/0 plausibility flag and
``wrap_pct`` the cable-wrap fraction (percent).

<a id="vanchor.hardware.serial_devices.parse_steering_feedback"></a>

#### parse\_steering\_feedback

```python
def parse_steering_feedback(line: str) -> SteeringFeedback | None
```

Parse one ``A <angle_deg> <ok> <wrap_pct>`` feedback line.

Returns a :class:`SteeringFeedback` on success or ``None`` for anything
that is not a well-formed ``A`` report (blank lines, other line types such
as ``CMD``/``E`` echoes, truncated/partial lines, non-numeric fields). This
is deliberately lenient so a noisy or partially-buffered serial stream can
never raise out of the read loop.

<a id="vanchor.hardware.serial_devices.SerialGps"></a>

## SerialGps Objects

```python
class SerialGps(_SerialNmeaSensor)
```

A GPS receiver on a serial port emitting RMC/GGA sentences.

<a id="vanchor.hardware.serial_devices.SerialCompass"></a>

## SerialCompass Objects

```python
class SerialCompass(_SerialNmeaSensor)
```

A digital compass on a serial port emitting HDM/HDG sentences.

<a id="vanchor.hardware.serial_devices.SerialMotorController"></a>

## SerialMotorController Objects

```python
class SerialMotorController(MotorController)
```

Drive an Arduino-style motor board over a serial line protocol.

Line protocol (newline-terminated, one command per :meth:`flush`)::

    CMD <pwm> <dir> <steer>

where:

  ``pwm``    integer 0..255  -- magnitude of thrust (0 = stop)
  ``dir``    ``F`` or ``R``  -- forward or reverse (drive direction)
  ``steer``  integer -100..100 -- steering: -100 hard port, +100 hard
             starboard, 0 centred

Example lines::

    CMD 0 F 0          # stopped, centred
    CMD 255 F 0        # full ahead, centred
    CMD 128 R -100     # half astern, hard port
    CMD 255 F 100      # full ahead, hard starboard

The normalized :class:`MotorCommand` maps as ``pwm = round(|thrust| * 255)``
and ``steer = round(steering * 100)``; ``thrust >= 0`` is ``F`` else ``R``.

**Reverse delay.** Real ESCs / motor drivers can be damaged (or stall) by an
instantaneous forward<->reverse reversal. This controller enforces a
``reverse_delay_s`` (default 0.9 s) during which a thrust *sign flip* is
blocked: when the requested direction would reverse, the commanded thrust is
forced to zero until thrust has been ~zero for at least the delay, then the
new direction is allowed. Time is read from an injectable ``time_fn`` so the
behaviour is deterministic in tests (the delay is evaluated on each
:meth:`flush`).

<a id="vanchor.hardware.serial_devices.SerialMotorController.ZERO_THRUST_EPS"></a>

#### ZERO\_THRUST\_EPS

Thrust magnitudes at or below this are treated as "stopped" for the
purpose of the reverse-delay interlock.

<a id="vanchor.hardware.serial_devices.SerialMotorController.flush"></a>

#### flush

```python
async def flush() -> None
```

Apply the reverse-delay interlock and write the latest command.

<a id="vanchor.hardware.serial_devices.SerialMotorController.format_command"></a>

#### format\_command

```python
def format_command(command: MotorCommand) -> str
```

Format ``command`` to its protocol line (no interlock; for tests).


<a id="vanchor.hardware.serial_link"></a>

# vanchor.hardware.serial\_link

Serial transport abstraction for real-hardware drivers.

The drivers in :mod:`vanchor.hardware.serial_devices` talk to physical GPS,
compass and motor controllers over a serial line. To keep them testable with
*no* physical port (and to keep the import graph free of a hard ``pyserial``
dependency), all byte-level I/O goes through a small line-oriented transport
abstraction:

  SerialTransport       -- the interface: open/close + read_line/write_line
  FakeSerialTransport   -- in-memory transport for tests; push inbound lines,
                           inspect outbound lines
  PySerialTransport     -- real transport backed by ``serial_asyncio``; the
                           import is guarded so importing this module never
                           requires the ``serial`` extra to be installed

Lines are newline-delimited UTF-8 strings; the transport strips the trailing
newline on read and appends ``\r\n`` on write (standard for NMEA / Arduino
serial protocols).

<a id="vanchor.hardware.serial_link.SerialTransport"></a>

## SerialTransport Objects

```python
class SerialTransport(abc.ABC)
```

A line-oriented, asynchronous serial transport.

Implementations move whole text lines to and from some underlying byte
stream. Drivers depend only on this interface, so the same driver code runs
against a real port (:class:`PySerialTransport`) or an in-memory fake
(:class:`FakeSerialTransport`).

<a id="vanchor.hardware.serial_link.SerialTransport.open"></a>

#### open

```python
@abc.abstractmethod
async def open() -> None
```

Open/connect the underlying stream (idempotent).

<a id="vanchor.hardware.serial_link.SerialTransport.close"></a>

#### close

```python
@abc.abstractmethod
async def close() -> None
```

Close the underlying stream (idempotent).

<a id="vanchor.hardware.serial_link.SerialTransport.read_line"></a>

#### read\_line

```python
@abc.abstractmethod
async def read_line() -> str
```

Read one line, with the line terminator stripped.

Blocks (asynchronously) until a line is available. May raise
:class:`asyncio.CancelledError` when the awaiting task is cancelled, or
``EOFError`` when the stream is closed.

<a id="vanchor.hardware.serial_link.SerialTransport.write_line"></a>

#### write\_line

```python
@abc.abstractmethod
async def write_line(line: str) -> None
```

Write one line; the terminator (``\r\n``) is appended here.

<a id="vanchor.hardware.serial_link.FakeSerialTransport"></a>

## FakeSerialTransport Objects

```python
class FakeSerialTransport(SerialTransport)
```

In-memory transport for deterministic tests.

Tests push inbound lines with :meth:`feed` (which a reader picks up via
:meth:`read_line`) and inspect everything a driver wrote via the
:attr:`written` list.

<a id="vanchor.hardware.serial_link.FakeSerialTransport.feed"></a>

#### feed

```python
def feed(line: str) -> None
```

Make ``line`` available to the next :meth:`read_line` call.

<a id="vanchor.hardware.serial_link.FakeSerialTransport.feed_eof"></a>

#### feed\_eof

```python
def feed_eof() -> None
```

Signal end-of-stream; a pending/next :meth:`read_line` raises EOF.

<a id="vanchor.hardware.serial_link.PySerialTransport"></a>

## PySerialTransport Objects

```python
class PySerialTransport(SerialTransport)
```

Real serial transport backed by ``pyserial-asyncio``.

The ``serial_asyncio`` import is deferred to :meth:`open` so that merely
importing this module never requires the optional ``serial`` extra or any
physical hardware. Construct it with a device ``port`` (e.g.
``"/dev/ttyUSB0"``) and a ``baudrate``.

