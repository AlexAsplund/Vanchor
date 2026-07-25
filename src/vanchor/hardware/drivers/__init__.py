"""Pluggable device drivers.

Two discovery paths, both self-registering with the driver registry
(:mod:`vanchor.hardware.registry`):

* **In-tree** — each module in this package registers itself at import.
  :func:`load_drivers` imports them all, so dropping a new ``<name>.py`` here that
  calls ``register_driver(...)`` / ``register_context_driver(...)`` adds a
  selectable device source with NO other edits (no app.py build-seam change, no
  source-list edit).
* **Installed packs (roadmap #43)** — a pip-installed community pack advertises a
  callable under the ``vanchor.drivers`` entry-point group; :func:`load_drivers`
  loads and invokes each one so the pack's drivers register themselves. This is
  what makes drivers pip-installable without any core edit. Discovery **no-ops
  gracefully** when no packs are installed.

A driver (in-tree or pack) that fails to import/register is logged and skipped,
never breaking startup — the whole point of the narrow capability contract is
that a bad pack degrades safely.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from ...ext import discover

logger = logging.getLogger("vanchor.hardware.drivers")

# Entry-point group a pip-installed driver pack registers under (roadmap #43).
DRIVER_ENTRY_POINT_GROUP = "vanchor.drivers"

_loaded = False


def _load_pack_drivers() -> None:
    """Discover + register drivers from installed packs via entry points.

    Each entry point resolves to a callable (a registration hook); we call it so
    the pack registers its driver(s). A pack that fails to load or register is
    logged and skipped. No-op when no packs are installed."""
    for ep in discover(DRIVER_ENTRY_POINT_GROUP):
        try:
            hook = ep.load()
            if callable(hook):
                hook()
            else:  # pragma: no cover - a non-callable target is a pack bug
                logger.warning("driver pack %r target is not callable; skipping", ep.name)
        except Exception as exc:  # noqa: BLE001 - a bad pack must not break startup
            logger.warning("driver pack %r failed to load: %s", getattr(ep, "name", ep), exc)


def load_drivers() -> None:
    """Import every in-tree driver module + every installed driver pack once, so
    they self-register. Idempotent."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{mod.name}")
        except Exception as exc:  # noqa: BLE001 - a bad driver must not break startup
            logger.warning("device driver %r failed to load: %s", mod.name, exc)
    _load_pack_drivers()
