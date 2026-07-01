"""Registry of pluggable device drivers (external compasses/IMUs/… hardware).

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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

BuildFn = Callable[[Any, Any], Any]  # (runtime, cfg) -> device


@dataclass(frozen=True)
class DriverSpec:
    kind: str          # device kind: "compass" | "gps" | "depth" | "motor" | …
    source: str        # the *_source value that selects it, e.g. "hwt901b"
    build: BuildFn     # (runtime, cfg) -> device
    label: str = ""    # human label for the UI


_REGISTRY: dict[tuple[str, str], DriverSpec] = {}


def register_driver(kind: str, source: str, build: BuildFn, *, label: str = "") -> None:
    """Register a driver as a selectable ``{kind}_source`` value. Idempotent
    (re-registering the same key overwrites)."""
    _REGISTRY[(kind, source)] = DriverSpec(kind, source, build, label)


def has(kind: str, source: str) -> bool:
    return (kind, source) in _REGISTRY


def build_device(kind: str, source: str, runtime: Any, cfg: Any) -> Any:
    return _REGISTRY[(kind, source)].build(runtime, cfg)


def sources(kind: str) -> list[str]:
    """Registered source names for a device kind (stable/sorted)."""
    return sorted(s.source for s in _REGISTRY.values() if s.kind == kind)


def specs(kind: str) -> list[DriverSpec]:
    return [s for s in _REGISTRY.values() if s.kind == kind]
