"""Observability helpers that sit *beside* the control loop, never inside it.

Currently this package holds the always-on **black-box flight recorder** (roadmap
#20): a lightweight, bounded ring buffer that samples a low-rate snapshot of the
autopilot each control tick and, on any alarm transition, dumps its pre-trigger
history (plus a short post-trigger tail) to a timestamped file off the event
loop. It is deliberately decoupled from the controller/state so it can never slow
or perturb the hot path -- it only observes.
"""

from __future__ import annotations

from .blackbox import BlackBox

__all__ = ["BlackBox"]
