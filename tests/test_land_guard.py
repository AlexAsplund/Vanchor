"""Land-collision guard: auto-stop before the shoreline while driving manually.

The governor probes the offline water chart along the boat's TRACK and cuts
thrust ``land_guard_margin_m`` metres before land (plus a small coasting
allowance). Direction-aware, so thrusting AWAY from the shore always works.
"""

import math

import pytest
from shapely.geometry import Polygon

from vanchor.controller.safety import SafetyConfig, SafetyGovernor
from vanchor.core.geo import destination_point
from vanchor.core.models import ControlModeName, GeoPoint, GpsFix, MotorCommand
from vanchor.core.state import NavigationState

LAT0, LON0 = 59.0, 18.0
COS = math.cos(math.radians(LAT0))


def _square_lake(half_m: float = 200.0) -> Polygon:
    """A square lake centred on (LAT0, LON0), ``half_m`` metres to each shore."""
    dlat = half_m / 111320.0
    dlon = half_m / (111320.0 * COS)
    return Polygon([
        (LON0 - dlon, LAT0 - dlat), (LON0 + dlon, LAT0 - dlat),
        (LON0 + dlon, LAT0 + dlat), (LON0 - dlon, LAT0 + dlat),
    ])


def _state(pos: GeoPoint, heading: float, thrust: float = 0.5,
           mode=ControlModeName.MANUAL) -> NavigationState:
    st = NavigationState()
    st.fix = GpsFix(point=pos)
    st.heading_deg = heading
    st.mode = mode
    st.motor_command = MotorCommand(thrust=thrust, steering=0.0)
    return st


def _gov(**kw) -> SafetyGovernor:
    gov = SafetyGovernor(SafetyConfig(**kw))
    gov.set_water_geometry(_square_lake())
    return gov


def _govern(gov, st, thrust=0.5):
    # Two passes: the probe result computed on the first tick feeds the same
    # tick's decision, but run twice to also flush the probe throttle.
    cmd, status = gov.govern(MotorCommand(thrust=thrust, steering=0.0), st, 0.6, True)
    return cmd, status


def test_clear_water_ahead_no_trip_but_stop_point_shown():
    gov = _gov(land_guard_margin_m=15.0)
    st = _state(GeoPoint(LAT0, LON0), heading=0.0)   # centre, 200 m to shore
    cmd, status = _govern(gov, st)
    assert status.land_guard_active
    assert not status.land_stop
    assert status.land_distance_m == pytest.approx(200.0, abs=8.0)
    # Predicted stop point sits margin_m short of the north shore.
    stop = GeoPoint(status.land_stop_lat, status.land_stop_lon)
    d_stop = status.land_distance_m - 15.0
    expect = destination_point(GeoPoint(LAT0, LON0), d_stop, 0.0)
    assert abs(stop.lat - expect.lat) < 2e-5 and abs(stop.lon - expect.lon) < 2e-5


def test_trips_and_cuts_thrust_close_to_shore():
    gov = _gov(land_guard_margin_m=15.0)
    near = destination_point(GeoPoint(LAT0, LON0), 190.0, 0.0)  # 10 m off the shore
    st = _state(near, heading=0.0)
    cmd, status = _govern(gov, st)
    assert status.land_stop
    assert cmd.thrust == 0.0


def test_reversing_away_from_shore_is_allowed():
    gov = _gov(land_guard_margin_m=15.0)
    near = destination_point(GeoPoint(LAT0, LON0), 190.0, 0.0)
    st = _state(near, heading=0.0, thrust=-0.4)      # backing away, bow at shore
    cmd, status = _govern(gov, st, thrust=-0.4)
    assert not status.land_stop                       # track points to open water
    assert cmd.thrust != 0.0


def test_disabled_or_guided_modes_inactive():
    gov = _gov(land_guard_margin_m=15.0, land_guard_enabled=False)
    near = destination_point(GeoPoint(LAT0, LON0), 190.0, 0.0)
    cmd, status = _govern(gov, _state(near, heading=0.0))
    assert not status.land_guard_active and not status.land_stop
    assert cmd.thrust != 0.0

    gov2 = _gov(land_guard_margin_m=15.0)
    st = _state(near, heading=0.0, mode=ControlModeName.WAYPOINT)
    cmd, status = _govern(gov2, st)
    assert not status.land_guard_active               # manual only


def test_no_chart_means_inert():
    gov = SafetyGovernor(SafetyConfig(land_guard_margin_m=15.0))
    near = destination_point(GeoPoint(LAT0, LON0), 190.0, 0.0)
    cmd, status = _govern(gov, _state(near, heading=0.0))
    assert not status.land_guard_active
    assert cmd.thrust != 0.0


def test_margin_scales_the_trip_distance():
    gov = _gov(land_guard_margin_m=100.0)
    st = _state(destination_point(GeoPoint(LAT0, LON0), 120.0, 0.0), heading=0.0)
    # 80 m of water left, 100 m guard -> tripped.
    cmd, status = _govern(gov, st)
    assert status.land_stop and cmd.thrust == 0.0


def test_persistence_via_safety_store(tmp_path):
    from vanchor.core.prefs import SafetyGeometryStore
    store = SafetyGeometryStore(str(tmp_path))
    store.set_land_guard(False, 42.0)
    fresh = SafetyGeometryStore(str(tmp_path))
    assert fresh.land_guard_enabled is False
    assert fresh.land_guard_margin_m == 42.0


def test_braking_in_reverse_while_carried_toward_land_is_allowed():
    """Moving toward the shore with way on: commanding REVERSE is braking —
    the guard must never cut it (that would remove the operator's brake)."""
    gov = _gov(land_guard_margin_m=15.0)
    near = destination_point(GeoPoint(LAT0, LON0), 190.0, 0.0)
    st = _state(near, heading=0.0)
    st.sog_knots = 2.0
    st.fix = GpsFix(point=near, sog_knots=2.0, cog_deg=0.0)   # drifting at land
    cmd, status = _govern(gov, st, thrust=-0.6)
    assert not status.land_stop
    assert cmd.thrust < 0.0


def test_guard_cut_does_not_freeze_the_probe_direction():
    """Regression (live e2e): after the guard cut thrust, the probe kept using
    the APPLIED (zeroed) thrust and pointed at land forever — reversing away
    was dead-locked. The probe must follow the COMMANDED direction."""
    gov = _gov(land_guard_margin_m=15.0)
    near = destination_point(GeoPoint(LAT0, LON0), 190.0, 0.0)
    st = _state(near, heading=0.0, thrust=0.0)   # applied thrust already cut
    cmd, status = _govern(gov, st, thrust=-0.4)  # commanding reverse (away)
    assert not status.land_stop
    assert cmd.thrust != 0.0
