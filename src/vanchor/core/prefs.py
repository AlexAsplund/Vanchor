"""Server-persisted safety geometry + a generic UI-preferences KV store.

The browser is a CACHE, not the source of truth. Two small, deterministic,
file-backed stores live here:

* :class:`SafetyGeometryStore` -- the persistence layer for the operator's
  *safety geometry*: no-go polygons, the shallow-water min-depth, and the
  loss-of-fix failsafe switch. The live authority is still the
  :class:`~vanchor.controller.safety.SafetyGovernor`; this store just mirrors
  what the operator set so it SURVIVES A RESTART with no client connected. The
  Runtime loads it on init and applies it to the governor, and updates it
  whenever a ``set_nogo_zones`` / ``set_min_depth`` / ``set_fix_failsafe``
  command lands.

* :class:`PrefsStore` -- a generic string-keyed JSON blob for UI preferences
  (HUD layout, basemap choice, ...). ``GET /api/prefs`` reads it; ``PUT
  /api/prefs`` merges a patch into it. This is the "browser as cache" mechanism
  for any UI pref the client wants durable across devices/reinstalls.

Both use the same atomic ``tmp + os.replace`` write as the other stores
(boats.json, devices.json) so a crash mid-write can never leave a half-written
file.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("vanchor.prefs")


def _atomic_write_json(path: str, data: Any) -> None:
    """Write ``data`` as JSON to ``path`` atomically (tmp file + os.replace).

    The replace is atomic on POSIX, so a reader (or a crash) never sees a
    partially-written file -- it sees either the old file or the new one."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _clean_zones(zones: Any) -> list[list[list[float]]]:
    """Coerce a raw zones payload into a list of rings ``[[[lat,lon],...],...]``.

    Rings with fewer than 3 points, or non-numeric vertices, are dropped -- the
    same degenerate-ring rule the governor applies -- so the stored geometry is
    always renderable + applyable."""
    out: list[list[list[float]]] = []
    if not isinstance(zones, (list, tuple)):
        return out
    for ring in zones:
        if not isinstance(ring, (list, tuple)) or len(ring) < 3:
            continue
        pts: list[list[float]] = []
        ok = True
        for p in ring:
            if not isinstance(p, (list, tuple)) or len(p) < 2:
                ok = False
                break
            try:
                pts.append([float(p[0]), float(p[1])])
            except (TypeError, ValueError):
                ok = False
                break
        if ok and len(pts) >= 3:
            out.append(pts)
    return out


class SafetyGeometryStore:
    """Persistent mirror of the operator's safety geometry.

    Holds ``{nogo_zones, min_depth_m, fix_failsafe_enabled}`` at
    ``<data_dir>/safety.json``. ``min_depth_m`` / ``fix_failsafe_enabled`` are
    ``None`` until the operator has ever set them, so a fresh install applies
    nothing (and the config defaults stand) -- we only override the governor
    with values the operator actually chose.
    """

    def __init__(self, data_dir: str) -> None:
        self._dir = data_dir
        self._path = os.path.join(data_dir, "safety.json")
        self.nogo_zones: list[list[list[float]]] = []
        self.min_depth_m: float | None = None
        self.fix_failsafe_enabled: bool | None = None
        self.auto_follow_apb: bool | None = None
        self._load()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self.nogo_zones = _clean_zones(data.get("nogo_zones"))
        md = data.get("min_depth_m")
        if isinstance(md, (int, float)) and not isinstance(md, bool):
            self.min_depth_m = float(md)
        ff = data.get("fix_failsafe_enabled")
        if isinstance(ff, bool):
            self.fix_failsafe_enabled = ff
        aa = data.get("auto_follow_apb")
        if isinstance(aa, bool):
            self.auto_follow_apb = aa

    def _save(self) -> None:
        _atomic_write_json(self._path, self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "nogo_zones": self.nogo_zones,
            "min_depth_m": self.min_depth_m,
            "fix_failsafe_enabled": self.fix_failsafe_enabled,
            "auto_follow_apb": self.auto_follow_apb,
        }

    # ------------------------------------------------------------------ #
    # Mutations (each persists immediately)
    # ------------------------------------------------------------------ #
    def set_nogo_zones(self, zones: Any) -> None:
        self.nogo_zones = _clean_zones(zones)
        self._save()

    def set_min_depth(self, min_depth_m: float | None) -> None:
        self.min_depth_m = None if min_depth_m is None else float(min_depth_m)
        self._save()

    def set_fix_failsafe(self, enabled: bool) -> None:
        self.fix_failsafe_enabled = bool(enabled)
        self._save()

    def set_auto_follow_apb(self, enabled: bool) -> None:
        self.auto_follow_apb = bool(enabled)
        self._save()


class PrefsStore:
    """A generic, string-keyed JSON preferences blob at ``<data_dir>/prefs.json``.

    The "browser as cache" mechanism for UI preferences: the client renders from
    its own localStorage for instant paint, but the durable copy lives here so a
    reinstall / a different device sees the same layout. ``merge`` is a shallow
    top-level merge (a patch replaces whole top-level keys), which is enough for
    the flat pref maps the UI keeps.
    """

    def __init__(self, data_dir: str) -> None:
        self._dir = data_dir
        self._path = os.path.join(data_dir, "prefs.json")
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self._data = data

    def get(self) -> dict[str, Any]:
        """The full persisted prefs dict (a copy, so callers can't mutate it)."""
        return dict(self._data)

    def merge(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Shallow-merge ``patch`` into the stored prefs and persist atomically.
        Returns the merged dict. A non-dict patch is ignored (returns current)."""
        if not isinstance(patch, dict):
            return self.get()
        self._data.update(patch)
        _atomic_write_json(self._path, self._data)
        return self.get()
