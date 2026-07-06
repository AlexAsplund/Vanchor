"""The narrow capability object a connector is built with — and the only seam
through which it can touch vanchor.

Every permission check lives here, so enforcement is real rather than decorative:
a connector subscribes, publishes and submits commands ONLY through
:class:`ConnectorContext`, and each of those is a default-deny allowlist check
against the connector's own :class:`~vanchor.connectors.base.ConnectorManifest`.

Safety invariants (the connector-framework floor):

* **Default-deny** — subscribe/publish are refused unless the topic is explicitly
  in the manifest (Global Constraint 1).
* **No control via publish** — a control topic (``motor.command`` / STOP / …) can
  never be published, EVEN if the manifest lists it in ``produces`` (Constraint 2).
* **STOP always works** — ``submit_command({"type": "stop"})`` is forwarded from
  any connector, granted or not (Constraint 3).
* **Control is a capability** — any non-STOP command is refused unless the manifest
  carries the ``control`` grant.

The context deliberately NEVER holds the Runtime, motor or governor; the command
sink is a narrow injected callable the Runtime wires in (Task 2).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Callable

from ..core import events

if TYPE_CHECKING:  # avoid a runtime import cycle (base imports this module)
    from .base import ConnectorManifest

logger = logging.getLogger("vanchor.connectors")

# The ONLY topics a connector may publish INTO vanchor, even with ``produces``
# (Global Constraint 6). These are the sensor/ingress input seams — never a
# control or telemetry-output topic.
INGRESS_TOPICS = frozenset({"nmea.in", "gps.fix_in", "imu.in"})

# Control/actuation topics that can NEVER be published through the context, even
# if a connector lists one in its own ``produces`` (Global Constraint 2). A
# connector reaches the motor only via the governed :meth:`submit_command`.
_CONTROL_TOPICS = frozenset(
    {"command", "commands", "stop", "estop", "e_stop", events.MOTOR_COMMAND}
)

# Throttle window (seconds) for denial warnings, so a misbehaving connector in a
# tight loop cannot flood the log.
_DENY_LOG_INTERVAL = 5.0

CommandSink = Callable[[dict], Any]


class ConnectorContext:
    """The single, narrow seam a connector uses to interact with vanchor.

    Built by the Runtime for each armed connector and passed to
    :meth:`~vanchor.connectors.base.Connector.start`. Holds only a bus, the
    connector's manifest, an injected command sink and a monotonic clock — never
    the Runtime/motor/governor.
    """

    def __init__(
        self,
        bus: events.EventBus,
        manifest: "ConnectorManifest",
        command_sink: CommandSink,
        mono_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._manifest = manifest
        self._command_sink = command_sink
        self._mono = mono_fn
        self._last_deny_log: dict[str, float] = {}

    # --- denial logging ---------------------------------------------------- #
    def _deny(self, key: str, msg: str, *args: Any) -> None:
        """Log a throttled denial warning (logger ``vanchor.connectors``)."""
        now = self._mono()
        last = self._last_deny_log.get(key)
        if last is None or (now - last) >= _DENY_LOG_INTERVAL:
            self._last_deny_log[key] = now
            logger.warning("connector %s: " + msg, self._manifest.name, *args)

    # --- read OUT of vanchor (consumes allowlist) -------------------------- #
    def subscribe(self, topic: str, handler: events.Handler) -> None:
        """Subscribe ``handler`` to ``topic``. Refused (``PermissionError``) unless
        ``topic`` is in the manifest's ``consumes``."""
        if topic not in self._manifest.consumes:
            self._deny(
                f"sub:{topic}",
                "denied subscribe to %r (not in consumes %r)",
                topic,
                self._manifest.consumes,
            )
            raise PermissionError(
                f"connector {self._manifest.name!r} may not subscribe to {topic!r}"
            )
        self._bus.subscribe(topic, handler)

    # --- inject INTO vanchor (produces ∩ ingress; never control) ----------- #
    async def publish(self, topic: str, payload: Any) -> None:
        """Publish ``payload`` onto an ingress ``topic``. Refused unless ``topic``
        is BOTH in the manifest's ``produces`` AND in :data:`INGRESS_TOPICS`. A
        control topic is ALWAYS refused, even if listed in ``produces``
        (Global Constraint 2)."""
        if topic in _CONTROL_TOPICS:
            self._deny(
                f"pub:{topic}",
                "denied publish to control topic %r (control topics can never be "
                "granted via produces)",
                topic,
            )
            raise PermissionError(
                f"connector {self._manifest.name!r} may never publish control topic {topic!r}"
            )
        if topic not in self._manifest.produces or topic not in INGRESS_TOPICS:
            self._deny(
                f"pub:{topic}",
                "denied publish to %r (not in produces ∩ ingress allowlist)",
                topic,
            )
            raise PermissionError(
                f"connector {self._manifest.name!r} may not publish to {topic!r}"
            )
        await self._bus.publish(topic, payload)

    # --- reach the motor (governed capability; STOP always works) ---------- #
    def submit_command(self, cmd: dict) -> None:
        """Submit a command to the governed command path.

        ``{"type": "stop"}`` is ALWAYS forwarded (Global Constraint 3). Any other
        command is refused (``PermissionError``) unless the manifest carries the
        ``control`` grant. Forwarding calls the injected command sink (the Runtime
        wires it to its validated ``handle_command`` in Task 2)."""
        if cmd.get("type") == "stop":
            self._command_sink(cmd)
            return
        if not self._manifest.control:
            self._deny(
                "cmd",
                "denied submit_command %r (no control grant)",
                cmd.get("type"),
            )
            raise PermissionError(
                f"connector {self._manifest.name!r} may not submit command "
                f"{cmd.get('type')!r} (no control grant)"
            )
        self._command_sink(cmd)
