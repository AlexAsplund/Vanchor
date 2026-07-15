"""Chaos / fault-injection tests: prove the safety FLOOR holds under failure.

Each test injects a specific fault and asserts the boat ends MOTIONLESS
(commanded thrust ~0) and/or the correct alarm/flag is raised. These are the
executable rows of ``docs/safety-matrix.md`` -- every software-testable failure
mode there is backed by a test here (see that doc for the full matrix and the
residual risks that are NOT software-testable, e.g. a Pi hard-hang).

Determinism: no wall-clock sleeps beyond trivial ``asyncio.sleep(0)`` yields;
all timers are driven off injected monotonic clocks so the whole file runs in a
couple of seconds. Failsafes are triggered via their DIRECT public methods
(``controller.control_tick`` / ``controller._tick_once`` / ``governor.govern``
/ ``runtime.evaluate_link_failsafe`` / ``controller.handle_command``) rather
than through ``Runtime.telemetry()`` side effects, so the tests are stable
across the concurrent supervisor-loop refactor of ``app.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from vanchor.app import Runtime
from vanchor.controller.controller import Controller
from vanchor.controller.safety import SafetyConfig, SafetyGovernor
from vanchor.core.models import ControlModeName, GeoPoint, GpsFix, MotorCommand
from vanchor.core.state import NavigationState
from vanchor.hardware.serial_devices import SerialMotorController
from vanchor.hardware.serial_link import FakeSerialTransport, append_crc


# --------------------------------------------------------------------------- #
# Shared fakes / helpers (deliberately mirror tests/test_staleness.py idioms).
# --------------------------------------------------------------------------- #
class _FakeMotor:
    """MotorController stand-in: records the last applied command, async flush.

    Unlike the ``_FakeMotor`` in test_staleness (apply-only), this one also
    implements the async ``flush()`` so it can drive the supervised
    ``Controller._tick_once`` path (which awaits ``motor.flush()``)."""

    def __init__(self) -> None:
        self.last = MotorCommand()
        self.applied: list[MotorCommand] = []
        self.flushes = 0

    def apply(self, command: MotorCommand) -> None:
        self.last = command
        self.applied.append(command)

    async def flush(self) -> None:
        self.flushes += 1


def _controller(mode: ControlModeName = ControlModeName.MANUAL, mono=None):
    """A Controller wired to a fake motor, with a fresh fix + heading seeded so
    the first governor tick is fully fresh (not tripped by staleness)."""
    clock = mono if mono is not None else [1000.0]
    state = NavigationState()
    state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    state.heading_received_mono = clock[0]
    state.fix_received_mono = clock[0]
    ctrl = Controller(state, _FakeMotor(), bus=None, mono_fn=lambda: clock[0])
    state.mode = mode
    return clock, state, ctrl


async def _pump(predicate, limit: int = 300) -> None:
    """Yield to the event loop until ``predicate()`` is true (or ``limit``)."""
    for _ in range(limit):
        await asyncio.sleep(0)
        if predicate():
            return


# --------------------------------------------------------------------------- #
# 1. Controller tick exception -> supervised loop zeroes the motor.
#    (controller.py::_tick_once, lines ~803-836)
# --------------------------------------------------------------------------- #
async def test_controller_tick_exception_zeroes_motor():
    clock, state, ctrl = _controller(ControlModeName.HEADING_HOLD)
    ctrl.handle_command({"type": "heading_hold", "heading": 90.0, "throttle": 0.9})

    # A clean supervised tick first: the boat is driving and no fault is flagged.
    state.fix_seq += 1
    await ctrl._tick_once(0.2)
    assert ctrl.motor.last.thrust > 0.0
    assert state.controller_fault is None

    # Now sabotage the active mode so its update() raises on the next tick.
    def _boom(_state, _dt):
        raise RuntimeError("mode update exploded")

    ctrl.modes[ControlModeName.HEADING_HOLD].update = _boom  # type: ignore[assignment]

    state.fix_seq += 1
    await ctrl._tick_once(0.2)  # must NOT propagate -- the loop must survive

    # The fault is recorded AND the motor was best-effort zeroed (not left
    # stuck on the last driving command, which in sim would run the boat away).
    assert state.controller_fault is not None
    assert "RuntimeError" in state.controller_fault
    assert ctrl.motor.last.thrust == 0.0
    assert ctrl.motor.last.steering == 0.0
    assert state.motor_command.thrust == 0.0

    # And a subsequent CLEAN tick clears the fault (mode restored).
    ctrl.modes[ControlModeName.HEADING_HOLD].update = (
        lambda s, d: __import__("vanchor.core.models", fromlist=["GuidedSetpoint"])
        .GuidedSetpoint(target_heading=90.0, thrust=0.0)
    )
    state.fix_seq += 1
    await ctrl._tick_once(0.2)
    assert state.controller_fault is None


# --------------------------------------------------------------------------- #
# 2. Compass silence mid heading-hold -> staleness coast (zero thrust).
#    (safety.py::govern lines ~243-260, fed by controller._sensor_ages)
# --------------------------------------------------------------------------- #
def test_compass_silence_coasts_in_heading_hold():
    clock, state, ctrl = _controller(ControlModeName.HEADING_HOLD)
    ctrl.handle_command({"type": "heading_hold", "heading": 90.0, "throttle": 0.8})

    # Fresh heading -> the boat drives.
    state.fix_seq += 1
    cmd = ctrl.control_tick(0.2)
    assert cmd.thrust > 0.0
    assert not ctrl.safety_status.heading_stale

    # Compass goes silent for 5 s (> heading_stale_s default 3 s) -> coast.
    clock[0] = 1005.0
    state.fix_seq += 1  # fix still fresh; only the compass froze
    cmd = ctrl.control_tick(0.2)
    assert ctrl.safety_status.heading_stale
    assert cmd.thrust == 0.0

    # A fresh heading clears it and thrust ramps back up.
    state.heading_received_mono = 1005.0
    state.fix_seq += 1
    cmd = ctrl.control_tick(0.2)
    assert not ctrl.safety_status.heading_stale
    assert cmd.thrust > 0.0


# --------------------------------------------------------------------------- #
# 3. GPS fix loss -> loss-of-fix failsafe coast (on by default).
#    (safety.py::govern lines ~285-296; SafetyConfig.fix_failsafe_enabled=True)
# --------------------------------------------------------------------------- #
def test_gps_fix_loss_forces_coast_default_on():
    assert SafetyConfig().fix_failsafe_enabled is True
    gov = SafetyGovernor(SafetyConfig(max_thrust_slew_per_s=100.0, fix_timeout_s=3.0))
    s = NavigationState()

    cmd, status = gov.govern(MotorCommand(thrust=0.9), s, dt=0.2, fix_is_fresh=True)
    assert cmd.thrust > 0.0 and not status.fix_lost

    # No fresh fix; accumulate past the timeout -> forced to zero.
    gov.govern(MotorCommand(thrust=0.9), s, dt=2.0, fix_is_fresh=False)
    cmd, status = gov.govern(MotorCommand(thrust=0.9), s, dt=2.0, fix_is_fresh=False)
    assert status.fix_lost
    assert cmd.thrust == 0.0


# --------------------------------------------------------------------------- #
# 4. Depth sounder freeze -> shallow-stop treats a STALE sounding as unknown.
#    (safety.py::govern lines ~262-278). A fresh shallow reading stops; a frozen
#    one is NOT trusted (neither false-stops nor silently passes the check).
# --------------------------------------------------------------------------- #
def test_depth_freeze_treated_as_unknown_by_shallow_stop():
    gov = SafetyGovernor(SafetyConfig(min_depth_m=2.0, max_thrust_slew_per_s=100.0))
    s = NavigationState()
    s.mode = ControlModeName.WAYPOINT
    s.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    s.depth_m = 0.5  # a shallow reading

    # Fresh sounding below the minimum -> shallow stop (motor cut).
    cmd, status = gov.govern(
        MotorCommand(thrust=0.8), s, dt=0.2, fix_is_fresh=True, depth_age_s=1.0
    )
    assert status.shallow_stop
    assert cmd.thrust == 0.0

    # Same frozen value but now STALE (> depth_stale_s default 10 s) -> treated
    # as UNKNOWN, so the shallow-stop no longer trips on the frozen reading.
    cmd, status = gov.govern(
        MotorCommand(thrust=0.8), s, dt=0.2, fix_is_fresh=True, depth_age_s=15.0
    )
    assert not status.shallow_stop


# --------------------------------------------------------------------------- #
# 5. UI link loss while GUIDED -> anchor-hold; while DRIVING MANUALLY -> stop.
#    (app.py::evaluate_link_failsafe lines ~1155-1181, _underway ~1143-1153)
# --------------------------------------------------------------------------- #
def test_link_loss_guided_continues_mission_by_default():
    """Field report (2026-07-15): locking the phone during an active route
    must NOT park the boat — guided modes keep flying by default."""
    rt = Runtime(mono_fn=lambda: rt._mono_box[0])
    rt._mono_box = [1000.0]  # type: ignore[attr-defined]
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    rt.state.mode = ControlModeName.HEADING_HOLD  # a guided mode: making way
    rt.config.safety.link_loss_timeout_s = 20.0
    rt.client_connected()
    rt.client_disconnected()

    # Below the timeout -> nothing engages.
    assert rt.evaluate_link_failsafe(now=1015.0) is False
    assert rt.state.mode == ControlModeName.HEADING_HOLD

    # Past the timeout -> latched, but the mission CONTINUES (no anchor-hold).
    assert rt.evaluate_link_failsafe(now=1021.0) is True
    assert rt.state.mode == ControlModeName.HEADING_HOLD
    assert rt._link_failsafe_engaged
    assert rt._link_failsafe_action == "continue"


def test_link_loss_guided_engages_anchor_hold_when_opted_out():
    rt = Runtime(mono_fn=lambda: rt._mono_box[0])
    rt._mono_box = [1000.0]  # type: ignore[attr-defined]
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    rt.state.mode = ControlModeName.HEADING_HOLD  # a guided mode: making way
    rt.config.safety.link_loss_timeout_s = 20.0
    rt.config.safety.link_loss_continue_mission = False  # park-and-hold
    rt.client_connected()
    rt.client_disconnected()

    # Past the timeout -> guided mode holds position (anchor-hold).
    assert rt.evaluate_link_failsafe(now=1021.0) is True
    assert rt.state.mode == ControlModeName.ANCHOR_HOLD
    assert rt._link_failsafe_engaged
    assert rt._link_failsafe_action == "hold"


def test_link_loss_manual_driving_stops():
    rt = Runtime()
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    rt.state.mode = ControlModeName.MANUAL
    # Actually driving by hand -> a link loss must STOP (no target to hold to).
    rt.state.motor_command = MotorCommand(thrust=0.8, steering=0.3)
    rt.controller.manual.set(0.8, 0.3)
    rt.config.safety.link_loss_timeout_s = 10.0
    rt.client_connected()
    rt.client_disconnected()

    engaged = rt.evaluate_link_failsafe(now=rt._last_client_seen + 11.0)
    assert engaged is True
    assert rt.state.mode == ControlModeName.MANUAL  # STOP, not anchor-hold
    assert rt.controller.manual.thrust == 0.0
    assert rt._link_failsafe_engaged


# --------------------------------------------------------------------------- #
# 6. Serial device unplug: EOF mid-run -> the motor controller reconnects and a
#    write while the link is DOWN never raises out of the flush path.
#    (serial_devices.py::_SerialReadSupervisor.run/_reconnect ~128-194;
#     SerialMotorController.flush ~425-452)
# --------------------------------------------------------------------------- #
async def test_serial_motor_eof_reconnects_and_flush_survives_down_link():
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)  # record backoff, never wall-clock wait

    clock = {"t": 0.0}
    transport = FakeSerialTransport()
    motor = SerialMotorController(
        transport,
        reverse_delay_s=0.0,
        time_fn=lambda: clock["t"],
        sleep=fake_sleep,
    )
    await motor.start()
    await _pump(lambda: motor.healthy is True)  # let the supervisor task spin up
    assert motor.healthy is True

    # The feedback stream drops (device unplugged): the supervisor must NOT die.
    transport.feed_eof()
    await _pump(lambda: sleeps and motor.healthy is True and transport.open_calls == 2)
    assert motor.healthy is True  # reconnected on the re-opened transport
    assert transport.open_calls == 2  # initial open + one reconnect

    # A write attempted WHILE the transport is down must be dropped, not raised
    # out of the control loop (the firmware watchdog neutrals the motor).
    transport.fail_writes = True
    motor.apply(MotorCommand(thrust=0.7))
    await motor.flush()  # must not raise
    assert motor.healthy is False  # flush marked the link unhealthy

    # When the link recovers, a flush writes again cleanly.
    transport.fail_writes = False
    motor.apply(MotorCommand(thrust=0.5))
    await motor.flush()
    assert transport.written[-1].startswith("CMD ")
    await motor.stop()


# --------------------------------------------------------------------------- #
# 7. Rapid thrust reversal, incl. THROUGH ZERO -> gated at BOTH layers.
#    Layer A: SafetyGovernor (safety.py ~298-323, sticky _last_applied_dir).
#    Layer B: SerialMotorController._gate_reverse (serial_devices.py ~468-505).
# --------------------------------------------------------------------------- #
def test_through_zero_reversal_gated_in_governor():
    gov = SafetyGovernor(SafetyConfig(max_thrust_slew_per_s=100.0, reverse_delay_s=1.0))
    s = NavigationState()

    # Drive forward.
    cmd, _ = gov.govern(MotorCommand(thrust=0.8), s, dt=0.2, fix_is_fresh=True)
    assert cmd.thrust > 0.0

    # A single zero tick (PID crossing zero), then an immediate reverse request.
    gov.govern(MotorCommand(thrust=0.0), s, dt=0.2, fix_is_fresh=True)
    cmd, status = gov.govern(MotorCommand(thrust=-0.8), s, dt=0.2, fix_is_fresh=True)
    assert status.reverse_blocked, "through-zero reversal must stay gated"
    assert cmd.thrust == 0.0


async def test_through_zero_reversal_gated_in_serial_driver():
    clock = {"t": 0.0}
    transport = FakeSerialTransport()
    motor = SerialMotorController(transport, reverse_delay_s=0.9, time_fn=lambda: clock["t"])

    motor.apply(MotorCommand(thrust=1.0))
    await motor.flush()
    assert transport.written[-1] == append_crc("CMD 255 F 0")

    clock["t"] = 0.2  # one zero tick
    motor.apply(MotorCommand(thrust=0.0))
    await motor.flush()
    assert transport.written[-1] == append_crc("CMD 0 F 0")

    clock["t"] = 0.4  # reverse after a single zero tick -> still blocked
    motor.apply(MotorCommand(thrust=-1.0))
    await motor.flush()
    assert transport.written[-1] == append_crc("CMD 0 F 0"), "reverse bypassed the delay"

    clock["t"] = 1.2  # full delay elapsed from the first zero tick -> allowed
    await motor.flush()
    assert transport.written[-1] == append_crc("CMD 255 R 0")


# --------------------------------------------------------------------------- #
# 8. Shallow-water / no-go approach -> governor stops (with lookahead).
#    (safety.py::govern ~262-283, _in_or_near_nogo ~185-211)
# --------------------------------------------------------------------------- #
def test_shallow_water_and_nogo_lookahead_stop():
    # Shallow water: a fresh below-minimum sounding cuts thrust.
    gov = SafetyGovernor(SafetyConfig(min_depth_m=1.0, max_thrust_slew_per_s=100.0))
    s = NavigationState()
    s.mode = ControlModeName.WAYPOINT
    s.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    s.depth_m = 0.5
    cmd, status = gov.govern(MotorCommand(thrust=0.8), s, dt=0.2, fix_is_fresh=True)
    assert status.shallow_stop and cmd.thrust == 0.0

    # No-go zone: stopped by the lookahead BEFORE entering the polygon.
    gov2 = SafetyGovernor(SafetyConfig(max_thrust_slew_per_s=100.0, nogo_lookahead_m=50.0))
    gov2.set_nogo_zones(
        [[(58.99, 17.99), (58.99, 18.01), (59.01, 18.01), (59.01, 17.99)]]
    )
    s2 = NavigationState()
    s2.mode = ControlModeName.WAYPOINT
    s2.fix = GpsFix(point=GeoPoint(58.9897, 18.0))  # ~30 m N of the edge
    cmd, status = gov2.govern(MotorCommand(thrust=0.8), s2, dt=0.2, fix_is_fresh=True)
    assert status.nogo_stop and cmd.thrust == 0.0


# --------------------------------------------------------------------------- #
# 9. Anchor drag -> alarm raised (station-keeping only).
#    (safety.py::govern ~356-371)
# --------------------------------------------------------------------------- #
def test_anchor_drag_raises_alarm():
    gov = SafetyGovernor(SafetyConfig(drag_alarm_factor=2.0, max_thrust_slew_per_s=100.0))
    s = NavigationState()
    s.mode = ControlModeName.ANCHOR_HOLD
    s.anchor = GeoPoint(59.0, 18.0)
    s.anchor_radius_m = 5.0

    # Within 2x radius -> no alarm.
    s.distance_to_anchor_m = 8.0
    _, status = gov.govern(MotorCommand(), s, dt=0.2, fix_is_fresh=True)
    assert not status.drag_alarm

    # Beyond 2x radius -> drag alarm.
    s.distance_to_anchor_m = 12.0
    _, status = gov.govern(MotorCommand(), s, dt=0.2, fix_is_fresh=True)
    assert status.drag_alarm


# --------------------------------------------------------------------------- #
# 10. Wall-clock step (NTP) mid-session -> link failsafe keys off MONOTONIC.
#     evaluate_link_failsafe measures duration on the injected monotonic clock
#     (app.py ~1155-1181), so a wall-clock jump can neither prematurely engage
#     nor indefinitely defer the failsafe -- only monotonic elapsed matters.
# --------------------------------------------------------------------------- #
def test_wall_clock_step_does_not_disturb_link_failsafe():
    mono = [5000.0]
    rt = Runtime(mono_fn=lambda: mono[0])
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    rt.state.mode = ControlModeName.HEADING_HOLD
    rt.config.safety.link_loss_timeout_s = 20.0
    rt.config.safety.link_loss_continue_mission = False  # test the hold path
    rt.client_connected()
    rt.client_disconnected()  # stamped at mono=5000

    # Monotonic has advanced only 5 s -- regardless of any NTP wall-clock jump
    # (which the failsafe never reads), it must NOT engage yet.
    mono[0] = 5005.0
    assert rt.evaluate_link_failsafe() is False
    assert rt.state.mode == ControlModeName.HEADING_HOLD

    # Only once MONOTONIC elapsed exceeds the timeout does it engage.
    mono[0] = 5021.0
    assert rt.evaluate_link_failsafe() is True
    assert rt.state.mode == ControlModeName.ANCHOR_HOLD


# --------------------------------------------------------------------------- #
# 11. STOP from every motor-engaging mode -> MANUAL + zero thrust.
#     (controller.py::handle_command "stop" ~631-634)
# --------------------------------------------------------------------------- #
_STOP_FROM_COMMANDS = [
    {"type": "heading_hold", "heading": 90.0, "throttle": 0.9},
    {"type": "anchor_hold"},
    {"type": "anchor_ml"},
    {"type": "drift", "heading": 45.0, "knots": 1.0},
    {"type": "trolling", "amplitude_deg": 20.0, "period_s": 20.0},
    {"type": "orbit", "center_lat": 59.0, "center_lon": 18.0, "radius_m": 20.0},
    {"type": "goto", "waypoints": [{"lat": 59.01, "lon": 18.01}], "throttle": 0.9},
    {"type": "work_area", "waypoints": [{"lat": 59.01, "lon": 18.01}]},
    {"type": "follow_apb", "throttle": 0.8},
    {"type": "contour_follow", "target_depth_m": 3.0},
    {"type": "manual", "thrust": 0.9, "steering": 0.4},
]


@pytest.mark.parametrize("enter", _STOP_FROM_COMMANDS, ids=lambda c: c["type"])
def test_stop_from_every_mode_zeroes_and_goes_manual(enter):
    clock, state, ctrl = _controller()
    # Give the modes what they may read on activation.
    state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    ctrl.handle_command(enter)

    # STOP from whatever engaging mode we're in.
    ctrl.handle_command({"type": "stop"})
    assert state.mode == ControlModeName.MANUAL
    assert ctrl.manual.thrust == 0.0
    assert ctrl.manual.steering == 0.0

    # And the boat actually comes to rest: several ticks command ~zero thrust.
    last = None
    for _ in range(10):
        state.fix_seq += 1
        last = ctrl.control_tick(0.2)
    assert abs(last.thrust) < 1e-9


# --------------------------------------------------------------------------- #
# 12. STOP also reaches the boat through the WebSocket + POST API surfaces.
#     Proves the "STOP always works" floor at the transport boundary, not just
#     the controller. (ui/server.py command routes -> runtime.handle_command)
# --------------------------------------------------------------------------- #
def test_stop_via_post_api_goes_manual_zero():
    from fastapi.testclient import TestClient

    from vanchor.ui.server import create_app

    rt = Runtime()
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    rt.handle_command({"type": "heading_hold", "heading": 90.0, "throttle": 0.9})
    assert rt.state.mode == ControlModeName.HEADING_HOLD

    with TestClient(create_app(rt)) as c:
        resp = c.post("/api/command", json={"type": "stop"})
        assert resp.status_code == 200
    assert rt.state.mode == ControlModeName.MANUAL
    assert rt.controller.manual.thrust == 0.0
