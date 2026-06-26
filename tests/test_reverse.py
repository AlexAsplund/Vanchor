"""Reverse-thrust is less effective than forward (trolling-motor prop)."""

from vanchor.core.models import BoatState, Environment, GeoPoint, MotorCommand


def _terminal_speed(boat, thrust):
    for _ in range(240):
        boat.step(0.05, MotorCommand(thrust=thrust, steering=0.0), Environment())
    return abs(boat.state.speed_mps)


def test_fossen_reverse_weaker_than_forward():
    from vanchor.sim.fossen import FossenBoat, FossenParams

    p = FossenParams(reverse_efficiency=0.6)
    fwd = _terminal_speed(FossenBoat(BoatState(point=GeoPoint(59.0, 13.0)), p), 1.0)
    rev = _terminal_speed(FossenBoat(BoatState(point=GeoPoint(59.0, 13.0)), p), -1.0)
    assert fwd > 0.5
    assert rev < fwd * 0.85  # noticeably weaker astern


def test_simple_model_reverse_weaker():
    from vanchor.sim.boat import Boat, BoatParams

    p = BoatParams(reverse_efficiency=0.6)
    fwd = _terminal_speed(Boat(BoatState(point=GeoPoint(59.0, 13.0)), p), 1.0)
    rev = _terminal_speed(Boat(BoatState(point=GeoPoint(59.0, 13.0)), p), -1.0)
    assert abs(rev - 0.6 * fwd) < 0.05 * fwd  # ~0.6x forward


# --- forward/reverse manoeuvre decision (modes.maneuver_to_bearing) --------- #

from vanchor.controller.modes import maneuver_to_bearing


def test_maneuver_close_behind_reverses():
    # target directly behind (boat heading north, mark to the south), near -> back up
    _, sign, rev = maneuver_to_bearing(
        0.0, 180.0, 15.0, turn_rate_dps=18.0, fwd_speed_mps=1.6, reverse_efficiency=0.6
    )
    assert rev is True and sign == -1.0


def test_maneuver_far_behind_goes_forward():
    # same bearing but far -> the reverse speed penalty outweighs the 180° turn
    _, sign, rev = maneuver_to_bearing(
        0.0, 180.0, 200.0, turn_rate_dps=18.0, fwd_speed_mps=1.6, reverse_efficiency=0.6
    )
    assert rev is False and sign == 1.0


def test_maneuver_ahead_forward():
    th, sign, rev = maneuver_to_bearing(
        0.0, 10.0, 15.0, turn_rate_dps=18.0, fwd_speed_mps=1.6, reverse_efficiency=0.6
    )
    assert rev is False and sign == 1.0 and round(th) == 10


def test_maneuver_sluggish_hull_reverses_farther():
    # a slow-turning hull (keelboat) finds reverse worthwhile at a longer distance
    far = 40.0  # between snappy(~14m) and sluggish(~72m) crossovers
    _, _, snappy = maneuver_to_bearing(
        0.0, 180.0, far, turn_rate_dps=30.0, fwd_speed_mps=1.6, reverse_efficiency=0.6
    )
    _, _, sluggish = maneuver_to_bearing(
        0.0, 180.0, far, turn_rate_dps=6.0, fwd_speed_mps=1.6, reverse_efficiency=0.6
    )
    assert sluggish is True and snappy is False


def test_waypoint_reverses_for_close_mark_behind():
    """A close mark behind the boat is reached by backing up, not a 180° spin —
    and that is faster than forcing forward-only."""
    import sys; sys.path.insert(0, "tests")
    from harness import Harness, STOCKHOLM
    from vanchor.core.models import Environment
    from vanchor.core.state import ControlModeName
    from vanchor.core.geo import haversine_m, offset_meters

    def drive(allow_reverse):
        h = Harness(model="fossen", environment=Environment())
        wp = h.controller.modes.get(ControlModeName.WAYPOINT)
        wp.config.allow_reverse = allow_reverse
        target = offset_meters(STOCKHOLM, 0.0, -15.0)  # 15 m due south (behind heading 0)
        h.command({"type": "goto", "waypoints": [{"lat": target.lat, "lon": target.lon}]})
        t = 0.0; ng = nc = nk = 0.0; reached = None; used_rev = False
        while t < 90:
            h.sim.step(h.physics_dt)
            if t >= ng: h.nav.handle_sentence(h.gps.sample(h.sim.truth())); ng += 1.0
            if t >= nc: h.nav.handle_sentence(h.compass.sample(h.sim.truth())); nc += 0.2
            if t >= nk:
                h.controller.control_tick(0.2); nk += 0.2
                used_rev = used_rev or wp._reverse
                if reached is None and haversine_m(h.sim.truth().point, target) <= wp.config.arrival_radius_m:
                    reached = t
            t += h.physics_dt
        return reached, used_rev

    rev_t, used_rev = drive(True)
    fwd_t, _ = drive(False)
    assert used_rev is True and rev_t is not None      # it backed up and arrived
    assert rev_t < fwd_t                                # ... faster than turning around
