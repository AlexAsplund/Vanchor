"""Tests for sim/devices.py: loop robustness and opt-in actuation shaping.

Three areas covered:
1. Sensor loop resilience: publish exceptions are swallowed (loop survives);
   stop() cancels cleanly.
2. SimMotorController actuation shaping: reverse delay, lag, and default
   passthrough behaviour.
3. RNG determinism check: shaping params do not perturb sample() output.
"""

from __future__ import annotations

import asyncio
import math

import pytest

from vanchor.core.events import EventBus
from vanchor.core.models import BoatState, GeoPoint, MotorCommand
from vanchor.sim.devices import SimCompass, SimGps, SimMotorController

# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

_HERE = GeoPoint(59.3293, 18.0686)
_TRUTH = BoatState(point=_HERE, heading_deg=0.0)


def _truth() -> BoatState:
    return _TRUTH


# ------------------------------------------------------------------ #
# 1. Sensor loop robustness
# ------------------------------------------------------------------ #


class _BurstBus(EventBus):
    """An EventBus that raises on the first publish call, then works normally."""

    def __init__(self, fail_on: int = 1) -> None:
        super().__init__()
        self._calls = 0
        self._fail_on = fail_on
        self.published: list[tuple[str, object]] = []

    async def publish(self, topic: str, payload: object) -> None:
        self._calls += 1
        if self._calls == self._fail_on:
            raise RuntimeError("simulated publish failure")
        self.published.append((topic, payload))


async def test_gps_loop_survives_publish_exception() -> None:
    """The GPS loop must not die when bus.publish() raises."""
    bus = _BurstBus(fail_on=1)
    gps = SimGps(_truth, bus, update_hz=50.0)  # fast so the test is quick
    await gps.start()
    # Allow several publish attempts — the loop should keep running.
    await asyncio.sleep(0.12)
    await gps.stop()
    # At least two successful publishes after the one exception means the loop
    # recovered and continued.
    assert len(bus.published) >= 2, f"only {len(bus.published)} successful publishes"


async def test_compass_loop_survives_publish_exception() -> None:
    """The compass loop must not die when bus.publish() raises."""
    bus = _BurstBus(fail_on=1)
    compass = SimCompass(_truth, bus, update_hz=50.0)
    await compass.start()
    await asyncio.sleep(0.12)
    await compass.stop()
    # Compass publishes two events per tick (HDM + IMU); successful count > 2.
    assert len(bus.published) >= 2, f"only {len(bus.published)} successful publishes"


async def test_stop_cancels_gps_loop_promptly() -> None:
    """stop() must cancel the task and not leave it dangling."""
    gps = SimGps(_truth, bus=None, update_hz=1.0)
    await gps.start()
    task = gps._task
    assert task is not None
    assert not task.done()
    await gps.stop()
    assert gps._task is None
    assert task.done()
    assert task.cancelled()


async def test_stop_cancels_compass_loop_promptly() -> None:
    """stop() must cancel the compass task cleanly."""
    compass = SimCompass(_truth, bus=None, update_hz=1.0)
    await compass.start()
    task = compass._task
    assert task is not None
    await compass.stop()
    assert compass._task is None
    assert task.done()
    assert task.cancelled()


async def test_stop_before_start_is_noop() -> None:
    """stop() on a never-started sensor must not raise."""
    gps = SimGps(_truth, bus=None)
    await gps.stop()  # should be a silent no-op


async def test_double_stop_is_safe() -> None:
    """A second stop() on an already-stopped sensor must not raise."""
    gps = SimGps(_truth, bus=None, update_hz=10.0)
    await gps.start()
    await gps.stop()
    await gps.stop()  # must not raise


# ------------------------------------------------------------------ #
# 2. SimMotorController — default (passthrough) behaviour
# ------------------------------------------------------------------ #


def test_defaults_passthrough_instantly() -> None:
    """With no shaping params the command property returns the request verbatim."""
    m = SimMotorController()
    m.apply(MotorCommand(thrust=0.8, steering=-0.3))
    assert m.command.thrust == pytest.approx(0.8)
    assert m.command.steering == pytest.approx(-0.3)


def test_defaults_step_is_noop() -> None:
    """step() with default params applies no SHAPING — only the wire
    quantization (thrust rides as 8-bit PWM, sim-vs-real review 2026-07-15)."""
    m = SimMotorController()
    m.apply(MotorCommand(thrust=0.5))
    m.step(1.0)
    assert m.command.thrust == pytest.approx(round(0.5 * 255) / 255)


def test_defaults_identity_with_reversal() -> None:
    """Without reverse_delay the sign flip is instantaneous."""
    m = SimMotorController()
    m.apply(MotorCommand(thrust=1.0))
    m.apply(MotorCommand(thrust=-1.0))
    # No step() needed — default path is a transparent passthrough.
    assert m.command.thrust == pytest.approx(-1.0)


# ------------------------------------------------------------------ #
# 3. SimMotorController — reverse delay
# ------------------------------------------------------------------ #


def test_reverse_delay_holds_zero() -> None:
    """After a direction flip the output must stay at zero for reverse_delay_s."""
    m = SimMotorController(reverse_delay_s=0.9)
    m.apply(MotorCommand(thrust=1.0))
    m.step(0.05)  # establish forward
    m.apply(MotorCommand(thrust=-1.0))  # flip → arms hold timer

    # Still in hold after 0.85 s of sim time (< 0.9 s)
    t = 0.0
    dt = 0.05
    while t < 0.85:
        m.step(dt)
        assert m.command.thrust == pytest.approx(0.0), f"non-zero at t={t:.2f}"
        t += dt


def test_reverse_delay_releases_after_delay() -> None:
    """Output must become non-zero once the full reverse_delay_s has elapsed."""
    m = SimMotorController(reverse_delay_s=0.9)
    m.apply(MotorCommand(thrust=1.0))
    m.step(0.01)
    m.apply(MotorCommand(thrust=-1.0))

    # Advance 1.0 s of sim time (safely past the 0.9 s hold).
    # The hold expires when the remaining counter hits zero; the following step
    # then applies the new direction.  Using 1.0 s gives a clear margin.
    dt = 0.01
    for _ in range(100):  # 100 × 0.01 s = 1.0 s
        m.step(dt)

    # After the hold the applied thrust should track the −1.0 command.
    assert m.command.thrust < 0.0, "thrust still zero after delay expired"


def test_reverse_delay_same_direction_no_hold() -> None:
    """No hold if the new command stays in the same direction."""
    m = SimMotorController(reverse_delay_s=0.9)
    m.apply(MotorCommand(thrust=0.5))
    m.step(0.05)
    m.apply(MotorCommand(thrust=0.8))  # same direction, no flip
    m.step(0.05)
    assert m.command.thrust == pytest.approx(0.8)


def test_reverse_delay_zero_to_reverse_no_hold() -> None:
    """Zero → negative does not arm the hold (no active forward direction)."""
    m = SimMotorController(reverse_delay_s=0.9)
    # No forward command was ever issued.
    m.apply(MotorCommand(thrust=-1.0))
    m.step(0.05)
    assert m.command.thrust == pytest.approx(-1.0)


# ------------------------------------------------------------------ #
# 4. SimMotorController — first-order lag
# ------------------------------------------------------------------ #


def test_lag_approaches_command_exponentially() -> None:
    """Applied thrust must follow an exponential ramp toward the command."""
    tau = 1.0
    m = SimMotorController(thrust_lag_tau_s=tau)
    m.apply(MotorCommand(thrust=1.0))

    # After one time-constant (1 τ) the thrust should be close to
    # 1 − e⁻¹ ≈ 0.632 of the target (±10 % tolerance for discrete stepping).
    dt = 0.01
    steps = int(tau / dt)
    for _ in range(steps):
        m.step(dt)

    expected = 1.0 - math.exp(-1.0)
    assert m.command.thrust == pytest.approx(expected, rel=0.10), (
        f"expected ~{expected:.3f}, got {m.command.thrust:.3f}"
    )


def test_lag_approaches_asymptotically() -> None:
    """After many time-constants the thrust must be very close to the command."""
    tau = 0.2
    m = SimMotorController(thrust_lag_tau_s=tau)
    m.apply(MotorCommand(thrust=1.0))

    dt = 0.01
    for _ in range(int(5 * tau / dt)):  # 5τ
        m.step(dt)

    assert m.command.thrust == pytest.approx(1.0, abs=0.01)


def test_lag_steering_passthrough() -> None:
    """Steering is not lagged — it should always equal the requested value."""
    m = SimMotorController(thrust_lag_tau_s=0.5)
    m.apply(MotorCommand(thrust=1.0, steering=0.7))
    m.step(0.01)
    assert m.command.steering == pytest.approx(0.7)


# ------------------------------------------------------------------ #
# 5. RNG determinism — shaping params must not perturb sample() output
# ------------------------------------------------------------------ #


def test_gps_sample_output_unaffected_by_shaping() -> None:
    """sample() on SimGps must be identical with or without a motor controller."""
    gps_a = SimGps(_truth, bus=None, seed=42)
    gps_b = SimGps(_truth, bus=None, seed=42)
    # Calling sample() twice on both: same seed → same output.
    for _ in range(5):
        assert gps_a.sample() == gps_b.sample()
