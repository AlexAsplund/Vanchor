"""The plug-in manifest — what a pack declares about itself.

A small, **frozen** (hashable, so consent can key on it) dataclass carrying the
identity + contract a pack advertises: name, version, kind, the API version it
targets, its declared capabilities, and author. See ``docs/extensibility.md``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Manifest:
    """What a plug-in pack declares about itself (hashable — consent can key on it).

    * ``name`` — the pack's unique name.
    * ``version`` — the pack's own version string.
    * ``kind`` — which seam it plugs into (e.g. ``"driver"`` / ``"connector"``).
    * ``api_version`` — the seam's API version the pack targets, so core can
      refuse an incompatible pack rather than mis-build it.
    * ``capabilities`` — the narrow capabilities it declares it needs.
    * ``author`` — free-form provenance.
    """

    name: str
    version: str
    kind: str
    api_version: int
    capabilities: tuple[str, ...] = ()
    author: str = ""
