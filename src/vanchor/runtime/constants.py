"""Module-level constants shared by the Runtime and its collaborators.

Split out of the old monolithic ``vanchor.app`` (issue #80) so collaborators can
import them without a cycle back through ``app`` / ``runtime.runtime``.
"""
from __future__ import annotations

from ..core.models import ControlModeName

# Modes that count as "underway / making way" for the lost-connection failsafe
# (#64): every guided behaviour except idle manual and station-keeping anchor.
_UNDERWAY_MODES = frozenset(
    {
        ControlModeName.HEADING_HOLD,
        ControlModeName.WAYPOINT,
        ControlModeName.FOLLOW_APB,
        ControlModeName.DRIFT,
        ControlModeName.CONTOUR_FOLLOW,
        ControlModeName.ORBIT,
        ControlModeName.TROLLING,
        ControlModeName.WORK_AREA,
    }
)

# In MANUAL, |commanded thrust| above this counts as "driving" (making way) for
# the lost-connection failsafe (#64) -- below it the boat is effectively idle.
_MANUAL_UNDERWAY_THRUST_EPS = 0.02

# Environment fields persisted across restarts (environment.json): the base
# weather the Simulator panel sets. Derived live values (wind_gust_now) and
# tuning constants (gust_tau_s) stay out.
_ENV_PERSIST_KEYS = (
    "current_speed", "current_dir", "wind_speed", "wind_dir",
    "gust_amplitude_mps", "wind_variability", "current_variability",
)

__all__ = [
    "_UNDERWAY_MODES",
    "_MANUAL_UNDERWAY_THRUST_EPS",
    "_ENV_PERSIST_KEYS",
]
