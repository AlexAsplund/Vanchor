"""COG-derived heading fallback (#17).

When the compass goes stale/lost, a guided mode has no heading to steer on and
the safety governor coasts the boat. If the GPS shows the boat making way, the
navigator falls back to steering on course-over-ground so guided modes keep
steering. These tests drive the navigator directly with a controllable
monotonic clock (matching the other navigator tests).
"""

import pytest

from vanchor.core.models import GeoPoint
from vanchor.core.state import NavigationState
from vanchor.nav import nmea
from vanchor.nav.navigator import (
    COG_HEADING_MIN_SOG_KNOTS,
    COMPASS_STALE_S,
    Navigator,
)

_POS = GeoPoint(59.0, 18.0)


def _nav(clock):
    """A navigator driven by a mutable one-element clock list."""
    state = NavigationState()
    nav = Navigator(state, bus=None, mono_fn=lambda: clock[0])
    return state, nav


def test_fresh_compass_is_used_not_cog():
    """(a) A fresh compass heading wins; a fix with a different COG is ignored."""
    clock = [0.0]
    state, nav = _nav(clock)
    nav.handle_sentence(nmea.encode_hdt(90.0))  # true heading 90
    # A fix arrives moments later with a very different course.
    clock[0] = 1.0  # well within COMPASS_STALE_S
    nav.handle_sentence(nmea.encode_rmc(_POS, sog_knots=3.0, cog_deg=270.0))
    assert state.heading_deg == pytest.approx(90.0)
    assert state.heading_from_cog is False


def test_stale_compass_moving_tracks_cog():
    """(b) Compass stale + boat moving -> heading tracks COG and the flag is set."""
    clock = [0.0]
    state, nav = _nav(clock)
    nav.handle_sentence(nmea.encode_hdt(90.0))
    # Let the compass go stale, then a good fix at speed arrives.
    clock[0] = COMPASS_STALE_S + 1.0
    nav.handle_sentence(nmea.encode_rmc(_POS, sog_knots=3.0, cog_deg=270.0))
    assert state.heading_deg == pytest.approx(270.0)
    assert state.heading_from_cog is True
    # The governor watches heading_received_mono; the fallback refreshed it so a
    # guided mode is NOT coasted for a stale compass.
    assert state.heading_received_mono == pytest.approx(clock[0])
    # The real-compass stamp is untouched, so it is still known to be stale.
    assert state.compass_received_mono == pytest.approx(0.0)


def test_stale_compass_at_rest_still_coasts():
    """(c) Compass stale but boat at rest -> COG is noise, so we do NOT adopt it."""
    clock = [0.0]
    state, nav = _nav(clock)
    nav.handle_sentence(nmea.encode_hdt(90.0))
    stamp_before = state.heading_received_mono
    clock[0] = COMPASS_STALE_S + 1.0
    # Barely moving: below the trust threshold.
    nav.handle_sentence(
        nmea.encode_rmc(_POS, sog_knots=COG_HEADING_MIN_SOG_KNOTS - 0.1, cog_deg=270.0)
    )
    assert state.heading_deg == pytest.approx(90.0)  # last compass value held
    assert state.heading_from_cog is False
    # heading_received_mono NOT refreshed -> stays stale -> governor coasts.
    assert state.heading_received_mono == pytest.approx(stamp_before)


def test_compass_return_resumes_and_clears_flag():
    """When the compass comes back, it is used again and the COG flag clears."""
    clock = [0.0]
    state, nav = _nav(clock)
    nav.handle_sentence(nmea.encode_hdt(90.0))
    clock[0] = COMPASS_STALE_S + 1.0
    nav.handle_sentence(nmea.encode_rmc(_POS, sog_knots=3.0, cog_deg=270.0))
    assert state.heading_from_cog is True
    # Compass recovers (a small change from the last real 90, so the sensor
    # guard's jump filter accepts it on the first reading).
    clock[0] += 1.0
    nav.handle_sentence(nmea.encode_hdt(100.0))
    assert state.heading_deg == pytest.approx(100.0)
    assert state.heading_from_cog is False
    assert state.compass_received_mono == pytest.approx(clock[0])


def test_never_had_compass_does_not_synthesise_heading():
    """A boat that has never had a compass keeps its existing behaviour: no COG
    heading is invented even when moving fast."""
    clock = [100.0]
    state, nav = _nav(clock)
    nav.handle_sentence(nmea.encode_rmc(_POS, sog_knots=5.0, cog_deg=200.0))
    assert state.heading_deg == pytest.approx(0.0)  # untouched default
    assert state.heading_from_cog is False


def test_cog_at_threshold_speed_engages():
    """Exactly at the SOG threshold the fallback engages (>= is trusted)."""
    clock = [0.0]
    state, nav = _nav(clock)
    nav.handle_sentence(nmea.encode_hdt(10.0))
    clock[0] = COMPASS_STALE_S + 1.0
    nav.handle_sentence(
        nmea.encode_rmc(_POS, sog_knots=COG_HEADING_MIN_SOG_KNOTS, cog_deg=123.0)
    )
    assert state.heading_from_cog is True
    assert state.heading_deg == pytest.approx(123.0)


def test_gga_only_stale_cog_does_not_refresh_heading():
    """(#9) A GGA has no course/speed -- it forwards the previous fix's cog/sog.
    If the boat has since stopped but only GGA keeps arriving, that stale sog must
    NOT keep re-arming the COG fallback on a dead course. Only a fix with genuine
    course/speed (RMC) may drive it."""
    clock = [0.0]
    state, nav = _nav(clock)
    nav.handle_sentence(nmea.encode_hdt(90.0))
    # While the compass is fresh, a real RMC seeds fix.cog/sog (270 @ 3 kn).
    nav.handle_sentence(nmea.encode_rmc(_POS, sog_knots=3.0, cog_deg=270.0))
    assert state.heading_from_cog is False  # compass still fresh
    stamp_before = state.heading_received_mono
    # Compass goes stale; the boat has actually stopped, but only GGA keeps
    # arriving (same position). GGA forwards the last 270 @ 3 kn -> stale.
    clock[0] = COMPASS_STALE_S + 1.0
    nav.handle_sentence(nmea.encode_gga(_POS))
    assert state.heading_from_cog is False           # not driven by a GGA
    assert state.heading_deg == pytest.approx(90.0)  # last real compass held
    # heading_received_mono NOT refreshed -> stays stale -> the governor coasts.
    assert state.heading_received_mono == pytest.approx(stamp_before)


def test_fresh_rmc_still_drives_cog_after_gga():
    """(#9) The GGA guard must not disable the real fallback: a subsequent fresh
    RMC with genuine SOG still adopts COG when the compass is stale."""
    clock = [0.0]
    state, nav = _nav(clock)
    nav.handle_sentence(nmea.encode_hdt(90.0))
    clock[0] = COMPASS_STALE_S + 1.0
    nav.handle_sentence(nmea.encode_gga(_POS))       # ignored for COG
    assert state.heading_from_cog is False
    nav.handle_sentence(nmea.encode_rmc(_POS, sog_knots=3.0, cog_deg=270.0))
    assert state.heading_from_cog is True
    assert state.heading_deg == pytest.approx(270.0)


def test_heading_from_cog_in_telemetry():
    """The flag is surfaced in to_dict so the UI/alarm can say 'using GPS course'."""
    state = NavigationState()
    assert state.to_dict()["heading_from_cog"] is False
    state.heading_from_cog = True
    assert state.to_dict()["heading_from_cog"] is True
