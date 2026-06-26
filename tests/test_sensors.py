"""Tests for sensor-anomaly protection (spike rejection with confirmation)."""

from vanchor.core.geo import destination_point
from vanchor.core.models import GeoPoint
from vanchor.nav.guard import SensorGuard, SensorGuardConfig

P = GeoPoint(59.66275, 13.32247)


def _g(**kw) -> SensorGuard:
    return SensorGuard(SensorGuardConfig(**kw))


def test_accepts_first_and_small_moves():
    g = _g(position_jump_max_m=15.0)
    assert g.check_position(P)
    assert g.check_position(destination_point(P, 5.0, 0.0))
    assert g.position_rejected == 0


def test_rejects_isolated_position_glitch():
    g = _g(position_jump_max_m=15.0)
    g.check_position(P)
    assert not g.check_position(destination_point(P, 200.0, 90.0))  # glitch
    assert g.position_rejected == 1
    # A reading back near the real position continues normally.
    assert g.check_position(destination_point(P, 2.0, 0.0))


def test_accepts_confirmed_large_move():
    g = _g(position_jump_max_m=15.0)
    g.check_position(P)
    far = destination_point(P, 100.0, 45.0)
    assert not g.check_position(far)  # first jump rejected
    assert g.check_position(destination_point(far, 3.0, 0.0))  # confirmed -> accepted


def test_out_of_range_position_rejected():
    g = _g()
    assert not g.check_position(GeoPoint(95.0, 200.0))
    assert g.position_rejected == 1


def test_rejects_heading_flip_then_accepts_confirmed():
    g = _g(heading_jump_max_deg=30.0)
    assert g.check_heading(10.0)
    assert g.check_heading(20.0)
    assert not g.check_heading(200.0)  # ~180 deg flip in one sample
    assert g.heading_rejected == 1
    assert g.check_heading(205.0)  # confirms the new heading -> accepted


def test_heading_wrap_small_change_accepted():
    g = _g(heading_jump_max_deg=30.0)
    g.check_heading(355.0)
    assert g.check_heading(5.0)  # 10 deg across the wrap, accepted
    assert g.heading_rejected == 0
