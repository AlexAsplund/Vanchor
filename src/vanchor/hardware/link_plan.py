"""Pure link-resolution planner for the two motor channels.

The construction decision — *do the steering and thrust channels resolve to ONE
combined controller, or to two independent devices?* — is factored out here as a
pure function so it is unit-testable in isolation and can never diverge between the
runtime build (Task 2) and the tests.

Resolution is by PHYSICAL LINK, not by source string (Constraint 2): two channels
that land on the same endpoint (the default rig: both on one Arduino) MUST become
one combined device on one transport. The same serial port with conflicting framing
is a validation ERROR, never a silent pick.

Every LEGACY config — channel keys unset, any ``enabled``/``motor_source`` combo
including sim, serial, both, none — resolves to ``combined`` with the resolved link
equal to today's motor link field-for-field (Constraint 3), so Task 2 reproduces
today's object graph exactly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..core.config import HardwareConfig

# The framing keys that must match when two channels share a serial port.
_FRAMING_KEYS = ("baud", "bytesize", "parity", "stopbits")


def _normalize_port(port: str) -> str:
    """Resolve symlinks for Unix device paths so ``/dev/ttyUSB2`` and its
    ``/dev/serial/by-id/…`` alias compare as equal.

    Non-Unix paths (e.g. ``COM3`` on Windows) are returned unchanged.
    """
    if port.startswith("/"):
        try:
            return os.path.realpath(port)
        except OSError:
            pass
    return port


@dataclass(frozen=True)
class LinkPlan:
    """The resolved motor construction decision.

    ``kind == "combined"``: both channels are served by ONE device.
      * ``source`` is the combined source ("sim" | "serial" | "both" | "none").
      * ``link`` is the resolved ``{source, port, baud, bytesize, parity,
        stopbits}`` for the active endpoint (Task 2 builds a serial controller from
        it when ``source == "serial"``/``"both"``).
      * ``tee`` is True when ``source == "both"`` (drive the sim boat AND mirror to
        the serial controller — today's ``_TeeMotor`` semantics).
      * ``neutral_channel`` names the side ("steering"/"thrust") that resolved to
        "none" while the other is active: the combined frame still carries both
        fields, so the disabled channel is sent a neutral 0.

    ``kind == "split"``: two independent devices.
      * ``steering`` / ``thrust`` are the per-channel resolved link dicts (either
        may have ``source == "none"`` -> that channel is not built).
    """

    kind: str
    source: str | None = None
    link: dict | None = None
    tee: bool = False
    neutral_channel: str | None = None
    steering: dict | None = None
    thrust: dict | None = None


def _has_serial(link: dict) -> bool:
    """Whether a resolved channel link opens a serial endpoint."""
    return link["source"] in ("serial", "both")


def _framing(link: dict) -> tuple:
    return tuple(link[k] for k in _FRAMING_KEYS)


def _same_endpoint(a: dict, b: dict) -> bool:
    """Whether two links resolve to the SAME physical device.

    Same source, and — for serial-backed sources — the same port after
    resolving any symlinks (``/dev/serial/by-id/…`` and ``/dev/ttyUSB2``
    pointing to the same device must not be opened twice). Framing equality
    on a shared port is enforced separately (a mismatch is an error, not a "no").
    """
    if a["source"] != b["source"]:
        return False
    if _has_serial(a):
        return _normalize_port(a["port"]) == _normalize_port(b["port"])
    return True  # sim==sim, none==none share the one (sim / null) device


def plan_motor_links(hw: HardwareConfig) -> LinkPlan:
    """Resolve the two motor channels into a :class:`LinkPlan` (pure).

    Raises ``ValueError`` when both channels resolve to the SAME serial port but
    with mismatched framing — they would collapse to one combined controller, so
    the framing must agree.

    Ports are normalised via ``os.path.realpath`` before comparison so that
    ``/dev/ttyUSB2`` and a ``/dev/serial/by-id/…`` symlink to the same device
    resolve as the same endpoint (Constraint 2 deferred-review note).
    """
    steering = hw.channel_link("steering")
    thrust = hw.channel_link("thrust")

    # Safety guard (Constraint 2): never open the same serial port twice with
    # conflicting framing. If both channels touch a serial endpoint on the same
    # (normalised) port, their framing MUST match.
    if (
        _has_serial(steering)
        and _has_serial(thrust)
        and _normalize_port(steering["port"]) == _normalize_port(thrust["port"])
        and _framing(steering) != _framing(thrust)
    ):
        raise ValueError(
            f"steering and thrust channels share a port "
            f"({_normalize_port(steering['port'])!r}); "
            f"framing must match (they resolve to the combined controller): "
            f"steering={_framing(steering)} vs thrust={_framing(thrust)}"
        )

    s_src, t_src = steering["source"], thrust["source"]

    # Both disabled -> one NullMotor (combined "none").
    if s_src == "none" and t_src == "none":
        return LinkPlan(kind="combined", source="none", link=thrust)

    # Exactly one disabled -> the active side's single device carries both fields;
    # the disabled side is sent neutral 0 (Task 2/3 consume neutral_channel).
    if s_src == "none":
        return LinkPlan(kind="combined", source=t_src, link=thrust,
                        tee=(t_src == "both"), neutral_channel="steering")
    if t_src == "none":
        return LinkPlan(kind="combined", source=s_src, link=steering,
                        tee=(s_src == "both"), neutral_channel="thrust")

    # Both active and on the same physical endpoint -> one combined device. This is
    # the path EVERY legacy config takes (channels resolve identically).
    if _same_endpoint(steering, thrust):
        return LinkPlan(kind="combined", source=s_src, link=steering,
                        tee=(s_src == "both"))

    # Otherwise two genuinely independent devices.
    return LinkPlan(kind="split", steering=steering, thrust=thrust)
