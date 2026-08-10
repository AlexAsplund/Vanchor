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


# --- refresh_land_guard_water reload cadence (event-loop stall regression) --- #

def _refresh_rt(tmp_path, monkeypatch):
    """A sim Runtime with a controllable clock and a counting WaterCache."""
    from vanchor.app import Runtime
    from vanchor.core.config import load as _load
    from vanchor.core.models import GeoPoint, GpsFix
    from vanchor.nav import water as _water

    cfg = _load(None)
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    rt.controller.safety.config.land_guard_enabled = True
    rt.state.fix = GpsFix(point=GeoPoint(59.66, 13.32), valid=True)
    clock = {"t": 1000.0}
    rt._mono_fn = lambda: clock["t"]
    calls = {"n": 0}

    def counting_find(self, bbox):
        calls["n"] += 1
        return None                      # nothing cached

    monkeypatch.setattr(_water.WaterCache, "find_covering", counting_find)
    return rt, clock, calls


def test_covered_boat_never_reloads_the_chart(tmp_path, monkeypatch):
    # Regression: with the chart loaded and the boat inside the trigger box, the
    # 20 s timer used to force a FULL cache re-parse + geometry hand-off every
    # expiry -- a periodic event-loop stall ("data stale" + WS drops). Covered
    # must mean NO reload, ever.
    from shapely.geometry import MultiPolygon, Polygon
    rt, clock, calls = _refresh_rt(tmp_path, monkeypatch)
    rt.controller.safety.set_water_geometry(
        MultiPolygon([Polygon([(13.0, 59.5), (13.6, 59.5), (13.6, 59.8), (13.0, 59.8)])]))
    rt._land_water_bbox = (59.6, 13.2, 59.7, 13.4)   # boat inside
    for k in range(10):
        clock["t"] += 25.0                            # every timer expiry
        assert rt.refresh_land_guard_water() is False
    assert calls["n"] == 0                            # cache never touched


def test_uncovered_boat_throttles_reload_attempts(tmp_path, monkeypatch):
    # Regression: OUTSIDE any cached chart, the old guard bypassed the timer and
    # re-scanned the whole cache at the 1 Hz supervisor rate. Attempts must be
    # throttled to the 20 s cadence.
    rt, clock, calls = _refresh_rt(tmp_path, monkeypatch)
    assert rt.refresh_land_guard_water() is False     # first attempt (miss)
    assert calls["n"] == 1
    for k in range(19):                               # 19 more 1 Hz ticks
        clock["t"] += 1.0
        rt.refresh_land_guard_water()
    assert calls["n"] == 1                            # throttled: no re-scan yet
    clock["t"] += 2.0                                 # past the 20 s timer
    rt.refresh_land_guard_water()
    assert calls["n"] == 2                            # one retry per 20 s
