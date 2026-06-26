"""End-to-end closed-loop tests: navigator + controller + simulator together,
deterministic and hardware-free."""

import pytest

from vanchor.core.geo import destination_point, haversine_m
from vanchor.core.models import Environment

from .harness import STOCKHOLM, Harness


def test_anchor_hold_converges_under_drift():
    env = Environment(current_speed=0.3, current_dir=90.0, wind_speed=4.0, wind_dir=120.0)
    h = Harness(environment=env)
    h.command({"type": "anchor_hold", "radius_m": 5.0})
    distances = h.run(seconds=240)

    settled = distances[-200:]  # last ~10 s
    # The boat must be actively held near the anchor despite continuous drift.
    assert max(settled) < 7.0, f"drifted away: max={max(settled):.1f}m"
    assert sum(settled) / len(settled) < 5.0


def test_anchor_hold_returns_after_displacement():
    # Start displaced 25 m from where we drop the anchor; it must come back.
    h = Harness(environment=Environment())
    # Drop the anchor at the *current* position, then shove the boat away.
    h.command({"type": "anchor_hold", "radius_m": 4.0})
    anchor = h.state.anchor
    h.sim.boat.state.point = destination_point(anchor, 25.0, 45.0)
    distances = h.run(seconds=180)
    assert distances[0] > 20.0  # started far
    assert distances[-1] < 5.0  # ended close


def test_heading_hold_reaches_target():
    h = Harness(environment=Environment())
    h.command({"type": "heading_hold", "heading": 90.0, "throttle": 0.5})
    h.run(seconds=60)
    assert h.sim.truth().heading_deg == pytest.approx(90.0, abs=5.0)


def test_waypoint_navigation_reaches_marks():
    h = Harness(environment=Environment())
    wp0 = destination_point(STOCKHOLM, 60.0, 45.0)
    wp1 = destination_point(wp0, 60.0, 135.0)
    h.command(
        {
            "type": "goto",
            "throttle": 0.8,
            "waypoints": [
                {"name": "A", "lat": wp0.lat, "lon": wp0.lon},
                {"name": "B", "lat": wp1.lat, "lon": wp1.lon},
            ],
        }
    )
    h.run(seconds=240)
    # Both waypoints consumed => active index advanced past the list.
    assert h.state.active_waypoint >= 2
    assert haversine_m(h.sim.truth().point, wp1) < 8.0


def test_manual_command_drives_motor():
    h = Harness(environment=Environment())
    h.command({"type": "manual", "thrust": 1.0, "steering": 0.0})
    h.run(seconds=10)
    assert h.sim.truth().speed_mps > 0.5
