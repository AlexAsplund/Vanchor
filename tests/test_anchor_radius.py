"""A tight anchor radius must not cause overcorrection.

Without a recovery floor, a small radius sits below the GPS-noise floor so the
boat is permanently in aggressive "recover" mode -- darting. The floor makes a
small radius hold as calmly as a large one.
"""

from vanchor.analysis.metrics import steering_activity
from vanchor.analysis.runner import Command, Scenario, run_scenario
from vanchor.controller.modes import AnchorConfig
from vanchor.core.models import Environment, GeoPoint

START = GeoPoint(59.66275, 13.32247)


def _steer_activity(radius, floor):
    scen = Scenario(
        name="a", start=START, model="fossen", duration_s=120, environment=Environment(),
        anchor_config=AnchorConfig(recover_floor_m=floor),
        commands=[Command(2, {"type": "anchor_hold", "radius_m": radius})],
    )
    return steering_activity(run_scenario(scen)).mean_rate_dps


def test_recover_floor_calms_tight_radius():
    old = _steer_activity(1.0, 0.01)   # no floor -> overcorrects
    new = _steer_activity(1.0, 3.5)    # floor -> calm
    assert new < 0.7 * old


def test_large_radius_unaffected_by_floor():
    assert abs(_steer_activity(4.0, 0.01) - _steer_activity(4.0, 3.5)) < 0.6
