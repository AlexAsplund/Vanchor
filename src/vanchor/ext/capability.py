"""The narrow capability a plug-in receives instead of the ``Runtime``.

:class:`Capability` is the marker base for the narrow object handed to a plug-in
— the whole point of the safety floor is that a pack talks only to a capability,
**never** the ``Runtime``, the motor, or the governor. The rich verbs (event
bus, scheduler, config, routes, telemetry, ui, alarms — see
``docs/extensibility.md``) land on subclasses as the seams are built; this base
is deliberately minimal for now.
"""

from __future__ import annotations


class Capability:
    """Marker base for the narrow object a plug-in is given.

    A plug-in only ever talks to a capability, so core can evolve underneath it
    and a rogue/hung plug-in degrades safely — it never holds the ``Runtime``,
    the motor, or the governor. Concrete verbs (bus, scheduler, config, routes,
    telemetry, ui, alarms) are added by subclasses per seam.
    """
