"""Pluggable device drivers.

Each module in this package registers itself with the driver registry
(:mod:`vanchor.hardware.registry`) at import. :func:`load_drivers` imports them
all, so dropping a new ``<name>.py`` here that calls ``register_driver(...)``
adds a selectable device source with NO other edits (no app.py build-seam change,
no source-list edit). A driver that fails to import is logged and skipped, never
breaking startup.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

logger = logging.getLogger("vanchor.hardware.drivers")

_loaded = False


def load_drivers() -> None:
    """Import every driver module in this package once, so they self-register."""
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
