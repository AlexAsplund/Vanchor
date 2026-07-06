"""Which control modes need which devices — the single source of truth for
"this mode is unavailable because <device> is not connected".

A device is *connected* when its configured source is anything other than
``"none"`` (i.e. simulated / serial / NMEA / a pack driver all count as present).
When a required device is set to "Not connected", the modes that depend on it are
disabled — in the UI (greyed out, with the reason) and in the controller (which
refuses to engage them, as a safety backstop against a stale/API command).

STOP and plain manual-stop are never gated: the safety floor is always reachable.
"""
from __future__ import annotations

from .models import ControlModeName

# Human labels for the device kinds, used to build the reason string.
DEVICE_LABEL = {
    "gps": "GPS",
    "compass": "Compass",
    "depth": "Depth sounder",
    "motor": "Motor",
}

# Every motor-commanding mode needs a motor. GPS is required by anything that
# navigates to/holds a position; heading-hold needs a heading source (compass);
# contour-follow additionally needs the depth sounder.
MODE_REQUIRES: dict[ControlModeName, tuple[str, ...]] = {
    ControlModeName.MANUAL: ("motor",),
    ControlModeName.ANCHOR_HOLD: ("motor", "gps"),
    ControlModeName.ANCHOR_ML: ("motor", "gps"),
    ControlModeName.ANCHOR_LEIF: ("motor", "gps"),
    ControlModeName.HEADING_HOLD: ("motor", "compass"),
    ControlModeName.WAYPOINT: ("motor", "gps"),
    ControlModeName.WORK_AREA: ("motor", "gps"),
    ControlModeName.FOLLOW_APB: ("motor", "gps"),
    ControlModeName.DRIFT: ("motor", "gps"),
    ControlModeName.CONTOUR_FOLLOW: ("motor", "gps", "depth"),
    ControlModeName.ORBIT: ("motor", "gps"),
    ControlModeName.TROLLING: ("motor", "gps"),
}


def missing_devices(mode: ControlModeName, connected: dict[str, bool]) -> list[str]:
    """The required device kinds for ``mode`` that are NOT connected.

    ``connected`` maps a device kind -> bool; a missing key is treated as
    connected (fail-open) so an unknown device never wrongly disables a mode.
    """
    return [d for d in MODE_REQUIRES.get(mode, ()) if not connected.get(d, True)]


def unavailable_reason(mode: ControlModeName, connected: dict[str, bool]) -> str | None:
    """A short reason a mode is unavailable, or ``None`` if it is available."""
    miss = missing_devices(mode, connected)
    if not miss:
        return None
    labels = [DEVICE_LABEL.get(d, d) for d in miss]
    return f"{' + '.join(labels)} not connected"


def mode_availability(connected: dict[str, bool]) -> dict[str, dict]:
    """``{mode_value: {"available": bool, "reason": str|None}}`` for every mode,
    keyed by the mode's string value (matches the UI's ``data-mode``)."""
    out: dict[str, dict] = {}
    for mode in MODE_REQUIRES:
        reason = unavailable_reason(mode, connected)
        out[mode.value] = {"available": reason is None, "reason": reason}
    return out
