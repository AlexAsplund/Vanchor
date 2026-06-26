"""A small library of reproducible, named scenarios for analysis and tuning.

Add one by appending to :data:`SCENARIOS`. Each is a plain :class:`Scenario`,
so a tuning experiment is just a copy with one field changed.
"""

from __future__ import annotations

from ..core.geo import destination_point
from ..core.models import Environment, GeoPoint
from .runner import Command, Scenario

START = GeoPoint(59.66275, 13.32247)  # Lake Vänern default


def _anchor_displaced(name: str, *, model: str, radius: float, displace_m: float) -> Scenario:
    """Drop an anchor, then shove the boat ``displace_m`` away and watch it
    recover -- the canonical overshoot/settling test."""
    away = destination_point(START, displace_m, 45.0)
    return Scenario(
        name=name,
        start=START,
        model=model,
        duration_s=120.0,
        environment=Environment(),
        commands=[
            Command(2.0, {"type": "anchor_hold", "radius_m": radius}),
            Command(3.0, {"type": "teleport", "lat": away.lat, "lon": away.lon}),
        ],
    )


def _anchor_drift(name: str, *, model: str) -> Scenario:
    return Scenario(
        name=name,
        start=START,
        model=model,
        duration_s=200.0,
        environment=Environment(
            current_speed=0.3, current_dir=90.0, wind_speed=5.0, wind_dir=120.0
        ),
        commands=[Command(2.0, {"type": "anchor_hold", "radius_m": 5.0})],
    )


def _heading_step(name: str, *, model: str, target: float = 90.0) -> Scenario:
    return Scenario(
        name=name,
        start=START,
        model=model,
        duration_s=60.0,
        environment=Environment(),
        commands=[
            Command(2.0, {"type": "heading_hold", "heading": target, "throttle": 0.5})
        ],
    )


def _waypoint_box(name: str, *, model: str) -> Scenario:
    a = destination_point(START, 60.0, 45.0)
    b = destination_point(a, 60.0, 135.0)
    c = destination_point(b, 60.0, 225.0)
    return Scenario(
        name=name,
        start=START,
        model=model,
        duration_s=300.0,
        environment=Environment(current_speed=0.2, current_dir=0.0),
        commands=[
            Command(
                2.0,
                {
                    "type": "goto",
                    "throttle": 0.8,
                    "waypoints": [
                        {"name": "A", "lat": a.lat, "lon": a.lon},
                        {"name": "B", "lat": b.lat, "lon": b.lon},
                        {"name": "C", "lat": c.lat, "lon": c.lon},
                    ],
                },
            )
        ],
    )


def _anchor_gusty(name: str, *, model: str) -> Scenario:
    return Scenario(
        name=name,
        start=START,
        model=model,
        duration_s=200.0,
        environment=Environment(
            current_speed=0.2,
            current_dir=90.0,
            wind_speed=4.0,
            wind_dir=120.0,
            gust_amplitude_mps=3.0,
            gust_tau_s=4.0,
        ),
        commands=[Command(2.0, {"type": "anchor_hold", "radius_m": 6.0})],
    )


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in [
        _anchor_displaced("anchor_tight", model="fossen", radius=2.0, displace_m=10.0),
        _anchor_displaced("anchor_tight_simple", model="simple", radius=2.0, displace_m=10.0),
        _anchor_drift("anchor_drift", model="fossen"),
        _anchor_gusty("anchor_gusty", model="fossen"),
        _heading_step("heading_step", model="fossen"),
        _waypoint_box("waypoint_box", model="fossen"),
    ]
}
