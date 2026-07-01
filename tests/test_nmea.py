import pytest

from vanchor.core.models import GeoPoint
from vanchor.nav import nmea


def test_checksum_known_sentence():
    # A well-known GPGGA body has checksum 47.
    body = "GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"
    assert nmea.checksum(body) == "47"


def test_parse_real_rmc():
    s = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"
    p = nmea.parse(s)
    assert isinstance(p, nmea.RMC)
    assert p.valid
    assert p.point.lat == pytest.approx(48.1173, abs=1e-4)
    assert p.point.lon == pytest.approx(11.51667, abs=1e-4)
    assert p.sog_knots == pytest.approx(22.4)
    assert p.cog_deg == pytest.approx(84.4)


def test_bad_checksum_raises():
    with pytest.raises(nmea.NmeaError):
        nmea.parse("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,,*00")


def test_empty_checksum_raises():
    # A sentence with '*' but nothing after it must be rejected.
    with pytest.raises(nmea.NmeaError):
        nmea.parse("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,,*")


def test_malformed_checksum_raises():
    # Non-hex characters after '*' must be rejected.
    with pytest.raises(nmea.NmeaError):
        nmea.parse("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,,*ZZ")


def test_no_checksum_accepted_by_default():
    # Devices that omit the '*checksum' entirely are still accepted by default.
    s = "$GPHDM,123.4,M"
    result = nmea.parse(s)
    assert isinstance(result, nmea.Heading)


def test_no_checksum_rejected_when_required():
    with pytest.raises(nmea.NmeaError):
        nmea.parse("$GPHDM,123.4,M", require_checksum=True)


def test_valid_checksum_passes_require_checksum():
    s = nmea.encode_hdm(123.4)
    result = nmea.parse(s, require_checksum=True)
    assert isinstance(result, nmea.Heading)


def test_has_valid_checksum_dollar():
    good = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"
    assert nmea.has_valid_checksum(good) is True
    bad_cs = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*00"
    assert nmea.has_valid_checksum(bad_cs) is False
    no_star = "$GPHDM,123.4,M"
    assert nmea.has_valid_checksum(no_star) is False


def test_has_valid_checksum_bang():
    # AIS sentence with correct checksum 0x40.
    good = "!AIVDM,1,1,,A,foo,0*40"
    assert nmea.has_valid_checksum(good) is True
    bad = "!AIVDM,1,1,,A,foo,0*5C"
    assert nmea.has_valid_checksum(bad) is False


def test_has_valid_checksum_empty_field():
    assert nmea.has_valid_checksum("$GPHDM,123.4,M*") is False
    assert nmea.has_valid_checksum("") is False


def test_missing_dollar_raises():
    with pytest.raises(nmea.NmeaError):
        nmea.parse("GPRMC,foo")


def test_unmodelled_sentence_returns_none():
    # VTG is well-formed but we don't model it.
    assert nmea.parse("$GPVTG,054.7,T,034.4,M,005.5,N,010.2,K*48") is None


def test_rmc_roundtrip():
    pt = GeoPoint(59.3293, 18.0686)
    s = nmea.encode_rmc(pt, sog_knots=3.2, cog_deg=145.0)
    p = nmea.parse(s)
    assert isinstance(p, nmea.RMC)
    assert p.point.lat == pytest.approx(pt.lat, abs=1e-5)
    assert p.point.lon == pytest.approx(pt.lon, abs=1e-5)
    assert p.sog_knots == pytest.approx(3.2, abs=0.1)


def test_southern_western_hemisphere_roundtrip():
    pt = GeoPoint(-33.8688, -151.2093)
    p = nmea.parse(nmea.encode_rmc(pt, sog_knots=0, cog_deg=0))
    assert p.point.lat == pytest.approx(pt.lat, abs=1e-5)
    assert p.point.lon == pytest.approx(pt.lon, abs=1e-5)


def test_hdm_roundtrip():
    s = nmea.encode_hdm(123.4)
    p = nmea.parse(s)
    assert isinstance(p, nmea.Heading)
    assert p.reference == "M"
    assert p.heading_deg == pytest.approx(123.4)


def test_hdt_reference():
    """HDT sentences must be parsed with reference='T'."""
    s = nmea.encode_hdt(180.5)
    p = nmea.parse(s)
    assert isinstance(p, nmea.Heading)
    assert p.reference == "T"
    assert p.heading_deg == pytest.approx(180.5)


def test_hdg_with_dev_and_var_yields_true():
    """HDG with both deviation and variation → reference='T', corrected value.

    Convention: True = sensor + deviation(E+, W-) + variation(E+, W-)
    Example: sensor=100, dev=2°E, var=5°W → True = 100 + 2 - 5 = 97°
    """
    s = nmea.encode_hdg(100.0, deviation_deg=2.0, variation_deg=-5.0)
    p = nmea.parse(s)
    assert isinstance(p, nmea.Heading)
    assert p.reference == "T"
    assert p.heading_deg == pytest.approx(97.0, abs=0.1)


def test_hdg_with_east_var_adds():
    """Easterly variation is positive — adds to the sensor heading."""
    s = nmea.encode_hdg(90.0, deviation_deg=0.0, variation_deg=10.0)
    p = nmea.parse(s)
    assert p.reference == "T"
    assert p.heading_deg == pytest.approx(100.0, abs=0.1)


def test_hdg_without_dev_var_yields_magnetic():
    """HDG with no correction fields → reference='M', raw sensor heading."""
    s = nmea.encode_hdg(100.0)
    p = nmea.parse(s)
    assert isinstance(p, nmea.Heading)
    assert p.reference == "M"
    assert p.heading_deg == pytest.approx(100.0)


def test_apb_roundtrip():
    s = nmea.encode_apb(12.5, "R", 95.0, dest_id="WP3", arrived=False)
    p = nmea.parse(s)
    assert isinstance(p, nmea.APB)
    assert p.cross_track_m == pytest.approx(12.5)
    assert p.steer_to == "R"
    assert p.dest_id == "WP3"
    assert p.arrived is False


def test_gga_roundtrip():
    pt = GeoPoint(10.0, -20.0)
    p = nmea.parse(nmea.encode_gga(pt, satellites=7, quality=1))
    assert isinstance(p, nmea.GGA)
    assert p.satellites == 7
    assert p.point.lat == pytest.approx(10.0, abs=1e-5)
