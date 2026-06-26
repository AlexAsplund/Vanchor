import pytest

from vanchor.core.models import GeoPoint, Waypoint
from vanchor.nav import routes


def test_roundtrip_preserves_names_and_coords():
    original = [
        Waypoint("Start", GeoPoint(48.1173, 11.51667)),
        Waypoint("Middle", GeoPoint(-33.8688, 151.2093)),
        Waypoint("End", GeoPoint(0.0, 0.0)),
    ]
    text = routes.serialize_gpx(original, name="my-route")
    parsed = routes.parse_gpx(text)
    assert len(parsed) == len(original)
    for got, want in zip(parsed, original):
        assert got.name == want.name
        assert got.point.lat == pytest.approx(want.point.lat, abs=1e-9)
        assert got.point.lon == pytest.approx(want.point.lon, abs=1e-9)


def test_serialize_produces_valid_gpx_header():
    text = routes.serialize_gpx([Waypoint("A", GeoPoint(1.0, 2.0))])
    assert text.startswith("<?xml")
    assert 'version="1.1"' in text
    assert "http://www.topografix.com/GPX/1/1" in text


def test_parse_namespaced_wpt():
    gpx = """<?xml version="1.0" encoding="UTF-8"?>
    <gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
      <wpt lat="48.1173" lon="11.51667"><name>Munich</name></wpt>
      <wpt lat="52.52" lon="13.405"><name>Berlin</name></wpt>
    </gpx>"""
    parsed = routes.parse_gpx(gpx)
    assert [w.name for w in parsed] == ["Munich", "Berlin"]
    assert parsed[0].point.lat == pytest.approx(48.1173)
    assert parsed[1].point.lon == pytest.approx(13.405)


def test_parse_rtept_inside_rte():
    gpx = """<?xml version="1.0"?>
    <gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
      <rte>
        <name>leg</name>
        <rtept lat="10.0" lon="20.0"><name>P1</name></rtept>
        <rtept lat="11.0" lon="21.0"><name>P2</name></rtept>
      </rte>
    </gpx>"""
    parsed = routes.parse_gpx(gpx)
    assert [w.name for w in parsed] == ["P1", "P2"]
    assert parsed[1].point.lat == pytest.approx(11.0)


def test_parse_no_namespace_still_works():
    gpx = '<gpx version="1.1"><wpt lat="1.5" lon="2.5"></wpt></gpx>'
    parsed = routes.parse_gpx(gpx)
    assert len(parsed) == 1
    assert parsed[0].point.as_tuple() == (1.5, 2.5)


def test_missing_name_defaults_to_wp_index():
    gpx = """<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
      <wpt lat="1.0" lon="1.0"/>
      <wpt lat="2.0" lon="2.0"><name>Named</name></wpt>
      <wpt lat="3.0" lon="3.0"/>
    </gpx>"""
    parsed = routes.parse_gpx(gpx)
    assert [w.name for w in parsed] == ["WP0", "Named", "WP2"]


def test_serialize_defaults_missing_name():
    text = routes.serialize_gpx([Waypoint("", GeoPoint(1.0, 1.0))])
    parsed = routes.parse_gpx(text)
    assert parsed[0].name == "WP0"


def test_skip_bad_point_keeps_good_ones():
    gpx = """<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
      <wpt lat="1.0" lon="1.0"><name>Good</name></wpt>
      <wpt lat="oops" lon="2.0"><name>Bad</name></wpt>
      <wpt lon="3.0"><name>NoLat</name></wpt>
      <wpt lat="4.0" lon="4.0"><name>AlsoGood</name></wpt>
    </gpx>"""
    parsed = routes.parse_gpx(gpx)
    assert [w.name for w in parsed] == ["Good", "AlsoGood"]


def test_malformed_xml_raises_valueerror():
    with pytest.raises(ValueError):
        routes.parse_gpx("<gpx><wpt lat='1' lon='2'></gpx>not closed")


def test_empty_gpx_returns_empty_list():
    text = routes.serialize_gpx([], name="empty")
    assert routes.parse_gpx(text) == []
