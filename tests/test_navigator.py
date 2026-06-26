import pytest

from vanchor.core.state import NavigationState
from vanchor.nav import nmea
from vanchor.nav.navigator import Navigator
from vanchor.core.models import GeoPoint


def test_rmc_updates_position_and_speed():
    state = NavigationState()
    nav = Navigator(state, bus=None)
    nav.handle_sentence(nmea.encode_rmc(GeoPoint(59.0, 18.0), sog_knots=2.5, cog_deg=90))
    assert state.position.lat == pytest.approx(59.0, abs=1e-4)
    assert state.sog_knots == pytest.approx(2.5, abs=0.1)


def test_invalid_rmc_does_not_update():
    state = NavigationState()
    nav = Navigator(state, bus=None)
    bad = nmea.encode_rmc(GeoPoint(1, 1), sog_knots=0, cog_deg=0, valid=False)
    nav.handle_sentence(bad)
    assert state.fix is None


def test_heading_updates_state():
    state = NavigationState()
    nav = Navigator(state, bus=None)
    nav.handle_sentence(nmea.encode_hdm(177.0))
    assert state.heading_deg == pytest.approx(177.0)


def test_apb_stored():
    state = NavigationState()
    nav = Navigator(state, bus=None)
    nav.handle_sentence(nmea.encode_apb(5.0, "L", 90.0))
    assert state.last_apb is not None


def test_garbage_is_ignored():
    state = NavigationState()
    nav = Navigator(state, bus=None)
    nav.handle_sentence("not a sentence")  # should not raise
    assert state.fix is None


def test_handle_sentence_returns_events():
    state = NavigationState()
    nav = Navigator(state, bus=None)
    out = nav.handle_sentence(nmea.encode_rmc(GeoPoint(59, 18), sog_knots=1, cog_deg=0))
    topics = [t for t, _ in out]
    assert "nav.fix" in topics
