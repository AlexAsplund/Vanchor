"""Registry of pluggable connectors + the user-consent (grants) store.

Adding a connector is not an ``app.py`` edit: a module in this package (or a
pip-installed pack) calls :func:`register_connector` at import and becomes a
buildable, consent-gated integration the Runtime can list and start.

Registration mirrors ``hardware/registry.register_context_driver`` (idempotent,
keyed by name, version-mismatch warns-but-registers). Consent is persisted
separately in ``<data_dir>/connectors.json`` as
``{name: {"enabled": bool, "manifest_hash": str, "settings": {...}}}`` — a
connector is *armed* only when enabled AND the stored hash still matches the
current manifest (else it needs re-consent, Global Constraint 5).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .base import Connector, ConnectorManifest, manifest_hash

logger = logging.getLogger("vanchor.connectors")

# Version of the connector build/capability API. Bumped only on a breaking change
# to the ``build(settings)`` contract or :class:`ConnectorContext`, so a pack can
# declare the version it targets and a mismatch is visible in the logs.
CONNECTOR_API_VERSION = 1

# On-disk consent store filename under ``data_dir``.
GRANTS_FILE = "connectors.json"

BuildFn = Callable[[dict], Connector]  # (settings) -> Connector


@dataclass(frozen=True)
class ConnectorSpec:
    """A registered connector: its name, its ``build(settings)`` factory, the UI
    label, and the connector-API version it targets."""

    name: str
    build: BuildFn
    label: str = ""
    api_version: int = CONNECTOR_API_VERSION


_REGISTRY: dict[str, ConnectorSpec] = {}


def register_connector(
    name: str,
    build: BuildFn,
    *,
    api_version: int = CONNECTOR_API_VERSION,
    label: str = "",
) -> None:
    """Register a connector under ``name`` with a ``build(settings) -> Connector``
    factory. Idempotent (re-registering replaces the spec). A version mismatch is
    logged but still registered (mirror ``register_context_driver``)."""
    if api_version != CONNECTOR_API_VERSION:
        logger.warning(
            "connector %r targets connector-API v%s but core is v%s; registering anyway",
            name,
            api_version,
            CONNECTOR_API_VERSION,
        )
    _REGISTRY[name] = ConnectorSpec(
        name=name, build=build, label=label, api_version=api_version
    )


def names() -> list[str]:
    """Registered connector names (stable/sorted)."""
    return sorted(_REGISTRY)


def has(name: str) -> bool:
    return name in _REGISTRY


def spec(name: str) -> ConnectorSpec | None:
    """The registered :class:`ConnectorSpec` for ``name`` or ``None``."""
    return _REGISTRY.get(name)


def build(name: str, settings: dict) -> Connector:
    """Build the connector registered as ``name`` from ``settings``. Raises
    ``KeyError`` for an unknown name."""
    return _REGISTRY[name].build(settings)


# --------------------------------------------------------------------------- #
# Consent / grants store (<data_dir>/connectors.json)
# --------------------------------------------------------------------------- #
def load_grants(data_dir: str | Path) -> dict[str, Any]:
    """Read persisted consent from ``<data_dir>/connectors.json``.

    Returns ``{name: {"enabled": bool, "manifest_hash": str, "settings": {...}}}``.
    A missing, unreadable, or non-mapping file returns ``{}`` so startup falls
    back to default-deny (mirror ``load_device_overrides`` tolerance)."""
    p = Path(data_dir) / GRANTS_FILE
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("ignoring unreadable %s: %s", p, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("ignoring %s: not a mapping", p)
        return {}
    return data


def save_grants(data_dir: str | Path, grants: dict[str, Any]) -> None:
    """Persist ``grants`` to ``<data_dir>/connectors.json`` (creating the dir)."""
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / GRANTS_FILE).write_text(json.dumps(grants, indent=2), encoding="utf-8")


def armed(name: str, manifest: ConnectorManifest, grants: dict[str, Any]) -> bool:
    """True when the connector is enabled AND the stored consent hash still matches
    the current manifest (i.e. the user consented to exactly these permissions)."""
    g = grants.get(name)
    if not g or not g.get("enabled"):
        return False
    return g.get("manifest_hash") == manifest_hash(manifest)


def needs_reconsent(
    name: str, manifest: ConnectorManifest, grants: dict[str, Any]
) -> bool:
    """True when the connector is enabled but its manifest changed since consent —
    it stays disarmed until the user re-approves (Global Constraint 5)."""
    g = grants.get(name)
    if not g or not g.get("enabled"):
        return False
    return g.get("manifest_hash") != manifest_hash(manifest)
