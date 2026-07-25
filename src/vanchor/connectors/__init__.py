"""Connectors — consent-gated bridges between the vanchor bus and external systems.

A *connector* is a multiplexed, often bidirectional bridge (NMEA-TCP, metrics
export, NMEA 2000, an RF remote). Where a device driver maps one transducer to one
signal, a connector spans many topics and directions, so its trust model is
stricter and explicit:

* **Manifest** — every connector declares a
  :class:`~vanchor.connectors.base.ConnectorManifest`: the topics it may read
  (``consumes``), the ingress topics it may inject (``produces``), whether it wants
  the governed motor-control capability (``control``), and plain-language consent
  lines.
* **Default-deny** — the :class:`~vanchor.connectors.context.ConnectorContext` is
  the only seam a connector touches, and every subscribe/publish/command is an
  allowlist check against that manifest. Nothing outside the manifest is reachable;
  control topics can never be published, and STOP is always accepted.
* **User consent** — a connector runs only after the user consents to its exact
  manifest. Consent is persisted with a manifest hash; any permission change
  disarms it until re-approved.
* **Control-as-capability** — reaching the motor requires the ``control`` grant and
  routes through the same governed command path the app uses (wired by the Runtime,
  Task 2).

Connectors self-register at import (in-tree modules + installed packs), exactly
like the device-driver loader.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from ..ext import discover

logger = logging.getLogger("vanchor.connectors")

# Entry-point group a pip-installed connector pack registers under.
CONNECTOR_ENTRY_POINT_GROUP = "vanchor.connectors"

_loaded = False


def _load_pack_connectors() -> None:
    """Discover + register connectors from installed packs via entry points. A pack
    that fails to load or register is logged and skipped; no-op with no packs."""
    for ep in discover(CONNECTOR_ENTRY_POINT_GROUP):
        try:
            hook = ep.load()
            if callable(hook):
                hook()
            else:  # pragma: no cover - a non-callable target is a pack bug
                logger.warning("connector pack %r target is not callable; skipping", ep.name)
        except Exception as exc:  # noqa: BLE001 - a bad pack must not break startup
            logger.warning(
                "connector pack %r failed to load: %s", getattr(ep, "name", ep), exc
            )


def load_connectors() -> None:
    """Import every in-tree connector module + every installed connector pack once,
    so they self-register with the registry. Idempotent; failures are logged and
    skipped, never fatal."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_") or mod.name in {"base", "context", "registry"}:
            continue
        try:
            importlib.import_module(f"{__name__}.{mod.name}")
        except Exception as exc:  # noqa: BLE001 - a bad connector must not break startup
            logger.warning("connector %r failed to load: %s", mod.name, exc)
    _load_pack_connectors()
