"""The navigator wires GNSS/INS fusion additively: it fills the state.fusion_*
fields when enabled, and changes nothing when disabled (non-blocking)."""
import asyncio

from vanchor.core.models import GeoPoint, GpsFix, ImuSample
from vanchor.core.state import NavigationState
from vanchor.nav import nmea
from vanchor.nav.fusion import NavFusion
from vanchor.nav.navigator import Navigator


def _nav(with_fusion: bool):
    clock = [100.0]
    st = NavigationState()
    nav = Navigator(st, bus=None, mono_fn=lambda: clock[0],
                    fusion=NavFusion() if with_fusion else None)
    return st, nav, clock


def test_fusion_populates_yawrate_velocity_and_crab():
    st, nav, _ = _nav(True)
    nav.handle_sentence(nmea.encode_hdt(90.0))            # bow points east
    asyncio.new_event_loop().run_until_complete(nav._on_imu(ImuSample(gz=3.5, source="t")))
    nav.handle_sentence(nmea.encode_rmc(GeoPoint(59.0, 18.0), sog_knots=4.0, cog_deg=95.0))
    assert st.yaw_rate_dps == 3.5                         # straight from the gyro
    assert st.ground_vel_e_mps is not None and st.ground_vel_e_mps > 1.0  # moving ~east
    assert st.crab_deg is not None and abs(st.crab_deg - 5.0) < 1.0       # course-heading


def test_ubx_fix_velocity_vector_flows_through_fusion():
    st, nav, _ = _nav(True)
    nav.handle_sentence(nmea.encode_hdt(90.0))
    fix = GpsFix(point=GeoPoint(59.0, 18.0), sog_knots=4.0, cog_deg=95.0,
                 vel_n_mps=-0.18, vel_e_mps=2.05, valid=True)
    asyncio.new_event_loop().run_until_complete(nav._on_gps_fix(fix))
    assert st.fix.vel_e_mps == 2.05                       # the rich fix is kept
    assert abs(st.ground_vel_e_mps - 2.05) < 0.01         # fusion used the vector


def test_gpsfix_capability_flags_are_source_agnostic():
    plain = GpsFix(point=GeoPoint(59.0, 18.0), sog_knots=1.0, cog_deg=10.0)
    assert not plain.has_velocity and not plain.has_3d_velocity and not plain.has_accuracy
    rich = GpsFix(point=GeoPoint(59.0, 18.0), vel_n_mps=1.0, vel_e_mps=0.5,
                  vel_d_mps=0.0, h_acc_m=0.5)
    assert rich.has_velocity and rich.has_3d_velocity and rich.has_accuracy


def test_sim_gps_velocity_activates_enhanced_path_from_a_non_ublox_source():
    """The generalization: a velocity-carrying fix from the SIM (a different
    source, no UBX driver) activates the same capability-gated fusion features."""
    from vanchor.core.models import BoatState
    from vanchor.sim.devices import SimGps
    truth = BoatState(point=GeoPoint(59.0, 18.0), heading_deg=90.0,
                      ground_vn=-0.18, ground_ve=2.05)
    sim = SimGps(lambda: truth, bus=None, emit_velocity=True, position_noise_m=0.0)
    fix = sim.sample_fix()
    assert fix.has_velocity and fix.has_3d_velocity  # sim now carries a real vector

    st, nav, _ = _nav(True)
    nav.handle_sentence(nmea.encode_hdt(90.0))
    asyncio.new_event_loop().run_until_complete(nav._on_gps_fix(fix))
    assert st.velocity_measured is True                # enhanced mode active
    assert abs(st.ground_vel_e_mps - 2.05) < 0.01
    assert st.crab_deg is not None                     # crab available at this speed


def test_no_fusion_leaves_everything_unchanged():
    # The whole point: without a fusion filter, the additive fields stay None and
    # the existing pipeline is untouched (every other hardware combo is safe).
    st, nav, _ = _nav(False)
    nav.handle_sentence(nmea.encode_hdt(90.0))
    asyncio.new_event_loop().run_until_complete(nav._on_imu(ImuSample(gz=3.5, source="t")))
    nav.handle_sentence(nmea.encode_rmc(GeoPoint(59.0, 18.0), sog_knots=4.0, cog_deg=95.0))
    assert st.yaw_rate_dps is None
    assert st.ground_vel_n_mps is None and st.ground_vel_e_mps is None
    assert st.crab_deg is None
    assert st.dead_reckoning is False
    assert st.heading_deg == 90.0                          # heading path unchanged
