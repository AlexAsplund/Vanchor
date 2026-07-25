"""A small, generic, typed name→factory registry.

New scaffolding for future seams. Drivers and connectors keep their bespoke
registries for now (full migration is follow-up); this is the shared shape they
will converge on: a ``kind``-scoped, API-versioned map that **logs-and-skips** a
duplicate name or an API-version mismatch rather than raising — the same
degrade-safely posture as ``hardware/registry.py`` (a bad pack must never break
startup).
"""

from __future__ import annotations

import logging
from typing import Generic, TypeVar

from .manifest import Manifest

logger = logging.getLogger("vanchor.ext")

T = TypeVar("T")


class Registry(Generic[T]):
    """A typed ``name -> factory`` registry scoped to one ``kind`` + API version.

    ``register`` **logs-and-skips** (never raises) on a duplicate name or an
    ``api_version`` mismatch, so a bad pack degrades safely instead of breaking
    startup or clobbering a good registration.
    """

    def __init__(self, kind: str, api_version: int) -> None:
        self.kind = kind
        self.api_version = api_version
        self._factories: dict[str, T] = {}
        self._manifests: dict[str, Manifest | None] = {}

    def register(
        self,
        name: str,
        factory: T,
        *,
        api_version: int | None = None,
        manifest: Manifest | None = None,
    ) -> None:
        """Register ``factory`` under ``name``.

        Skips (logs a warning, no raise) if ``name`` is already registered or if
        ``api_version`` is given and does not match this registry's version."""
        if name in self._factories:
            logger.warning(
                "%s plug-in %r already registered; skipping duplicate", self.kind, name
            )
            return
        if api_version is not None and api_version != self.api_version:
            logger.warning(
                "%s plug-in %r targets API v%s but registry is v%s; skipping",
                self.kind, name, api_version, self.api_version,
            )
            return
        self._factories[name] = factory
        self._manifests[name] = manifest

    def get(self, name: str) -> T:
        """The factory registered under ``name`` (raises ``KeyError`` if absent)."""
        return self._factories[name]

    def names(self) -> list[str]:
        """Registered names, stable/sorted."""
        return sorted(self._factories)

    def all(self) -> dict[str, T]:
        """A copy of the ``name -> factory`` map."""
        return dict(self._factories)
