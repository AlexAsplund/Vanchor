"""Integration tests for the roadmap features: Fossen physics, FollowAPB mode,
the safety governor in the loop, GPX route loading, and safety telemetry."""

import pytest

from vanchor.core.geo import angle_difference, destination_point, haversine_m
from vanchor.core.models import ControlModeName, Environment, GeoPoint
from vanchor.nav import nmea

from .harness import STOCKHOLM, Harness


def test_anchor_hold_converges_with_fossen_model():
    env = Environment(current_speed=0.3, current_dir=90.0, wind_speed=3.0, wind_dir=120.0)
    h = Harness(environment=env, model="fossen")
    assert h.sim.model == "fossen"
    h.command({"type": "anchor_hold", "radius_m": 6.0})
    distances = h.run(seconds=300)
    settled = distances[-200:]
    assert max(settled) < 10.0, f"fossen drift: max={max(settled):.1f}m"
    assert sum(settled) / len(settled) < 7.0


def test_follow_apb_steers_to_bearing():
    h = Harness(environment=Environment())
    # Inject an APB telling the boat to steer to bearing ~090 with no XTE.
    h.nav.handle_sentence(
        nmea.encode_apb(0.0, "L", 90.0, dest_id="EXT", arrived=False)
    )
    h.command({"type": "follow_apb", "throttle": 0.5})
    h.run(seconds=60)
    assert h.state.mode == ControlModeName.FOLLOW_APB
    assert h.sim.truth().heading_deg == pytest.approx(90.0, abs=8.0)


def test_loss_of_fix_failsafe_stops_motor():
    # Manual full ahead, but starve the controller of GPS fixes: with the
    # loss-of-fix failsafe ENABLED, the safety governor must cut thrust once the
    # fix goes stale. (The failsafe is OFF by default now; turn it on here.)
    h = Harness(environment=Environment(), gps_hz=1.0)
    h.controller.safety.config.fix_failsafe_enabled = True
    h.command({"type": "manual", "thrust": 1.0, "steering": 0.0})
    # Run the control + physics loop WITHOUT feeding any new GPS fixes.
    dt = 0.05
    t = 0.0
    ctrl_dt = 1.0 / h.control_hz
    next_ctrl = 0.0
    while t < 10.0:  # > default fix_timeout_s
        h.sim.step(dt)
        if t >= next_ctrl:
            h.controller.control_tick(ctrl_dt)
            next_ctrl += ctrl_dt
        t += dt
    assert h.controller.safety_status.fix_lost is True
    assert h.state.motor_command.thrust == pytest.approx(0.0, abs=1e-6)


def test_anchor_tight_radius_uses_reverse_and_does_not_orbit():
    # The reported bug: a small radius made the boat loop in circles bigger than
    # the radius because it could only drive forward. It must now brake/reverse
    # and hover near the mark.
    h = Harness(environment=Environment(current_speed=0.1, current_dir=90.0), model="simple")
    h.command({"type": "anchor_hold", "radius_m": 1.0})
    anchor = h.state.anchor
    h.sim.boat.state.point = destination_point(anchor, 8.0, 0.0)  # displaced 8 m

    dt = 0.05
    ctrl_dt = 1.0 / h.control_hz
    t = next_gps = next_compass = next_ctrl = 0.0
    min_thrust = 1.0
    tail: list[float] = []
    while t < 200.0:
        h.sim.step(dt)
        if t >= next_gps:
            h.nav.handle_sentence(h.gps.sample(h.sim.truth())); next_gps += 1.0 / h.gps_hz
        if t >= next_compass:
            h.nav.handle_sentence(h.compass.sample(h.sim.truth())); next_compass += 1.0 / h.compass_hz
        if t >= next_ctrl:
            h.controller.control_tick(ctrl_dt); next_ctrl += ctrl_dt
        min_thrust = min(min_thrust, h.state.motor_command.thrust)
        if t > 140.0:
            tail.append(haversine_m(h.sim.truth().point, anchor))
        t += dt

    assert min_thrust < -0.02, "anchor hold should use reverse thrust to brake/hold"
    assert max(tail) < 4.0, f"should hover near anchor, not orbit (max {max(tail):.1f} m)"
    assert sum(tail) / len(tail) < 2.5


def test_helm_freezes_steering_without_thrust():
    from vanchor.controller.controller import Helm
    from vanchor.core.models import GpsFix, GuidedSetpoint
    from vanchor.core.state import NavigationState

    helm = Helm(steer_tau=0.0)  # disable smoothing to test the hold directly
    state = NavigationState()
    state.fix = GpsFix(point=STOCKHOLM)
    state.heading_deg = 0.0
    # With real thrust the helm steers toward a starboard target.
    moving = helm.compute(GuidedSetpoint(target_heading=90.0, thrust=0.5), state, 0.2)
    assert moving.steering > 0
    # With ~zero thrust it must NOT chase heading: even though the target is now
    # to PORT, it holds the previous (starboard) command instead of jittering.
    idle = helm.compute(GuidedSetpoint(target_heading=270.0, thrust=0.0), state, 0.2)
    assert idle.steering == moving.steering  # held, not recomputed for the new target


def test_drift_estimator_learns_the_drift():
    # Under a steady current the anchor controller should estimate its speed and
    # direction (the basis of the predictive feed-forward).
    h = Harness(environment=Environment(current_speed=0.4, current_dir=90.0))
    h.command({"type": "anchor_hold", "radius_m": 5.0})
    h.run(seconds=90)
    assert h.state.est_drift_mps == pytest.approx(0.4, abs=0.2)
    assert abs(angle_difference(h.state.est_drift_dir, 90.0)) < 30.0  # pushes east


def test_anchor_anticipates_drift_and_holds_tight():
    # Wind and current are now true aerodynamic / hydrodynamic FORCES (not a
    # kinematic position bias), so holding station means actively fighting them.
    # The controller's velocity term still anticipates the drift and keeps the
    # boat within radius nearly always -- with realistic darting as it works
    # against the real load (the old <2 m assumed disturbance-free station-keep).
    from vanchor.analysis.metrics import anchor_metrics
    from vanchor.analysis.runner import run_scenario
    from vanchor.analysis.scenarios import SCENARIOS

    m = anchor_metrics(run_scenario(SCENARIOS["anchor_drift"]))
    assert m.within_radius_pct >= 95.0
    assert m.steady_peak_to_peak_m < 4.0


def test_anchor_hold_does_not_spin_on_station():
    # On station in calm water the boat must hold its heading passively (idling =
    # no yaw) rather than spinning to chase the noisy GPS bearing.
    h = Harness(environment=Environment())  # calm
    h.sim.boat.state.heading_deg = 40.0
    h.nav.handle_sentence(h.compass.sample(h.sim.truth()))
    h.command({"type": "anchor_hold", "radius_m": 5.0})

    dt = 0.05
    ctrl_dt = 1.0 / h.control_hz
    t = next_gps = next_compass = next_ctrl = 0.0
    headings: list[float] = []
    while t < 60.0:
        h.sim.step(dt)
        if t >= next_gps:
            h.nav.handle_sentence(h.gps.sample(h.sim.truth())); next_gps += 1.0 / h.gps_hz
        if t >= next_compass:
            h.nav.handle_sentence(h.compass.sample(h.sim.truth())); next_compass += 1.0 / h.compass_hz
        if t >= next_ctrl:
            h.controller.control_tick(ctrl_dt); next_ctrl += ctrl_dt
        if t > 20.0:
            headings.append(h.sim.truth().heading_deg)
        t += dt

    # Heading should stay near its settled value (no continuous spin).
    spread = max(abs(angle_difference(headings[0], hh)) for hh in headings)
    assert spread < 45.0, f"boat is spinning on station (spread {spread:.0f} deg)"
    assert h.state.distance_to_anchor_m < 5.0


def test_safety_status_in_telemetry():
    from vanchor.app import Runtime

    rt = Runtime()
    tel = rt.telemetry()
    assert "safety" in tel
    assert set(tel["safety"]) >= {"fix_lost", "drag_alarm", "reverse_blocked", "thrust_limited"}


def test_load_route_via_runtime_sets_waypoints():
    from vanchor.app import Runtime
    from vanchor.nav.routes import serialize_gpx
    from vanchor.core.models import Waypoint

    rt = Runtime()
    wps = [
        Waypoint("A", destination_point(STOCKHOLM, 40.0, 30.0)),
        Waypoint("B", destination_point(STOCKHOLM, 80.0, 60.0)),
    ]
    gpx = serialize_gpx(wps, name="test")
    rt.handle_command({"type": "load_route", "gpx": gpx, "throttle": 0.6})
    assert rt.state.mode == ControlModeName.WAYPOINT
    assert len(rt.state.waypoints) == 2
    assert rt.state.waypoints[0].name == "A"
