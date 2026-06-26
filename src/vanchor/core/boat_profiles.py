"""Named boat profiles: persisted, selectable sets of :class:`BoatConfig` specs.

A *profile* is one named bundle of the editable boat specs (the same fields the
Init-boat wizard edits -- length, mass, thrust, steering geometry, sonar cone,
etc.). The :class:`BoatProfileStore` keeps several of them on disk so a helmsman
can switch between, say, a light kayak and a heavier skiff, and have the live
physics + telemetry follow the selection.

The store is a thin, deterministic, file-backed thing: it owns the JSON at
``<data_dir>/boats.json`` of the shape::

    {
        "active_id": "<id>",
        "profiles": {
            "<id>": {"name": "<str>", "specs": { ...BoatConfig fields... }},
            ...
        }
    }

On first run (no file) it seeds a small set of ready-to-pick presets (#89) --
bow/stern trolling motors, an off-centre bow motor, and a 15 HP stern outboard
-- with the bow trolling motor marked active. If handed an explicit
:class:`BoatConfig` ``seed`` instead, it falls back to a single ``"default"``
profile built from it. Either way it never clobbers an existing ``boats.json``.

IDs are derived deterministically from the profile name (a slug) plus an
incrementing counter on collision -- never from the wall clock -- so tests are
fully reproducible. The store knows nothing about the live runtime; applying a
profile to the simulator is the Runtime's job.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, fields
from typing import Any

from .config import BoatConfig

logger = logging.getLogger("vanchor.boat_profiles")

# The editable spec fields, taken straight off BoatConfig so the two never drift.
_SPEC_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(BoatConfig))


def _slug(name: str) -> str:
    """A filesystem/url-safe slug derived from a profile name.

    Lower-cases, replaces any run of non-alphanumerics with a single dash and
    trims leading/trailing dashes. Empty/garbage names fall back to ``"boat"``.
    """
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "boat"


def _clean_specs(specs: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only known BoatConfig fields from a (possibly partial/dirty) dict,
    coercing numeric fields to the declared type. Missing fields fall back to
    the BoatConfig default, so every stored profile is always complete."""
    base = BoatConfig()
    out: dict[str, Any] = {}
    src = specs or {}
    for f in fields(BoatConfig):
        if f.name in src and src[f.name] is not None:
            val = src[f.name]
            default = getattr(base, f.name)
            if isinstance(default, bool):
                out[f.name] = bool(val)
            elif isinstance(default, int) and not isinstance(default, bool):
                out[f.name] = int(val)
            elif isinstance(default, float):
                out[f.name] = float(val)
            else:
                out[f.name] = val
        else:
            out[f.name] = getattr(base, f.name)
    return out


def specs_from_boat(boat: BoatConfig) -> dict[str, Any]:
    """Snapshot a :class:`BoatConfig` into a plain specs dict."""
    return {f.name: getattr(boat, f.name) for f in fields(BoatConfig)}


# Ready-to-pick starter presets seeded on first run (#89). Each is a full
# BoatConfig built from the defaults with a few fields overridden so the physics
# + steering differ realistically. The first entry is the active default. The
# bow trolling motor mirrors the BoatConfig defaults so a fresh install behaves
# exactly as before; the others flip the mount, add a lateral offset, or model a
# faster outboard. ``_clean_specs`` fills any field not named here from defaults.
_PRESETS: tuple[tuple[str, dict[str, Any]], ...] = (
    # The current default boat: bow-mounted ~55 lbf trolling motor. ACTIVE.
    # Default hull tracking (1.0) keeps a fresh install byte-identical.
    (
        "Bow trolling motor",
        {
            "length_m": 4.1,
            "mass_kg": 300.0,
            "max_speed_mps": 1.6,
            "max_thrust_n": 250.0,
            "thruster_mount": "bow",
            "thruster_y_m": 0.0,
            "hull_tracking": 1.0,
        },
    ),
    # Same boat, motor on the transom: the helm steer-sign + physics yaw flip.
    (
        "Stern trolling motor",
        {
            "length_m": 4.1,
            "mass_kg": 300.0,
            "max_speed_mps": 1.6,
            "max_thrust_n": 250.0,
            "thruster_mount": "stern",
            "thruster_y_m": 0.0,
            "hull_tracking": 1.0,
        },
    ),
    # Bow motor clamped to a transom corner: a lateral offset for the thrust-yaw
    # feed-forward to cancel.
    (
        "Off-centre bow trolling",
        {
            "length_m": 4.1,
            "mass_kg": 300.0,
            "max_speed_mps": 1.6,
            "max_thrust_n": 250.0,
            "thruster_mount": "bow",
            "thruster_y_m": 0.35,
            "hull_tracking": 1.0,
        },
    ),
    # A flat-bottom jon boat: short, light, beamy -> skittish and loose. Low
    # hull tracking makes it turn snappily and slide/leeway easily.
    (
        "Jon boat (flat-bottom)",
        {
            "length_m": 3.7,
            "beam_m": 1.5,
            "mass_kg": 180.0,
            "max_speed_mps": 1.6,
            "max_thrust_n": 250.0,
            "thruster_mount": "bow",
            "thruster_y_m": 0.0,
            "hull_tracking": 0.35,
        },
    ),
    # A regular ~15 HP stern outboard: much more thrust + speed, heavier hull.
    # A planing-ish deep-V hull tracks more, so a touch above default.
    (
        "15 HP stern outboard",
        {
            "length_m": 4.5,
            "mass_kg": 450.0,
            "max_speed_mps": 7.0,  # ~14 knots
            "max_thrust_n": 700.0,
            "thruster_mount": "stern",
            "thruster_y_m": 0.0,
            "hull_tracking": 1.6,
        },
    ),
)


class BoatProfileStore:
    """Persistent set of named boat profiles with an active selection."""

    def __init__(self, data_dir: str, seed: BoatConfig | None = None) -> None:
        self._dir = data_dir
        self._path = os.path.join(data_dir, "boats.json")
        self._active_id: str = ""
        self._profiles: dict[str, dict[str, Any]] = {}
        # A monotonically-incrementing counter used only to disambiguate
        # colliding slugs -- never the wall clock, so IDs are reproducible.
        self._counter: int = 0
        # Only seed when no file exists -- never clobber a user's saved profiles.
        # With an explicit seed (e.g. a custom boat config) we keep the legacy
        # single-"default" behaviour; otherwise we seed the starter presets (#89).
        if not self._load():
            if seed is not None:
                self._seed(seed)
            else:
                self._seed_presets()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load(self) -> bool:
        """Load from disk. Returns True if a usable file was read."""
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        profiles = data.get("profiles") if isinstance(data, dict) else None
        if not isinstance(profiles, dict) or not profiles:
            return False
        clean: dict[str, dict[str, Any]] = {}
        for pid, prof in profiles.items():
            if not isinstance(prof, dict):
                continue
            clean[str(pid)] = {
                "name": str(prof.get("name", pid)),
                "specs": _clean_specs(prof.get("specs")),
            }
        if not clean:
            return False
        self._profiles = clean
        active = data.get("active_id")
        self._active_id = active if active in clean else next(iter(clean))
        return True

    def _save(self) -> None:
        os.makedirs(self._dir, exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {"active_id": self._active_id, "profiles": self._profiles},
                fh,
                indent=2,
            )
        os.replace(tmp, self._path)

    def _seed(self, boat: BoatConfig) -> None:
        """Create the initial ``default`` profile from the given boat config."""
        self._profiles = {
            "default": {"name": "Default", "specs": specs_from_boat(boat)}
        }
        self._active_id = "default"
        self._save()
        logger.info("seeded boat profile store with the default profile")

    def _seed_presets(self) -> None:
        """Seed the ready-to-pick starter presets (#89), with the bow trolling
        motor as the active default. Only called on first run (no file)."""
        self._profiles = {}
        for name, overrides in _PRESETS:
            pid = self._new_id(name)
            self._profiles[pid] = {"name": name, "specs": _clean_specs(overrides)}
        # The first preset (bow trolling motor) is the active default.
        self._active_id = next(iter(self._profiles))
        self._save()
        logger.info("seeded boat profile store with %d presets", len(self._profiles))

    # ------------------------------------------------------------------ #
    # Ids
    # ------------------------------------------------------------------ #
    def _new_id(self, name: str) -> str:
        base = _slug(name)
        candidate = base
        while candidate in self._profiles:
            self._counter += 1
            candidate = f"{base}-{self._counter}"
        return candidate

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def list(self) -> list[dict[str, Any]]:
        """All profiles as ``[{id, name, ...specs}, ...]`` (insertion order)."""
        return [self._flat(pid) for pid in self._profiles]

    def get(self, profile_id: str) -> dict[str, Any] | None:
        """One profile as ``{id, name, specs:{...}}`` (None if unknown)."""
        prof = self._profiles.get(profile_id)
        if prof is None:
            return None
        return {
            "id": profile_id,
            "name": prof["name"],
            "specs": dict(prof["specs"]),
        }

    def active(self) -> dict[str, Any] | None:
        return self.get(self._active_id)

    @property
    def active_id(self) -> str:
        return self._active_id

    def _flat(self, profile_id: str) -> dict[str, Any]:
        """Profile flattened to ``{id, name, ...specs}`` for the list/REST shape."""
        prof = self._profiles[profile_id]
        return {"id": profile_id, "name": prof["name"], **prof["specs"]}

    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #
    def create(self, name: str, specs: dict[str, Any] | None = None) -> str:
        """Create a new profile; returns its id. Specs default to BoatConfig
        defaults for any field not provided."""
        pid = self._new_id(name)
        self._profiles[pid] = {
            "name": str(name) or pid,
            "specs": _clean_specs(specs),
        }
        self._save()
        logger.info("created boat profile %r (%s)", name, pid)
        return pid

    def save(self, profile_id: str, name: str | None, specs: dict[str, Any] | None) -> bool:
        """Update an existing profile's name and/or specs. Returns False if the
        id is unknown. Specs are merged onto the existing ones (partial update)."""
        prof = self._profiles.get(profile_id)
        if prof is None:
            return False
        if name is not None:
            prof["name"] = str(name)
        if specs is not None:
            merged = dict(prof["specs"])
            for f in _SPEC_FIELDS:
                if f in specs and specs[f] is not None:
                    merged[f] = specs[f]
            prof["specs"] = _clean_specs(merged)
        self._save()
        return True

    def delete(self, profile_id: str) -> bool:
        """Delete a profile. Refuses to delete the last remaining one (returns
        False). If the active profile is deleted, the active selection falls
        back to the first remaining profile."""
        if profile_id not in self._profiles or len(self._profiles) <= 1:
            return False
        del self._profiles[profile_id]
        if self._active_id == profile_id:
            self._active_id = next(iter(self._profiles))
        self._save()
        logger.info("deleted boat profile %s", profile_id)
        return True

    def set_active(self, profile_id: str) -> bool:
        """Mark a profile active. Returns False if the id is unknown."""
        if profile_id not in self._profiles:
            return False
        self._active_id = profile_id
        self._save()
        return True

    def to_dict(self) -> dict[str, Any]:
        """REST shape: ``{active_id, profiles:[{id,name,...specs}, ...]}``."""
        return {"active_id": self._active_id, "profiles": self.list()}
