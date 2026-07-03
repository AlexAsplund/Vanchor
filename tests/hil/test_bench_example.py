"""Example hardware-in-the-loop bench test (skipped unless ``VANCHOR_HIL=1``).

This documents the shape of a real command->motion assertion against a physical
bench: send a thrust command to the Arduino motor controller, wait, and confirm
the boat actually moved. On any machine without a declared bench (i.e. normal
dev boxes and CI) this is collected but **skipped**, so it never breaks the
suite -- yet it is real, runnable code the moment a bench is wired up and
``VANCHOR_HIL=1`` is set.

The ``bench`` fixture (see ``tests/hil/conftest.py``) both provides the rig and
skips if the hardware is unreachable, so these tests are safe to keep committed.
"""

from __future__ import annotations

import time


def test_thrust_command_produces_forward_motion(bench):
    """Full-ahead thrust should make the boat move (speed over ground rises).

    The core HIL assertion: a *command* out of the autopilot produces the
    expected physical *motion*. This closes the loop the pure-sim tests cannot,
    validating wiring, firmware, motor direction and telemetry end to end.
    """
    # Stop, settle, and record the resting speed.
    bench.send_command(thrust=0.0, steering=0.0)
    time.sleep(1.0)
    rest = bench.read_motion()

    # Drive forward for a few seconds.
    bench.send_command(thrust=0.6, steering=0.0)
    time.sleep(4.0)
    moving = bench.read_motion()

    # Always stop the motor, even if the assertion fails.
    bench.send_command(thrust=0.0, steering=0.0)

    assert moving.speed_mps > rest.speed_mps + 0.1, (
        "thrust command did not produce forward motion "
        f"(rest={rest.speed_mps:.2f} m/s, moving={moving.speed_mps:.2f} m/s)"
    )


def test_starboard_steering_turns_starboard(bench):
    """Positive steering with thrust should swing the heading to starboard.

    Mirrors the sim's steering-authority-scales-with-thrust behaviour on real
    hardware: with thrust applied, a starboard steer command must increase the
    heading (turn right). Catches reversed steering wiring -- a safety-relevant
    class of bug the bench is uniquely able to detect.
    """
    bench.send_command(thrust=0.4, steering=0.0)
    time.sleep(2.0)
    before = bench.read_motion().heading_deg

    bench.send_command(thrust=0.4, steering=0.8)
    time.sleep(4.0)
    after = bench.read_motion().heading_deg

    bench.send_command(thrust=0.0, steering=0.0)

    delta = (after - before + 180.0) % 360.0 - 180.0
    assert delta > 3.0, f"starboard steer did not turn starboard (delta={delta:.1f} deg)"
