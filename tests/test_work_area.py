"""Work Area mode: travel to each spot, HOLD position (spot-lock), then advance
on the on-screen button and/or a dwell timer; loop/patrol cycle the spots; each
spot may carry a desired hold heading.
"""

from vanchor.controller.controller import Controller
from vanchor.controller.modes import WorkAreaConfig, WorkAreaMode
from vanchor.core.models import ControlModeName, GeoPoint, GpsFix, GuidedSetpoint, Waypoint
from vanchor.core.state import NavigationState
from vanchor.sim.devices import SimMotorController


def _state(headings=None, n=3):
    st = NavigationState()
    st.waypoints = [
        Waypoint(name=f"S{i}", point=GeoPoint(59.0 + i * 0.002, 18.0),
                 heading=(headings[i] if headings else None))
        for i in range(n)
    ]
    st.active_waypoint = 0
    return st


def _park_on(st, idx):
    """Sit the boat exactly on spot idx so the mode registers arrival."""
    st.fix = GpsFix(point=st.waypoints[idx].point)


def test_travels_then_arrives_and_holds():
    m = WorkAreaMode(WorkAreaConfig(advance="manual"))
    st = _state(); m.activate(st)
    st.fix = GpsFix(point=GeoPoint(58.95, 18.0))  # far from spot 0
    m.update(st, 0.2)
    assert st.work_holding is False               # still travelling
    _park_on(st, 0)
    m.update(st, 0.2)
    assert st.work_holding is True and st.active_waypoint == 0


def test_manual_advance_waits_for_the_button():
    m = WorkAreaMode(WorkAreaConfig(advance="manual"))
    st = _state(); m.activate(st)
    _park_on(st, 0); m.update(st, 0.2)            # hold spot 0
    for _ in range(10):
        m.update(st, 0.2)
    assert st.active_waypoint == 0 and st.work_holding  # no auto-advance
    st.work_next_requested = True
    m.update(st, 0.2)
    assert st.active_waypoint == 1                # the button advanced


def test_timed_advance_after_dwell():
    m = WorkAreaMode(WorkAreaConfig(advance="timed", dwell_s=1.0))
    st = _state(); m.activate(st)
    _park_on(st, 0); m.update(st, 0.2)            # begin hold (dwell resets)
    for _ in range(6):                            # 6 x 0.2s > 1.0s dwell
        m.update(st, 0.2)
    assert st.active_waypoint == 1                # auto-advanced


def test_loop_cycles_back_to_first():
    m = WorkAreaMode(WorkAreaConfig(advance="manual"))
    st = _state(n=3); st.route_loop = True; m.activate(st)
    for idx in (0, 1, 2):
        _park_on(st, idx); m.update(st, 0.2)
        assert st.work_holding
        st.work_next_requested = True; m.update(st, 0.2)
    assert st.active_waypoint == 0                # wrapped


def test_holds_final_spot_when_route_done():
    m = WorkAreaMode(WorkAreaConfig(advance="manual"))
    st = _state(n=2); m.activate(st)
    _park_on(st, 0); m.update(st, 0.2)
    st.work_next_requested = True; m.update(st, 0.2)   # -> spot 1
    _park_on(st, 1); m.update(st, 0.2)                 # hold spot 1 (last)
    st.work_next_requested = True; m.update(st, 0.2)   # button at last -> done
    assert st.route_complete is True
    assert st.work_holding is True and st.active_waypoint == 1


def test_orients_to_per_spot_heading_on_station():
    m = WorkAreaMode(WorkAreaConfig(advance="manual"))
    st = _state(headings=[123.0, None, None]); m.activate(st)
    _park_on(st, 0)
    m.update(st, 0.2)                # arrival -> begin hold
    sp = m.update(st, 0.2)           # on station, heading set -> orient
    assert isinstance(sp, GuidedSetpoint)
    assert abs(sp.target_heading - 123.0) < 1e-6


# ---- controller command wiring ------------------------------------------- #
def _ctl():
    st = NavigationState()
    return Controller(st, SimMotorController()), st


def test_work_area_command_sets_mode_spots_headings_and_config():
    ctl, st = _ctl()
    ctl.handle_command({"type": "work_area", "advance": "timed", "dwell_s": 30,
                        "waypoints": [
                            {"name": "A", "lat": 59.0, "lon": 18.0, "heading": 90},
                            {"name": "B", "lat": 59.002, "lon": 18.0}]})
    assert st.mode == ControlModeName.WORK_AREA
    assert len(st.waypoints) == 2
    assert st.waypoints[0].heading == 90.0 and st.waypoints[1].heading is None
    cfg = ctl.modes[ControlModeName.WORK_AREA].config
    assert cfg.advance == "timed" and cfg.dwell_s == 30.0


def test_next_spot_command_sets_flag():
    ctl, st = _ctl()
    ctl.handle_command({"type": "next_spot"})
    assert st.work_next_requested is True


# ---- Work Area spot generator (flavor C: draw area -> grid of spots) ------ #
def test_plan_work_spots_grid_inside_area():
    from vanchor.nav.survey import plan_work_spots
    poly = [[59.000, 18.000], [59.002, 18.000], [59.002, 18.003], [59.000, 18.003]]
    r = plan_work_spots(poly, spacing_m=50.0)
    assert r.ok and len(r.waypoints) >= 4
    for w in r.waypoints:
        assert 59.0 <= w["lat"] <= 59.002 and 18.0 <= w["lon"] <= 18.003


def test_plan_work_spots_rejects_too_many():
    from vanchor.nav.survey import plan_work_spots
    poly = [[59.0, 18.0], [59.02, 18.0], [59.02, 18.02], [59.0, 18.02]]  # ~2 km
    r = plan_work_spots(poly, spacing_m=1.0)  # would be tens of thousands
    assert not r.ok and "spacing" in r.message.lower() or "too many" in r.message.lower()


def test_plan_work_spots_needs_three_points():
    from vanchor.nav.survey import plan_work_spots
    assert plan_work_spots([[59.0, 18.0], [59.1, 18.0]], 50.0).ok is False
