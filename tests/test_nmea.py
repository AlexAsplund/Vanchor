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
