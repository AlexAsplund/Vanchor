"""Tests for the four nav-control backend features (#45, #49, #50, #54):

* GPS offset calibration in the navigator,
* throttle % override for guided modes,
* pause / resume / stop navigation,
* cancellable route planning.
"""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.controller.controller import Controller
from vanchor.core.models import (
    ControlModeName,
    GeoPoint,
    GpsFix,
    GuidedSetpoint,
    ManualSetpoint,
    MotorCommand,
)
from vanchor.core.state import NavigationState
from vanchor.nav import nmea, routing, water
from vanchor.nav.navigator import Navigator
from vanchor.sim.devices import SimMotorController

FIXTURE = Path(__file__).parent / "data" / "water_sim.geojson"


@pytest.fixture()
def client():
    app = create_app_runtime()
    with TestClient(app) as c:
        yield c


def create_app_runtime():
    from vanchor.ui.server import create_app

    return create_app(Runtime())


def _controller_at(point: GeoPoint, heading: float = 0.0) -> Controller:
    state = NavigationState()
    state.fix = GpsFix(point=point)
    state.heading_deg = heading
    ctrl = Controller(state, SimMotorController(), bus=None)
    return ctrl


def _settle(ctrl: Controller, dt: float = 0.2, ticks: int = 60):
    """Tick the control loop with a fresh GPS fix each step so the safety
    governor's loss-of-fix failsafe never fires and the thrust slew settles."""
    cmd = ctrl.state.motor_command
    for _ in range(ticks):
        ctrl.state.fix_seq += 1  # keep the fix "fresh"
        cmd = ctrl.control_tick(dt)
    return cmd


# --------------------------------------------------------------------------- #
# #45 GPS offset calibration
# --------------------------------------------------------------------------- #
def test_gps_offset_shifts_reported_position():
    state = NavigationState()
    nav = Navigator(state, bus=None)
    nav.handle_sentence(nmea.encode_rmc(GeoPoint(59.0, 18.0), sog_knots=1, cog_deg=0))
    assert state.position.lat == pytest.approx(59.0, abs=1e-4)

    # Correct so the current fix should read (59.001, 18.002).
    nav.set_gps_offset(59.001, 18.002)
    assert nav.gps_offset_active
    assert nav.gps_dlat == pytest.approx(0.001, abs=1e-6)
    assert nav.gps_dlon == pytest.approx(0.002, abs=1e-6)

    # The current fix is corrected immediately on the next fix and shifted by Δ.
    nav.handle_sentence(nmea.encode_rmc(GeoPoint(59.0, 18.0), sog_knots=1, cog_deg=0))
    assert state.position.lat == pytest.approx(59.001, abs=1e-5)
    assert state.position.lon == pytest.approx(18.002, abs=1e-5)


def test_gps_offset_applies_to_subsequent_fixes_by_delta():
    state = NavigationState()
    nav = Navigator(state, bus=None)
    nav.handle_sentence(nmea.encode_rmc(GeoPoint(59.0, 18.0), sog_knots=1, cog_deg=0))
    nav.set_gps_offset(59.00005, 18.00005)  # Δ = (5e-5, 5e-5)

    # A new raw fix a small step away is shifted by the same delta.
    nav.handle_sentence(nmea.encode_rmc(GeoPoint(59.0001, 18.0001), sog_knots=1, cog_deg=0))
    assert state.position.lat == pytest.approx(59.0001 + 5e-5, abs=1e-6)
    assert state.position.lon == pytest.approx(18.0001 + 5e-5, abs=1e-6)


def test_gps_offset_clear_restores_raw():
    state = NavigationState()
    nav = Navigator(state, bus=None)
    nav.handle_sentence(nmea.encode_rmc(GeoPoint(59.0, 18.0), sog_knots=1, cog_deg=0))
    nav.set_gps_offset(59.001, 18.001)
    nav.clear_gps_offset()
    assert not nav.gps_offset_active
    nav.handle_sentence(nmea.encode_rmc(GeoPoint(59.0, 18.0), sog_knots=1, cog_deg=0))
    assert state.position.lat == pytest.approx(59.0, abs=1e-5)
    assert state.position.lon == pytest.approx(18.0, abs=1e-5)


def test_gps_offset_telemetry_and_commands(client):
    client.post(
        "/api/command",
        json={"type": "inject_nmea", "sentence": nmea.encode_rmc(GeoPoint(59.0, 18.0), 1, 0)},
    )
    # Give the bus a tick to apply the injected fix.
    for _ in range(5):
        client.get("/api/state")
    client.post(
        "/api/command", json={"type": "set_gps_offset", "true_lat": 59.0, "true_lon": 18.0}
    )
    off = client.get("/api/state").json()["gps_offset"]
    assert set(off) == {"dlat", "dlon", "active"}

    client.post("/api/command", json={"type": "clear_gps_offset"})
    off = client.get("/api/state").json()["gps_offset"]
    assert off["active"] is False
    assert off["dlat"] == 0.0 and off["dlon"] == 0.0


# --------------------------------------------------------------------------- #
# #49 throttle % override
# --------------------------------------------------------------------------- #
def test_throttle_override_tracks_percent_for_heading_hold():
    ctrl = _controller_at(GeoPoint(59.0, 18.0), heading=90.0)
    # Heading-hold with a built-in throttle of 0.4.
    ctrl.modes[ControlModeName.HEADING_HOLD].throttle = 0.4
    ctrl.handle_command({"type": "heading_hold", "heading": 90.0})

    ctrl.handle_command({"type": "set_throttle", "percent": 75})
    assert ctrl.throttle_override == pytest.approx(0.75)
    # Let the safety thrust-slew settle to the commanded magnitude.
    cmd = _settle(ctrl)
    # On-heading => steering small; thrust magnitude tracks the override.
    assert abs(cmd.thrust) == pytest.approx(0.75, abs=0.02)


def test_throttle_override_cleared_restores_default():
    ctrl = _controller_at(GeoPoint(59.0, 18.0), heading=90.0)
    ctrl.modes[ControlModeName.HEADING_HOLD].throttle = 0.4
    ctrl.handle_command({"type": "heading_hold", "heading": 90.0})
    ctrl.handle_command({"type": "set_throttle", "percent": 75})
    _settle(ctrl)
    ctrl.handle_command({"type": "set_throttle", "percent": None})
    assert ctrl.throttle_override is None
    cmd = _settle(ctrl)
    assert abs(cmd.thrust) == pytest.approx(0.4, abs=0.02)


def test_throttle_override_zero_clears():
    ctrl = _controller_at(GeoPoint(59.0, 18.0))
    ctrl.handle_command({"type": "set_throttle", "percent": 50})
    assert ctrl.throttle_override == pytest.approx(0.5)
    ctrl.handle_command({"type": "set_throttle", "percent": 0})
    assert ctrl.throttle_override is None


def test_throttle_override_does_not_affect_manual():
    ctrl = _controller_at(GeoPoint(59.0, 18.0))
    ctrl.handle_command({"type": "set_throttle", "percent": 80})
    ctrl.handle_command({"type": "manual", "thrust": 0.3, "steering": 0.0})
    # Settle past the safety thrust-slew; manual must reach exactly 0.3, NOT 0.8.
    cmd = _settle(ctrl)
    assert cmd.thrust == pytest.approx(0.3, abs=1e-6)


def test_throttle_override_telemetry(client):
    client.post("/api/command", json={"type": "set_throttle", "percent": 60})
    t = client.get("/api/state").json()["throttle_override"]
    assert t["active"] is True
    assert t["percent"] == pytest.approx(60.0)
    client.post("/api/command", json={"type": "set_throttle", "percent": None})
    t = client.get("/api/state").json()["throttle_override"]
    assert t["active"] is False


# --------------------------------------------------------------------------- #
# #50 pause / resume / stop
# --------------------------------------------------------------------------- #
def _waypoint_controller() -> Controller:
    ctrl = _controller_at(GeoPoint(59.0, 18.0))
    ctrl.handle_command(
        {
            "type": "goto",
            "waypoints": [
                {"lat": 59.01, "lon": 18.0},
                {"lat": 59.02, "lon": 18.0},
            ],
        }
    )
    ctrl.state.active_waypoint = 1  # pretend we advanced a leg
    return ctrl


def test_pause_waypoint_stores_and_switches_to_anchor_hold():
    ctrl = _waypoint_controller()
    ctrl.handle_command({"type": "pause_nav"})
    assert ctrl.state.mode == ControlModeName.ANCHOR_HOLD
    assert ctrl.suspended is not None
    assert ctrl.suspended["mode"] == ControlModeName.WAYPOINT
    assert ctrl.suspended["active_waypoint"] == 1
    assert len(ctrl.suspended["waypoints"]) == 2
    # Holds at the current position.
    assert ctrl.state.anchor == GeoPoint(59.0, 18.0)


def test_resume_restores_waypoint_mode_and_params():
    ctrl = _waypoint_controller()
    wps_before = list(ctrl.state.waypoints)
    ctrl.handle_command({"type": "pause_nav"})
    ctrl.handle_command({"type": "resume_nav"})
    assert ctrl.state.mode == ControlModeName.WAYPOINT
    assert ctrl.suspended is None
    assert ctrl.state.active_waypoint == 1
    assert [w.point for w in ctrl.state.waypoints] == [w.point for w in wps_before]


def test_resume_with_nothing_suspended_is_noop():
    ctrl = _controller_at(GeoPoint(59.0, 18.0))
    ctrl.handle_command({"type": "manual", "thrust": 0.0, "steering": 0.0})
    ctrl.handle_command({"type": "resume_nav"})
    assert ctrl.state.mode == ControlModeName.MANUAL
    assert ctrl.suspended is None


def test_stop_clears_suspended():
    ctrl = _waypoint_controller()
    ctrl.handle_command({"type": "pause_nav"})
    assert ctrl.suspended is not None
    ctrl.handle_command({"type": "stop"})
    assert ctrl.suspended is None
    assert ctrl.state.mode == ControlModeName.MANUAL


def test_pause_preserves_throttle_override():
    ctrl = _waypoint_controller()
    ctrl.handle_command({"type": "set_throttle", "percent": 55})
    ctrl.handle_command({"type": "pause_nav"})
    # Anchor hold should not depend on the override; resume restores it.
    ctrl.handle_command({"type": "resume_nav"})
    assert ctrl.throttle_override == pytest.approx(0.55)


def test_nav_telemetry(client):
    client.post(
        "/api/command",
        json={"type": "goto", "waypoints": [{"lat": 59.4, "lon": 18.1}]},
    )
    client.post("/api/command", json={"type": "pause_nav"})
    nav = client.get("/api/state").json()["nav"]
    assert nav["paused"] is True
    assert nav["suspended_mode"] == "waypoint"
    client.post("/api/command", json={"type": "resume_nav"})
    nav = client.get("/api/state").json()["nav"]
    assert nav["paused"] is False
    assert nav["suspended_mode"] is None


# --------------------------------------------------------------------------- #
# #54 cancellable route planning
# --------------------------------------------------------------------------- #
def test_plan_route_cancelled_returns_cancelled_result():
    poly = water.load_geojson(FIXTURE)
    from vanchor.core.config import SimConfig

    cfg = SimConfig()
    result = routing.plan_route(
        start_lat=cfg.start_lat,
        start_lon=cfg.start_lon,
        dest_lat=59.66430488913581,
        dest_lon=13.368675408442506,
        water_ll=poly,
        mode="fastest",
        cancelled=lambda: True,
    )
    assert result.ok is False
    assert result.waypoints == []
    assert result.message == "Route planning cancelled."


def test_runtime_cancel_flag_makes_plan_cancel():
    rt = Runtime()
    rt._route_plan_cancelled = False
    rt.cancel_route_plan()
    assert rt._route_plan_cancelled is True
    # plan_route resets the flag at the start, so to observe cancellation we set
    # it via a monkeypatched plan that re-asserts after reset; simplest: call the
    # planner path directly through routing with the runtime predicate.
    poly = water.load_geojson(FIXTURE)
    from vanchor.core.config import SimConfig

    cfg = SimConfig()
    rt._route_plan_cancelled = True
    result = routing.plan_route(
        start_lat=cfg.start_lat,
        start_lon=cfg.start_lon,
        dest_lat=59.66430488913581,
        dest_lon=13.368675408442506,
        water_ll=poly,
        cancelled=lambda: rt._route_plan_cancelled,
    )
    assert result.ok is False and "cancelled" in result.message.lower()


def test_route_plan_cancel_endpoint(client):
    r = client.post("/api/route/plan/cancel")
    assert r.json() == {"cancelled": True}


# --------------------------------------------------------------------------- #
# Review fixes: supervised control loop, cruise sign, no-reset-on-resend
# --------------------------------------------------------------------------- #
class _BoomMode:
    """A mode whose update() raises until told to heal."""

    name = ControlModeName.MANUAL

    def __init__(self) -> None:
        self.raising = True

    def activate(self, state) -> None:
        pass

    def update(self, state, dt):
        if self.raising:
            raise RuntimeError("boom")
        return ManualSetpoint(0.0, 0.0)


async def test_control_loop_survives_faulting_tick_and_zeroes_motor():
    ctrl = _controller_at(GeoPoint(59.0, 18.0))
    boom = _BoomMode()
    ctrl.modes[ControlModeName.MANUAL] = boom
    ctrl.state.mode = ControlModeName.MANUAL
    # Pretend the boat was driving hard when the fault hits.
    running = MotorCommand(thrust=0.7, steering=0.2)
    ctrl.state.motor_command = running
    ctrl.motor.apply(running)

    await ctrl._tick_once(0.2)  # faulting tick
    assert ctrl.state.controller_fault is not None
    # The motor was zeroed (STOP always works) rather than left running.
    assert ctrl.motor.command.thrust == 0.0
    assert ctrl.motor.command.steering == 0.0
    assert ctrl.state.motor_command.thrust == 0.0

    # A subsequent healthy tick clears the fault and the loop keeps running.
    boom.raising = False
    await ctrl._tick_once(0.2)
    assert ctrl.state.controller_fault is None


async def test_control_loop_survives_faulting_motor():
    # Even if the motor itself throws on apply(), the tick must not propagate.
    class _BadMotor:
        def apply(self, command):
            raise RuntimeError("motor down")

        async def flush(self):
            raise RuntimeError("flush down")

    state = NavigationState()
    state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    ctrl = Controller(state, _BadMotor(), bus=None)
    await ctrl._tick_once(0.2)  # must not raise
    assert ctrl.state.controller_fault is not None


async def test_run_loop_updates_heartbeat_and_stops_cleanly():
    ctrl = _controller_at(GeoPoint(59.0, 18.0))
    ctrl.tick_hz = 200.0
    task = asyncio.create_task(ctrl.run())
    try:
        await asyncio.sleep(0.05)
        assert ctrl.state.controller_last_tick_monotonic > 0.0
    finally:
        ctrl.stop()
        await asyncio.wait_for(task, timeout=1.0)


def test_cruise_preserves_reverse_setpoint_sign():
    # Cruise's SOG PID is unsigned (forward-only). A reverse setpoint (e.g. a
    # WaypointMode backing toward a mark that's close behind) must stay negative,
    # not be flipped forward and drive the boat away from the mark.
    ctrl = _controller_at(GeoPoint(59.0, 18.0))
    ctrl.state.mode = ControlModeName.WAYPOINT
    ctrl.cruise_knots = 1.0
    ctrl.cruise_pid.setpoint = 1.0
    ctrl.state.sog_knots = 0.0  # far below target -> PID wants full forward drive
    reverse_sp = GuidedSetpoint(target_heading=200.0, thrust=-0.6)
    out = ctrl._apply_cruise(reverse_sp, dt=0.2)
    assert out.thrust < 0.0


def test_resend_same_mode_does_not_reset_governor_slew():
    # Re-sending {"type":"manual"} (a remote-helm button re-press) must NOT reset
    # the governor: a reset would zero the slew anchor and re-ramp the prop from 0.
    ctrl = _controller_at(GeoPoint(59.0, 18.0))
    ctrl.handle_command({"type": "manual", "thrust": 0.8, "steering": 0.0})
    cmd = _settle(ctrl)  # let thrust slew up to ~0.8
    assert cmd.thrust == pytest.approx(0.8, abs=0.02)
    # Re-send the SAME manual command; thrust must stay put, not dip toward 0.
    ctrl.handle_command({"type": "manual", "thrust": 0.8, "steering": 0.0})
    ctrl.state.fix_seq += 1
    cmd = ctrl.control_tick(0.2)
    assert cmd.thrust == pytest.approx(0.8, abs=0.02)


# --------------------------------------------------------------------------- #
# Fix 1: handle_command malformed-payload robustness
# --------------------------------------------------------------------------- #
def test_handle_command_bad_heading_no_exception_mode_unchanged():
    # A non-numeric heading value must not propagate a ValueError up; the mode
    # must stay MANUAL (no partial mode switch after a parse error).
    ctrl = _controller_at(GeoPoint(59.0, 18.0))
    ctrl.handle_command({"type": "heading_hold", "heading": "abc"})  # ValueError
    assert ctrl.state.mode == ControlModeName.MANUAL


def test_handle_command_goto_no_waypoints_no_exception():
    # An empty / missing waypoints list must not raise.
    ctrl = _controller_at(GeoPoint(59.0, 18.0))
    ctrl.handle_command({"type": "goto"})  # no exception


def test_handle_command_goto_malformed_waypoint_no_exception():
    # A waypoint dict missing lat/lon must not propagate a KeyError.
    ctrl = _controller_at(GeoPoint(59.0, 18.0))
    ctrl.handle_command({"type": "goto", "waypoints": [{"name": "bad"}]})  # KeyError
    assert ctrl.state.mode == ControlModeName.MANUAL  # mode not switched


def test_handle_command_jog_no_anchor_no_exception():
    # Jog with no anchor set returns early without error; mode stays MANUAL.
    ctrl = _controller_at(GeoPoint(59.0, 18.0))
    ctrl.handle_command({"type": "jog"})
    assert ctrl.state.mode == ControlModeName.MANUAL


def test_gps_offset_on_sim_gps_teleports_instead(client):
    """Field report: with a GPS offset active in the SIM, chart-relative modes
    (contour follow etc.) ran displaced by the offset — the sim sounder samples
    TRUTH, so biasing the perceived frame away from truth breaks alignment.
    On a simulated GPS, "adjust my position" therefore MOVES the boat and
    installs NO offset."""
    client.post("/api/command",
                json={"type": "set_gps_offset", "true_lat": 59.1, "true_lon": 18.1})
    st = client.get("/api/state").json()
    assert st["gps_offset"]["active"] is False          # no lying offset in sim
    truth = st.get("truth")
    assert truth and abs(truth["lat"] - 59.1) < 1e-6 and abs(truth["lon"] - 18.1) < 1e-6
