"""The extension kernel — shared plug-in machinery for every vanchor seam.

This is the one home for the plug-in *skeleton* that drivers and connectors were
each re-implementing: **entry-point discovery**, a **typed name→factory
registry**, a **manifest**, and the narrow **capability** a plug-in receives
instead of the ``Runtime``. See ``docs/extensibility.md`` ("The extension
kernel").

``vanchor.ext`` is a **leaf**: it must never import ``app`` / ``runtime`` /
``controller`` (no import cycle — it sits below every consumer). Drivers and
connectors are the first two consumers; today they consume :func:`discover`
while keeping their own bespoke registries (full migration to :class:`Registry`
is follow-up).
"""

from __future__ import annotations

from .capability import Capability
from .discovery import discover
from .manifest import Manifest
from .registry import Registry

__all__ = ["Capability", "Manifest", "Registry", "discover"]
