"""Trolling pattern mode (#59, roadmap #32): GROUND-TRACK S-curve.

The trolling mode now traces a fixed-width lawnmower-S over the GROUND by
following a corridor of virtual waypoints laid +/- amplitude either side of a
straight base course, and tracks it with cross-track correction + crab
feed-forward -- rather than weaving the target HEADING at a held speed.

Why the rewrite (justification): the old tests pinned the *heading* to a
sinusoid ``base + amplitude*sin(2*pi*t/period)``. That behaviour was the bug --
because it steered heading, wind/current sheared the S downstream and the swath
width scaled with boat speed. The intended improvement is a fixed-width ground
track, so those heading-weave assertions no longer apply. These tests instead
assert the GROUND track weaves with a ~constant swath under a beam current, the
swath is ~speed-independent, the pattern advances along the base course, speed
is held, and memory stays bounded.

Param semantics change: ``trolling_amplitude_deg`` is now the lateral half-width
in METRES and ``trolling_period_s`` the longitudinal wavelength in METRES.
"""

import math

from vanchor.controller.modes import TrollingConfig, TrollingMode
from vanchor.core.geo import (
    angle_difference,
    cross_track,
    destination_point,
    haversine_m,
    initial_bearing,
    normalize_deg,
    offset_meters,
)
from vanchor.core.models import GeoPoint, GpsFix, GuidedSetpoint
from vanchor.core.state import NavigationState

HERE = GeoPoint(59.3293, 18.0686)


def _state(heading=0.0, pos=HERE):
    s = NavigationState()
    s.fix = GpsFix(point=pos)
    s.heading_deg = heading
    return s


def _setup(base, amplitude_m, wavelength_m, config=None):
    state = _state(heading=base)
    state.trolling_base_heading = base
    state.trolling_amplitude_deg = amplitude_m  # reinterpreted as METRES
    state.trolling_period_s = wavelength_m      # reinterpreted as METRES
    mode = TrollingMode(config)
    mode.activate(state)
    return state, mode


def _lateral_from_centerline(base, bearing, pos):
    """Signed distance of ``pos`` from the base course (+ = right/starboard)."""
    far = destination_point(base, 100_000.0, bearing)
    return cross_track(base, far, pos).distance_m


def _run(mode, state, *, base, bearing, drift_e=0.0, drift_n=0.0,
         speed=1.0, dt=0.2, steps=400, turn_rate_dps=40.0, set_estimate=True):
    """Deterministic closed-loop kinematic sim: the boat runs at ``speed`` along
    its heading plus a constant beam drift; heading slews toward the setpoint at
    ``turn_rate_dps``. Returns the list of (position, setpoint) per tick."""
    if set_estimate and (drift_e or drift_n):
        state.est_drift_east = drift_e
        state.est_drift_north = drift_n
        state.est_drift_mps = math.hypot(drift_e, drift_n)
        state.est_drift_settled = True
    trail = []
    for _ in range(steps):
        sp = mode.update(state, dt)
        err = angle_difference(state.heading_deg, sp.target_heading)
        step = max(-turn_rate_dps * dt, min(turn_rate_dps * dt, err))
        state.heading_deg = normalize_deg(state.heading_deg + step)
        hdg = math.radians(state.heading_deg)
        east = speed * math.sin(hdg) * dt + drift_e * dt
        north = speed * math.cos(hdg) * dt + drift_n * dt
        newpos = offset_meters(state.position, east, north)
        state.fix = GpsFix(point=newpos)
        trail.append((newpos, sp))
    return trail


# --------------------------------------------------------------------------- #
# Basic contract
# --------------------------------------------------------------------------- #
def test_drives_forward():
    state, mode = _setup(0.0, 15.0, 40.0)
    sp = mode.update(state, 0.2)
    assert isinstance(sp, GuidedSetpoint)
    assert sp.thrust > 0.0


def test_speed_is_held_constant():
    # The mode emits a constant thrust every tick; the controller's cruise/SOG
    # loop (unchanged) rides on top. Ground-tracking must not regress that.
    state, mode = _setup(30.0, 12.0, 30.0, TrollingConfig(throttle=0.5))
    trail = _run(mode, state, base=HERE, bearing=30.0, steps=200)
    assert all(sp.thrust == 0.5 for _, sp in trail)


def test_base_defaults_to_engaged_heading():
    # With no explicit base bearing the controller stores the live heading; the
    # corridor centreline then runs along that heading. First virtual point lies
    # ahead-and-to-one-side of it.
    state = _state(heading=137.0)
    state.trolling_base_heading = state.heading_deg
    state.trolling_amplitude_deg = 10.0
    state.trolling_period_s = 40.0
    mode = TrollingMode()
    mode.activate(state)
    assert mode._bearing == 137.0
    # First target is offset ~amplitude off the centreline (a peak of the S).
    first = mode._pending[0]
    lat = _lateral_from_centerline(HERE, 137.0, first)
    assert abs(lat) > 5.0  # clearly off the centreline


# --------------------------------------------------------------------------- #
# Corridor geometry
# --------------------------------------------------------------------------- #
def test_virtual_waypoints_form_fixed_width_corridor():
    amp, wl = 15.0, 40.0
    _, mode = _setup(0.0, amp, wl)
    # Generate a long run of points and check each sits ~amplitude off the
    # centreline, alternating sides, at the expected along-course spacing.
    pts = [mode._make_point(k) for k in range(8)]
    for k, p in enumerate(pts):
        lat = _lateral_from_centerline(HERE, 0.0, p)
        assert abs(abs(lat) - amp) < 0.5           # fixed lateral half-width
        expected_side = 1.0 if k % 2 == 0 else -1.0  # starboard first
        assert math.copysign(1.0, lat) == expected_side
        # Along-course distance = (2k+1)*wavelength/4.
        d = haversine_m(HERE, p)
        brg = initial_bearing(HERE, p)
        along = d * math.cos(math.radians(angle_difference(0.0, brg)))
        assert abs(along - (2 * k + 1) * wl / 4.0) < 0.5


# --------------------------------------------------------------------------- #
# The key win: constant swath over the ground under a beam current
# --------------------------------------------------------------------------- #
def _swath_and_center(trail, base, bearing, last_fraction=0.5):
    """Peak-to-peak lateral excursion and mean lateral offset over the tail of a
    run (skip the initial capture transient)."""
    tail = trail[int(len(trail) * (1 - last_fraction)):]
    lats = [_lateral_from_centerline(base, bearing, p) for p, _ in tail]
    return max(lats) - min(lats), sum(lats) / len(lats)


def test_ground_swath_constant_under_beam_current():
    amp, wl = 15.0, 40.0
    state, mode = _setup(0.0, amp, wl)  # base course due north
    # Strong beam current pushing EAST (perpendicular to the north base course).
    trail = _run(mode, state, base=HERE, bearing=0.0,
                 drift_e=0.3, drift_n=0.0, speed=1.0, steps=800)
    swath, center = _swath_and_center(trail, HERE, 0.0)
    # Swath ~ 2*amplitude (turn-rate limits round the peaks a little).
    assert 1.3 * amp <= swath <= 2.2 * amp
    # The whole point: the S does NOT shear downstream with the current. Pure
    # heading-weave would drift ~ drift*time = 0.3 * 160 s = ~48 m off to the
    # east; the ground-track corridor holds the centre near zero.
    assert abs(center) < amp


def test_swath_is_speed_independent():
    # Old heading weave: swath scaled with boat speed. Ground-track corridor is a
    # fixed geometry, so the swath is ~the same slow vs fast (the win for #32).
    amp, wl = 15.0, 50.0
    s_slow, m_slow = _setup(0.0, amp, wl)
    slow = _run(m_slow, s_slow, base=HERE, bearing=0.0, speed=0.7, steps=1000)
    s_fast, m_fast = _setup(0.0, amp, wl)
    fast = _run(m_fast, s_fast, base=HERE, bearing=0.0, speed=1.4, steps=1000)
    sw_slow, _ = _swath_and_center(slow, HERE, 0.0)
    sw_fast, _ = _swath_and_center(fast, HERE, 0.0)
    assert abs(sw_slow - sw_fast) < 0.5 * amp  # within ~half an amplitude


def test_pattern_advances_along_base_course():
    amp, wl = 12.0, 40.0
    state, mode = _setup(0.0, amp, wl)
    trail = _run(mode, state, base=HERE, bearing=0.0, speed=1.0, steps=600)
    final, _ = trail[-1]
    d = haversine_m(HERE, final)
    brg = initial_bearing(HERE, final)
    along = d * math.cos(math.radians(angle_difference(0.0, brg)))
    # 600 * 0.2 s * ~1 m/s ~ 120 m of path; weaving spends some of it laterally,
    # so along-course progress is a bit less but still several wavelengths.
    assert along > 1.5 * wl


# --------------------------------------------------------------------------- #
# Bounded memory over an endless run
# --------------------------------------------------------------------------- #
def test_memory_stays_bounded_over_long_run():
    cfg = TrollingConfig(lookahead_points=4)
    state, mode = _setup(0.0, 15.0, 30.0, cfg)
    _run(mode, state, base=HERE, bearing=0.0, speed=1.5, steps=2000)
    # The rolling buffer never grows; only the integer generation counter does.
    assert len(mode._pending) == 4
    assert mode._next_k > 12  # many points were generated and consumed


def test_advances_through_virtual_waypoints():
    # Sanity: the generation counter climbs as the boat progresses (points are
    # consumed and refilled), i.e. the corridor rolls forward.
    state, mode = _setup(0.0, 10.0, 24.0)
    start_k = mode._next_k
    _run(mode, state, base=HERE, bearing=0.0, speed=1.2, steps=300)
    assert mode._next_k > start_k


# --------------------------------------------------------------------------- #
# Telemetry the UI reads
# --------------------------------------------------------------------------- #
def test_phase_advances_with_progress():
    state, mode = _setup(0.0, 12.0, 40.0)
    phases = []
    trail_state = state
    for _ in range(30):
        mode.update(trail_state, 0.2)
        phases.append(mode.phase)
        # Nudge the boat forward along the base course.
        newpos = offset_meters(trail_state.position, 0.0, 1.0)
        trail_state.fix = GpsFix(point=newpos)
    assert 0.0 <= min(phases)
    assert max(phases) <= 2 * math.pi
    assert max(phases) > 0.0  # phase actually moved as we advanced
