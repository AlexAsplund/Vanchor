"""Heading semantics (#47): honor the M/T reference, a single central
declination, and correct HDT emission.

These lock in that the navigator works in one consistent TRUE frame:
* MAGNETIC sources (HDM / uncorrected HDG, reference="M") get declination added.
* TRUE sources (HDT / fully-corrected HDG, reference="T") pass through.
* the internal WMM/IGRF declination model is East-positive and reasonable.
* the zero-declination path (the simulator's world) is an exact no-op.
"""

import math

import pytest

from vanchor.core.models import GeoPoint
from vanchor.core.state import NavigationState
from vanchor.nav import nmea
from vanchor.nav.navigator import Navigator, magnetic_declination_deg

# A well-surveyed reference: Stockholm harbour. Real 2020 declination ~ +6.5 deg E.
STOCKHOLM = GeoPoint(59.33, 18.06)


# --------------------------------------------------------------------------- #
# (1) HDM + known declination -> correct true heading
# --------------------------------------------------------------------------- #
def test_hdm_plus_known_declination_yields_true():
    state = NavigationState()
    nav = Navigator(state, bus=None, declination_deg=12.0)
    nav.handle_sentence(nmea.encode_hdm(100.0))
    assert state.heading_deg == pytest.approx(112.0, abs=0.1)


def test_hdm_declination_wraps_past_360():
    state = NavigationState()
    nav = Navigator(state, bus=None, declination_deg=20.0)
    nav.handle_sentence(nmea.encode_hdm(350.0))
    assert state.heading_deg == pytest.approx(10.0, abs=0.1)


def test_hdm_west_declination_subtracts():
    state = NavigationState()
    nav = Navigator(state, bus=None, declination_deg=-8.0)
    nav.handle_sentence(nmea.encode_hdm(100.0))
    assert state.heading_deg == pytest.approx(92.0, abs=0.1)


# --------------------------------------------------------------------------- #
# (2) HDG variation field honored
# --------------------------------------------------------------------------- #
def test_hdg_variation_only_is_honored_as_true():
    """A real HDG often has an empty deviation field but a valid variation.
    Variation is what carries the sentence to the true frame, so it must be
    applied and the reference must be 'T' (was dropped as 'M' before #47)."""
    s = nmea.encode_hdg(90.0, deviation_deg=None, variation_deg=10.0)
    p = nmea.parse(s)
    assert isinstance(p, nmea.Heading)
    assert p.reference == "T"
    assert p.heading_deg == pytest.approx(100.0, abs=0.1)


def test_hdg_variation_only_true_not_shifted_again_by_navigator():
    """reference='T' from variation must NOT get declination applied on top."""
    state = NavigationState()
    nav = Navigator(state, bus=None, declination_deg=15.0)
    nav.handle_sentence(nmea.encode_hdg(90.0, deviation_deg=None, variation_deg=10.0))
    assert state.heading_deg == pytest.approx(100.0, abs=0.1)


def test_hdg_deviation_and_variation_combine_to_true():
    state = NavigationState()
    nav = Navigator(state, bus=None, declination_deg=15.0)
    # sensor=100, dev=2E, var=5W -> true = 97 (declination must NOT re-apply)
    nav.handle_sentence(nmea.encode_hdg(100.0, deviation_deg=2.0, variation_deg=-5.0))
    assert state.heading_deg == pytest.approx(97.0, abs=0.1)


def test_hdg_deviation_only_stays_magnetic_and_gets_declination():
    """Deviation alone reaches only MAGNETIC (reference='M'); the navigator then
    applies its declination to reach true."""
    s = nmea.encode_hdg(100.0, deviation_deg=3.0, variation_deg=None)
    p = nmea.parse(s)
    assert p.reference == "M"
    assert p.heading_deg == pytest.approx(103.0, abs=0.1)

    state = NavigationState()
    nav = Navigator(state, bus=None, declination_deg=10.0)
    nav.handle_sentence(s)
    assert state.heading_deg == pytest.approx(113.0, abs=0.1)


# --------------------------------------------------------------------------- #
# (3) HDT passes through as true
# --------------------------------------------------------------------------- #
def test_hdt_passes_through_untouched():
    state = NavigationState()
    nav = Navigator(state, bus=None, declination_deg=25.0)
    nav.handle_sentence(nmea.encode_hdt(100.0))
    assert state.heading_deg == pytest.approx(100.0, abs=0.1)


def test_true_heading_feeds_encode_hdt_roundtrip():
    """state.heading_deg is always true, so it round-trips through encode_hdt."""
    state = NavigationState()
    nav = Navigator(state, bus=None, declination_deg=10.0)
    nav.handle_sentence(nmea.encode_hdm(100.0))  # -> true 110
    assert nav.true_heading_deg == pytest.approx(110.0, abs=0.1)
    emitted = nmea.encode_hdt(nav.true_heading_deg)
    p = nmea.parse(emitted)
    assert isinstance(p, nmea.Heading)
    assert p.reference == "T"
    assert p.heading_deg == pytest.approx(110.0, abs=0.1)


# --------------------------------------------------------------------------- #
# (4) zero-declination path is an exact no-op vs. today (the sim world)
# --------------------------------------------------------------------------- #
def test_zero_declination_is_noop():
    state = NavigationState()
    nav = Navigator(state, bus=None)  # default declination_deg=0.0
    nav.handle_sentence(nmea.encode_hdm(177.0))
    assert state.heading_deg == pytest.approx(177.0)


def test_auto_mode_without_fix_is_noop():
    """AUTO (declination_deg=None) with no position yet falls back to 0.0 so
    behaviour still matches the zero-declination sim path."""
    state = NavigationState()
    nav = Navigator(state, bus=None, declination_deg=None)
    assert nav._declination() == 0.0
    nav.handle_sentence(nmea.encode_hdm(177.0))
    assert state.heading_deg == pytest.approx(177.0)


# --------------------------------------------------------------------------- #
# The internal WMM/IGRF declination model
# --------------------------------------------------------------------------- #
def test_model_is_pure_and_bounded():
    a = magnetic_declination_deg(59.33, 18.06)
    b = magnetic_declination_deg(59.33, 18.06)
    assert a == b  # deterministic
    for lat in (-89.0, -45.0, 0.0, 45.0, 89.0):
        for lon in (-179.0, -90.0, 0.0, 90.0, 179.0):
            d = magnetic_declination_deg(lat, lon)
            assert -180.0 <= d <= 180.0
            assert math.isfinite(d)


def test_model_geographic_pole_is_defined():
    # No blow-up at the geographic pole (horizontal field vanishes -> 0.0).
    assert magnetic_declination_deg(90.0, 0.0) == 0.0
    assert magnetic_declination_deg(-90.0, 123.0) == 0.0


def test_model_stockholm_is_east_positive_and_reasonable():
    d = magnetic_declination_deg(STOCKHOLM.lat, STOCKHOLM.lon)
    # Real 2020 value is ~ +6.5 deg E; the low-degree model gives ~ +5.9.
    assert d == pytest.approx(6.0, abs=3.0)
    assert d > 0.0


# --------------------------------------------------------------------------- #
# AUTO mode drives the central model off the boat's fix
# --------------------------------------------------------------------------- #
def test_auto_mode_uses_model_at_current_fix():
    state = NavigationState()
    nav = Navigator(state, bus=None, declination_deg=None)
    # Establish a position first.
    nav.handle_sentence(nmea.encode_rmc(STOCKHOLM, sog_knots=2.0, cog_deg=90.0))
    assert state.position is not None

    expected = magnetic_declination_deg(STOCKHOLM.lat, STOCKHOLM.lon)
    assert abs(expected) > 1.0  # the correction is genuinely non-trivial here
    assert nav._declination() == pytest.approx(expected, abs=1e-9)

    nav.handle_sentence(nmea.encode_hdm(100.0))
    assert state.heading_deg == pytest.approx((100.0 + expected) % 360, abs=0.1)


def test_auto_mode_leaves_true_headings_alone():
    state = NavigationState()
    nav = Navigator(state, bus=None, declination_deg=None)
    nav.handle_sentence(nmea.encode_rmc(STOCKHOLM, sog_knots=2.0, cog_deg=90.0))
    nav.handle_sentence(nmea.encode_hdt(100.0))
    assert state.heading_deg == pytest.approx(100.0, abs=0.1)
