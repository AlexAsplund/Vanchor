"""Tests for the depth source: DPT NMEA, bathymetry, sounder, depth map."""

import pytest

from vanchor.core.geo import destination_point
from vanchor.core.models import BoatState, GeoPoint
from vanchor.core.state import NavigationState
from vanchor.nav import nmea
from vanchor.nav.depth import DepthMap
from vanchor.nav.navigator import Navigator
from vanchor.sim.bathymetry import Bathymetry
from vanchor.sim.devices import SimDepthSounder


def test_dpt_roundtrip():
    parsed = nmea.parse(nmea.encode_dpt(7.3))
    assert isinstance(parsed, nmea.Depth)
    assert parsed.depth_m == pytest.approx(7.3, abs=0.1)


def test_dbt_parsed():
    body = "SDDBT,26.2,f,8.0,M,4.3,F"
    parsed = nmea.parse(f"${body}*{nmea.checksum(body)}")
    assert isinstance(parsed, nmea.Depth)
    assert parsed.depth_m == pytest.approx(8.0, abs=0.1)


def test_navigator_sets_depth():
    state = NavigationState()
    nav = Navigator(state, bus=None)
    nav.handle_sentence(nmea.encode_dpt(5.5))
    assert state.depth_m == pytest.approx(5.5, abs=0.1)


def test_bathymetry_within_bounds_and_varies():
    b = Bathymetry()
    depths = [b.depth_at(destination_point(b.origin, off, 45.0)) for off in (0, 50, 120, 250)]
    for d in depths:
        assert b.min_m <= d <= b.max_m
    assert max(depths) - min(depths) > 0.5  # it actually varies


def test_sim_depth_sounder_emits_valid_dpt():
    b = Bathymetry()
    sounder = SimDepthSounder(lambda: BoatState(point=b.origin), b, noise_m=0.0)
    parsed = nmea.parse(sounder.sample())
    assert isinstance(parsed, nmea.Depth)
    assert parsed.depth_m == pytest.approx(b.depth_at(b.origin), abs=0.1)


def test_depth_map_records_by_distance():
    dm = DepthMap(min_distance_m=10.0)
    p = GeoPoint(59.66275, 13.32247)
    dm.record(p, 8.0)
    dm.record(destination_point(p, 5.0, 0.0), 8.0)  # too close, skipped
    dm.record(destination_point(p, 15.0, 0.0), 9.0)
    assert len(dm.points) == 2
    assert dm.as_list()[0][2] == 8.0  # [lat, lon, depth]


def test_depth_map_ignores_zero_depth():
    dm = DepthMap(min_distance_m=0.0)
    dm.record(GeoPoint(59.66, 13.32), 0.0)
    assert dm.points == []


def test_depth_map_persists(tmp_path):
    path = str(tmp_path / "dm.json")
    dm = DepthMap(min_distance_m=0.0)
    dm.record(GeoPoint(59.66, 13.32), 8.0)
    dm.record(GeoPoint(59.67, 13.33), 9.5)
    dm.save(path)
    dm2 = DepthMap()
    dm2.load(path)
    assert len(dm2.points) == 2 and dm2.points[1][2] == 9.5
