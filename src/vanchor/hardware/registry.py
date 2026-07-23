"""Registry of pluggable device drivers (external compasses/IMUs/battery/… hardware).

The goal is that adding a new hardware driver is NOT a `app.py` edit: you drop a
module in ``hardware/drivers/`` (or ship a pip-installed *pack*, see below) that
calls :func:`register_driver` / :func:`register_context_driver` at import, and it
automatically becomes a selectable device *source* that the runtime can build and
the UI can list — build seam, source options, and validation all read from here.
The built-in ``sim``/``serial`` devices for gps/compass/depth/motor stay inline
in ``app.py`` (they're the baseline, tightly coupled to the simulator); this
registry is for the *extensible* ones (e.g. the HWT901B compass, the INA226
battery monitor — the reference 4th, non-core device kind).

Two registration flavours, both keyed by ``(kind, source)``:

* **Legacy** ``register_driver(kind, source, build)`` — ``build(runtime, cfg)``
  returns a device and may reach into ``runtime`` for wiring. Kept for the
  existing compass driver + tests; it is *wide* (hands the driver the whole
  runtime) and is therefore **not** what community packs should use.
* **Versioned capability API (roadmap #43)** ``register_context_driver(kind,
  source, build, api_version=...)`` — ``build(ctx)`` receives a NARROW,
  versioned :class:`DriverContext` (publish a reading, report health, read its
  own config, a logger/clock) and **never** the ``Runtime``, motor or governor.
  This is the contract community driver packs target (see
  ``docs/extension-packs.md`` — the safety floor is never a pack concern).

Packs are discovered via the ``vanchor.drivers`` entry-point group (see
:func:`vanchor.hardware.drivers.load_drivers`), so a pip-installed pack registers
itself with zero core edits; discovery no-ops gracefully with no packs installed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..core import events

logger = logging.getLogger("vanchor.hardware.registry")

# Version of the driver/capability API (roadmap #43). Bumped only on a
# breaking change to :class:`DriverContext` or the ``build(ctx)`` contract so a
# pack can declare the version it targets and we can refuse an incompatible one.
DRIVER_API_VERSION = 1

# --------------------------------------------------------------------------- #
# Capability publish whitelist (#43 safety guarantee)
# --------------------------------------------------------------------------- #
# A driver/pack may publish ONLY reading/telemetry onto the bus through its
# narrow capability object -- never a CONTROL topic that could command motion or
# weaken a failsafe. ``DriverContext.publish`` therefore whitelists the safe
# reading seams and refuses anything else (dropped + logged), so a driver can
# never reach the controller's "command" topic (or the motor) through it.
#
# Named reading/telemetry topics a driver may publish to regardless of its kind.
_READING_TOPICS = frozenset({"fixes", "soundings", "health"})
# Control/actuation topics that must ALWAYS be refused, even if a driver's own
# kind namespace would otherwise match (e.g. a "motor" driver + "motor.command").
_CONTROL_TOPICS = frozenset({"command", "commands", "stop", "estop", "e_stop",
                             events.MOTOR_COMMAND})

BuildFn = Callable[[Any, Any], Any]            # legacy: (runtime, cfg) -> device
ContextBuildFn = Callable[["DriverContext"], Any]  # #43: (ctx) -> device


@dataclass(frozen=True)
class DriverSpec:
    kind: str          # device kind: "compass" | "gps" | "depth" | "motor" | "battery" | …
    source: str        # the *_source value that selects it, e.g. "hwt901b" / "ina226"
    build: BuildFn | None = None     # legacy (runtime, cfg) -> device
    label: str = ""    # human label for the UI
    menu: dict | None = None  # static device_menu() schema (defaults), so the UI
                              # can render settings/actions on selection
    # Versioned capability API (roadmap #43): when set, the driver is built via
    # ``ctx_build(ctx)`` with a narrow :class:`DriverContext` instead of the wide
    # legacy ``build(runtime, cfg)``. ``api_version`` is the driver-API version
    # the driver targets (defaults to the current one).
    ctx_build: ContextBuildFn | None = None
    api_version: int = DRIVER_API_VERSION

    @property
    def uses_context(self) -> bool:
        """True for drivers built via the narrow, versioned capability object."""
        return self.ctx_build is not None


# --------------------------------------------------------------------------- #
# The narrow, versioned capability object (roadmap #43)
# --------------------------------------------------------------------------- #
@dataclass
class DriverContext:
    """The NARROW, versioned capability object a driver is built with (#43).

    This is the whole safety contract for community driver packs: a driver gets
    exactly what it legitimately needs and **nothing** that could weaken the
    safety floor. It can:

    * **publish a reading** — :meth:`publish_nmea` (onto ``nmea.in``, the same
      seam the sim/serial sensors use) or :meth:`publish` (a bus topic);
    * **report health** — :meth:`report_health` / :meth:`health`;
    * **read its own config** — :attr:`config` (its config slice only);
    * use a **logger** (:attr:`log`) and a **clock** (:meth:`now`);
    * read the boat's coarse **motion** (:meth:`motion` -> cog/sog) for drivers
      that self-calibrate against GPS course (e.g. compass declination).

    It deliberately does **not** expose the ``Runtime``, the motor, the safety
    governor, or the navigation state — a pack can never reach STOP, the
    deadman, or the failsafes through it. (Tests assert these are absent.)
    """

    kind: str
    source: str
    config: Any = None                     # the driver's own config slice (read-only intent)
    api_version: int = DRIVER_API_VERSION
    # Private collaborators (leading underscore => not part of the public surface
    # a pack should rely on). ``_bus`` is used only to publish readings.
    _bus: Any = None
    _logger: Any = None
    _now: Callable[[], float] = time.time
    _motion: Optional[Callable[[], Optional[tuple]]] = None
    _health: dict = field(default_factory=lambda: {"ok": True, "detail": ""})

    @property
    def log(self) -> logging.Logger:
        """A namespaced logger for the driver (``vanchor.driver.<kind>.<source>``)."""
        if self._logger is None:
            self._logger = logging.getLogger(f"vanchor.driver.{self.kind}.{self.source}")
        return self._logger

    def now(self) -> float:
        """Current time from the runtime's injected clock (deterministic in tests)."""
        return self._now()

    def motion(self) -> Optional[tuple]:
        """Coarse boat motion ``(cog_deg, sog_mps)`` or ``None`` when no fix.

        Read-only — a driver may use it to self-calibrate against GPS course; it
        can neither command motion nor see the motor."""
        return self._motion() if self._motion is not None else None

    async def publish_nmea(self, sentence: str) -> None:
        """Publish a raw NMEA sentence onto the bus (``nmea.in``) — the same seam
        the built-in sensors use, so the navigator/controller are unchanged."""
        if self._bus is not None:
            await self._bus.publish(events.NMEA_IN, sentence)

    def _is_reading_topic(self, topic: str) -> bool:
        """True only for SAFE reading/telemetry topics a driver may publish (#43).

        Allows the sensor input seams (``nmea.in`` / ``imu.in`` / any ``*.in``),
        the driver's OWN kind namespace (e.g. ``battery.health``), and the named
        reading topics (``fixes`` / ``soundings`` / ``health``). ALWAYS refuses a
        control/actuation topic (``command`` / ``motor.command`` / ``stop`` / …)
        so a driver or pack can never command motion or disable a failsafe
        through the capability object -- even a driver whose kind name collides
        with a control namespace."""
        t = str(topic)
        if t in _CONTROL_TOPICS or t == "command" or t.endswith(".command"):
            return False
        if t.endswith(".in"):
            return True
        if t.startswith(f"{self.kind}."):
            return True
        return t in _READING_TOPICS

    async def publish(self, topic: str, payload: Any) -> None:
        """Publish a READING on an event-bus ``topic`` (e.g. ``imu.in``). A narrow
        escape hatch for readings that aren't NMEA.

        Restricted to safe reading/telemetry topics (see :meth:`_is_reading_topic`):
        an attempt to publish a CONTROL topic (``command`` / ``motor.command`` /
        ``stop``) is REFUSED (dropped + logged), never forwarded to the bus -- so a
        driver/pack can never command motion or weaken a failsafe through it (the
        #43 capability guarantee)."""
        if self._bus is None:
            return
        if not self._is_reading_topic(topic):
            self.log.warning(
                "driver %s/%s refused publish on control/unsafe topic %r "
                "(capability object may publish readings only, #43)",
                self.kind, self.source, topic,
            )
            return
        await self._bus.publish(topic, payload)

    def report_health(self, ok: bool, detail: str = "") -> None:
        """Record the driver's health (surfaced to the UI/telemetry)."""
        self._health = {"ok": bool(ok), "detail": str(detail)}

    def health(self) -> dict:
        """The last reported health ``{"ok": bool, "detail": str}``."""
        return dict(self._health)


_REGISTRY: dict[tuple[str, str], DriverSpec] = {}


def register_driver(kind: str, source: str, build: BuildFn, *,
                    label: str = "", menu: dict | None = None) -> None:
    """Register a **legacy** driver (``build(runtime, cfg)``) as a selectable
    ``{kind}_source`` value. Optional ``menu`` is the driver's default
    device_menu() schema, so the UI can render its settings/actions the moment
    the source is selected. Idempotent.

    New drivers — and every community pack — should prefer
    :func:`register_context_driver`, which hands the driver a NARROW, versioned
    capability object instead of the whole runtime."""
    _REGISTRY[(kind, source)] = DriverSpec(kind, source, build=build, label=label, menu=menu)


def register_context_driver(
    kind: str, source: str, build: ContextBuildFn, *,
    api_version: int = DRIVER_API_VERSION, label: str = "", menu: dict | None = None,
) -> None:
    """Register a driver against the **versioned capability API** (roadmap #43).

    ``build(ctx)`` receives a narrow :class:`DriverContext` (never the runtime,
    motor or governor) and returns the device. ``api_version`` is the driver-API
    version the driver targets — kept explicit so a future breaking change can
    reject an incompatible pack rather than mis-build it. Idempotent."""
    if api_version != DRIVER_API_VERSION:
        # Not fatal today (only v1 exists); record + warn so an incompatible pack
        # is visible in the logs rather than silently mis-built.
        logger.warning(
            "driver %s/%s targets driver-API v%s but core is v%s; registering anyway",
            kind, source, api_version, DRIVER_API_VERSION,
        )
    _REGISTRY[(kind, source)] = DriverSpec(
        kind, source, label=label, menu=menu, ctx_build=build, api_version=api_version,
    )


def menus(kind: str) -> dict:
    """``{source: menu_schema}`` for registered drivers of ``kind`` shipping a
    menu -- used to render device settings on selection."""
    return {s.source: s.menu for s in _REGISTRY.values() if s.kind == kind and s.menu}


def has(kind: str, source: str) -> bool:
    return (kind, source) in _REGISTRY


def spec(kind: str, source: str) -> DriverSpec | None:
    """The registered :class:`DriverSpec` for ``(kind, source)`` or ``None``."""
    return _REGISTRY.get((kind, source))


def uses_context(kind: str, source: str) -> bool:
    """True when ``(kind, source)`` is built via the versioned capability API."""
    s = _REGISTRY.get((kind, source))
    return s is not None and s.uses_context


def build_device(kind: str, source: str, runtime: Any, cfg: Any) -> Any:
    """Build a **legacy** ``(runtime, cfg)`` driver. Raises ``KeyError`` if the
    source is unknown and ``TypeError`` if it is a context (v1) driver — callers
    with a runtime should route context drivers through :func:`build_with_context`."""
    s = _REGISTRY[(kind, source)]
    if s.build is None:
        raise TypeError(
            f"{kind}/{source} is a capability-API driver; build it via build_with_context()"
        )
    return s.build(runtime, cfg)


def build_with_context(kind: str, source: str, ctx: "DriverContext") -> Any:
    """Build a driver registered via the versioned capability API (#43), passing
    it the narrow :class:`DriverContext`. Raises ``KeyError`` for an unknown
    source and ``TypeError`` if it is a legacy ``(runtime, cfg)`` driver."""
    s = _REGISTRY[(kind, source)]
    if s.ctx_build is None:
        raise TypeError(f"{kind}/{source} is a legacy driver; build it via build_device()")
    return s.ctx_build(ctx)


def sources(kind: str) -> list[str]:
    """Registered source names for a device kind (stable/sorted)."""
    return sorted(s.source for s in _REGISTRY.values() if s.kind == kind)


def specs(kind: str) -> list[DriverSpec]:
    return [s for s in _REGISTRY.values() if s.kind == kind]
