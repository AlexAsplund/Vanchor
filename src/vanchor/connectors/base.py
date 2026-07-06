"""Connector base types: the permission manifest and the :class:`Connector` ABC.

A *connector* is a pluggable bridge between the vanchor event bus and an external
system (NMEA-TCP, metrics export, NMEA 2000, an RF remote). Unlike a device driver
(one transducer -> one signal), a connector is a multiplexed, often bidirectional
bridge, so its trust model is an explicit **permission manifest** + **default-deny**
+ **user consent** (mirroring the #43 ``DriverContext`` pattern).

The manifest is the whole permission surface: what bus topics the connector may
read (``consumes``), what ingress topics it may inject onto (``produces``), and
whether it requests the governed motor-control capability (``control``). Nothing
outside the manifest is reachable — enforcement lives in
:class:`vanchor.connectors.context.ConnectorContext`.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from .context import ConnectorContext


@dataclass(frozen=True)
class ConnectorManifest:
    """The declared, immutable permission surface of a connector.

    * ``consumes`` — bus topics the connector may :meth:`subscribe` to, i.e. read
      OUT of vanchor (e.g. ``telemetry``, ``nmea.out``).
    * ``produces`` — ingress topics the connector may :meth:`publish` INTO vanchor.
      Only ever effective when intersected with the ingress allowlist
      (:data:`vanchor.connectors.context.INGRESS_TOPICS`); a control topic can
      never be granted here (Global Constraint 2).
    * ``control`` — requests the governed ``submit_command`` capability (reaching
      the motor via the same validated path the app uses). Default-deny: ``False``.
    * ``grant_lines`` — plain-language lines shown to the user at the consent
      prompt (e.g. "Read live telemetry").

    Frozen + hashable so a persisted grant can pin the exact manifest the user
    consented to (see :func:`manifest_hash`); any field change re-triggers consent.
    """

    name: str
    label: str
    description: str
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    control: bool = False
    grant_lines: tuple[str, ...] = field(default_factory=tuple)


def manifest_hash(m: ConnectorManifest) -> str:
    """A short, deterministic fingerprint of ``m`` (sha256 of canonical JSON, first
    16 hex chars).

    Canonical = every field, JSON with sorted keys, so the hash is stable across
    runs and changes when ANY field changes. A persisted grant stores this hash;
    on the next boot a mismatch means the connector's requested permissions
    changed and the user must re-consent (Global Constraint 5).
    """
    payload = {
        "name": m.name,
        "label": m.label,
        "description": m.description,
        "consumes": list(m.consumes),
        "produces": list(m.produces),
        "control": bool(m.control),
        "grant_lines": list(m.grant_lines),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class Connector(ABC):
    """The base class every connector implements.

    A connector touches nothing but the :class:`ConnectorContext` it is handed in
    :meth:`start` — never the Runtime, motor or governor. Lifecycle mirrors the
    device contract: ``start(ctx)`` / ``stop()`` are async; ``status()`` and
    ``debug()`` are cheap introspection that MUST NOT raise.
    """

    #: The connector's permission manifest. Concrete connectors set this as a class
    #: attribute or override it as a property.
    manifest: ConnectorManifest

    #: Editable configuration schema for this connector.
    #:
    #: A list of field descriptors that declare which ``settings`` keys are
    #: user-editable via the API and the Connectors UI.  Each dict has:
    #:
    #: * ``key``         – the settings dict key (required)
    #: * ``label``       – human-readable label (required)
    #: * ``type``        – ``"str"``, ``"int"``, ``"float"``, or ``"bool"`` (required)
    #: * ``default``     – default value when the key is absent from the store
    #: * ``placeholder`` – optional input placeholder text (str only)
    #: * ``hint``        – optional one-liner shown below the field
    #: * ``secret``      – if ``True``, the stored value is masked as ``"•••"`` in API
    #:                     responses; it is still stored as **plain text** in
    #:                     ``connectors.json`` (note this in the hint)
    #:
    #: Connectors with no configurable settings leave this as the empty list (the
    #: default). Subclasses override it as a class-level list attribute.
    settings_schema: list = []

    @abstractmethod
    async def start(self, ctx: ConnectorContext) -> None:
        """Begin bridging, using ``ctx`` as the ONLY seam to vanchor."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop bridging and release any external resources."""

    def status(self) -> dict:
        """A small JSON-able status dict for the UI/telemetry. Default empty."""
        return {}

    def debug(self) -> str:
        """A human-readable multi-line debug string. MUST never raise (same
        contract as device ``debug()``); the default is a safe placeholder."""
        return f"{type(self).__name__}: no debug data"
