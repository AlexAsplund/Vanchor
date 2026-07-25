"""Entry-point discovery — the one home for finding installed plug-in packs.

:func:`discover` is the verbatim ``_iter_entry_points`` helper that was copy-
pasted in ``hardware/drivers/__init__.py`` and ``connectors/__init__.py``. It
yields the entry points in a group across importlib.metadata API versions and is
a **quiet no-op** when metadata is unavailable or zero packs are installed — it
must never raise, so a boat with no packs starts exactly as before.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

logger = logging.getLogger("vanchor.ext")


def discover(group: str) -> Iterator:
    """Yield entry points in ``group`` across importlib.metadata API versions.

    Returns nothing (a quiet no-op) if metadata is unavailable — the common case
    of zero installed packs must never raise."""
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover - importlib.metadata is stdlib on 3.11+
        return
    try:
        eps = entry_points()
    except Exception as exc:  # noqa: BLE001 - never let discovery crash startup
        logger.debug("entry-point discovery unavailable: %s", exc)
        return
    # Python 3.12: EntryPoints.select(group=...); older: a dict-like .get(group).
    try:
        selected = eps.select(group=group) if hasattr(eps, "select") else eps.get(group, [])
    except Exception as exc:  # noqa: BLE001
        logger.debug("entry-point discovery failed: %s", exc)
        return
    yield from selected
