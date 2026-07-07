"""Task 1 of the motor-split: SplitMotor composite, MotorChannel seam, per-channel
config resolution, and the pure link-resolution planner.

Safety-first TDD (Constraints 3 + 4 of the motor-split plan):
  * every LEGACY config (channel keys unset) resolves to EXACTLY today's object
    graph — ``plan_motor_links`` returns ``combined`` and ``channel_link`` mirrors
    the legacy ``motor_*`` link field-for-field;
  * a STOP-shaped command zeroes BOTH channels;
  * one failing channel never blocks the other.
"""

import pytest

from vanchor.core.config import HardwareConfig
from vanchor.core.models import MotorCommand
from vanchor.hardware.link_plan import plan_motor_links
from vanchor.hardware.split_motor import MotorChannel, SplitMotor


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class FakeChannel(MotorChannel):
    """Records everything for assertions; can be told to raise on any call."""

    def __init__(self, name: str = "ch", *, raise_on: set[str] | None = None) -> None:
        self.name = name
        self.raise_on = raise_on or set()
        self.values: list[float] = []
        self.flushes = 0
        self.starts = 0
        self.stops = 0

    def set_normalized(self, value: float) -> None:
        if "set_normalized" in self.raise_on:
            raise RuntimeError(f"{self.name} set_normalized boom")
        # clamp is the contract of the base class helper; record the clamped value
        self.values.append(max(-1.0, min(1.0, value)))

    async def flush(self) -> None:
        if "flush" in self.raise_on:
            raise RuntimeError(f"{self.name} flush boom")
        self.flushes += 1

    async def start(self) -> None:
        if "start" in self.raise_on:
            raise RuntimeError(f"{self.name} start boom")
        self.starts += 1

    async def stop(self) -> None:
        if "stop" in self.raise_on:
            raise RuntimeError(f"{self.name} stop boom")
        self.stops += 1

    def debug(self) -> str:
        if "debug" in self.raise_on:
            raise RuntimeError(f"{self.name} debug boom")
        return f"{self.name}: values={self.values}"

    @property
    def last(self) -> float | None:
        return self.values[-1] if self.values else None


# --------------------------------------------------------------------------- #
# SplitMotor routing + safety
# --------------------------------------------------------------------------- #
def test_apply_routes_each_field_to_the_right_channel():
    thrust, steering = FakeChannel("thrust"), FakeChannel("steering")
    m = SplitMotor(thrust=thrust, steering=steering)
    m.apply(MotorCommand(thrust=0.4, steering=-0.7))
    assert thrust.last == pytest.approx(0.4)
    assert steering.last == pytest.approx(-0.7)


def test_apply_clamps_out_of_range_command():
    thrust, steering = FakeChannel("thrust"), FakeChannel("steering")
    m = SplitMotor(thrust=thrust, steering=steering)
    m.apply(MotorCommand(thrust=5.0, steering=-9.0))
    assert thrust.last == pytest.approx(1.0)
    assert steering.last == pytest.approx(-1.0)


def test_stop_command_zeroes_BOTH_channels():
    """Constraint 4: a STOP-shaped command (0,0) must zero BOTH channels."""
    thrust, steering = FakeChannel("thrust"), FakeChannel("steering")
    m = SplitMotor(thrust=thrust, steering=steering)
    m.apply(MotorCommand(thrust=0.9, steering=0.9))
    m.apply(MotorCommand(thrust=0.0, steering=0.0))  # STOP
    assert thrust.last == 0.0
    assert steering.last == 0.0


def test_none_channel_is_skipped_not_connected():
    thrust = FakeChannel("thrust")
    m = SplitMotor(thrust=thrust, steering=None)
    m.apply(MotorCommand(thrust=0.3, steering=0.5))  # steering has no channel
    assert thrust.last == pytest.approx(0.3)
    assert m.steering is None


async def test_raising_thrust_channel_does_not_block_steering_on_apply():
    """One channel raising in apply must not stop the other from being commanded."""
    thrust = FakeChannel("thrust", raise_on={"set_normalized"})
    steering = FakeChannel("steering")
    m = SplitMotor(thrust=thrust, steering=steering)
    m.apply(MotorCommand(thrust=0.5, steering=0.5))  # must not raise
    assert steering.last == pytest.approx(0.5)


async def test_raising_steering_channel_does_not_block_thrust_on_apply():
    thrust = FakeChannel("thrust")
    steering = FakeChannel("steering", raise_on={"set_normalized"})
    m = SplitMotor(thrust=thrust, steering=steering)
    m.apply(MotorCommand(thrust=0.5, steering=0.5))
    assert thrust.last == pytest.approx(0.5)


async def test_flush_guards_each_channel():
    thrust = FakeChannel("thrust", raise_on={"flush"})
    steering = FakeChannel("steering")
    m = SplitMotor(thrust=thrust, steering=steering)
    await m.flush()  # thrust raises internally; steering must still flush
    assert steering.flushes == 1


async def test_start_and_stop_guard_each_channel():
    thrust = FakeChannel("thrust", raise_on={"start", "stop"})
    steering = FakeChannel("steering")
    m = SplitMotor(thrust=thrust, steering=steering)
    await m.start()
    await m.stop()
    assert steering.starts == 1 and steering.stops == 1


def test_debug_composes_both_and_never_raises():
    thrust = FakeChannel("thrust", raise_on={"debug"})
    steering = FakeChannel("steering")
    m = SplitMotor(thrust=thrust, steering=steering)
    out = m.debug()  # must not raise even though thrust.debug() raises
    assert "steering" in out


def test_debug_handles_none_channels():
    m = SplitMotor(thrust=None, steering=None)
    assert isinstance(m.debug(), str)  # never raises


# --------------------------------------------------------------------------- #
# Config resolution — legacy identity (Constraint 3)
# --------------------------------------------------------------------------- #
def _legacy_motor_link(hw: HardwareConfig) -> dict:
    """The link a legacy build reads straight off the motor_* fields."""
    return {
        "source": hw.source("motor"),
        "port": hw.motor_port,
        "baud": hw.motor_baud,
        "bytesize": hw.motor_bytesize,
        "parity": hw.motor_parity,
        "stopbits": hw.motor_stopbits,
    }


LEGACY_CASES = [
    HardwareConfig(),                                    # default (sim)
    HardwareConfig(enabled=True),                        # serial via enabled
    HardwareConfig(motor_source="sim"),
    HardwareConfig(motor_source="serial"),
    HardwareConfig(motor_source="both"),
    HardwareConfig(motor_source="none"),
    HardwareConfig(enabled=True, motor_source="both"),
    HardwareConfig(enabled=True, motor_port="/dev/ttyACM9",
                   motor_baud=115200, motor_bytesize=7,
                   motor_parity="E", motor_stopbits=2.0),
]


@pytest.mark.parametrize("hw", LEGACY_CASES)
def test_unset_channel_link_equals_legacy_motor_link(hw):
    """Constraint 3: with channel keys unset, BOTH channels resolve field-for-field
    to the legacy motor link."""
    legacy = _legacy_motor_link(hw)
    assert hw.channel_link("steering") == legacy
    assert hw.channel_link("thrust") == legacy


def test_explicit_channel_source_override_wins():
    hw = HardwareConfig(motor_source="serial", thrust_source="sim")
    assert hw.channel_link("thrust")["source"] == "sim"
    assert hw.channel_link("steering")["source"] == "serial"  # falls back to motor


def test_explicit_channel_port_and_framing_win():
    hw = HardwareConfig(
        motor_source="serial", motor_port="/dev/ttyUSB2", motor_baud=4800,
        thrust_source="serial", thrust_port="/dev/ttyACM0", thrust_baud=115200,
        thrust_bytesize=7, thrust_parity="E", thrust_stopbits=2.0,
    )
    t = hw.channel_link("thrust")
    assert t["port"] == "/dev/ttyACM0"
    assert t["baud"] == 115200
    assert t["bytesize"] == 7 and t["parity"] == "E" and t["stopbits"] == 2.0


def test_partial_override_switches_whole_channel_to_its_own_framing():
    """Partial-override rule: setting the channel source (or port) marks the channel
    'configured', so its OWN baud/framing apply — they do NOT keep blending with the
    motor_* framing. Port still falls back to motor_port when left blank (a serial
    link needs a port)."""
    hw = HardwareConfig(
        motor_source="serial", motor_port="/dev/ttyUSB2",
        motor_baud=9600, motor_bytesize=7, motor_parity="E", motor_stopbits=2.0,
        thrust_source="serial",  # configured, but baud/framing/port left at defaults
    )
    t = hw.channel_link("thrust")
    assert t["port"] == "/dev/ttyUSB2"   # blank channel port -> motor_port
    assert t["baud"] == 4800             # channel default, NOT motor's 9600
    assert t["bytesize"] == 8 and t["parity"] == "N" and t["stopbits"] == 1.0


# --------------------------------------------------------------------------- #
# plan_motor_links — construction decision (pure)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("hw", LEGACY_CASES)
def test_legacy_config_plans_combined_identical_to_today(hw):
    """Constraint 3: every legacy config yields a `combined` plan whose resolved link
    equals the legacy motor link field-for-field. Never `split`."""
    plan = plan_motor_links(hw)
    assert plan.kind == "combined"
    legacy = _legacy_motor_link(hw)
    assert plan.source == legacy["source"]
    # the combined link carries the resolved framing (relevant for serial builds)
    assert plan.link == legacy
    assert plan.tee is (legacy["source"] == "both")


def test_sim_sim_is_combined_sim():
    hw = HardwareConfig(steering_source="sim", thrust_source="sim")
    plan = plan_motor_links(hw)
    assert plan.kind == "combined" and plan.source == "sim"
    assert plan.tee is False and plan.neutral_channel is None


def test_serial_same_port_is_combined():
    hw = HardwareConfig(
        steering_source="serial", steering_port="/dev/ttyUSB2",
        thrust_source="serial", thrust_port="/dev/ttyUSB2",
    )
    plan = plan_motor_links(hw)
    assert plan.kind == "combined" and plan.source == "serial"
    assert plan.link["port"] == "/dev/ttyUSB2"


def test_serial_same_port_mismatched_framing_raises():
    hw = HardwareConfig(
        steering_source="serial", steering_port="/dev/ttyUSB2", steering_baud=4800,
        thrust_source="serial", thrust_port="/dev/ttyUSB2", thrust_baud=115200,
    )
    with pytest.raises(ValueError, match="share a port|framing must match"):
        plan_motor_links(hw)


def test_different_ports_is_split():
    hw = HardwareConfig(
        steering_source="serial", steering_port="/dev/ttyUSB2",
        thrust_source="serial", thrust_port="/dev/ttyACM0",
    )
    plan = plan_motor_links(hw)
    assert plan.kind == "split"
    assert plan.steering["port"] == "/dev/ttyUSB2"
    assert plan.thrust["port"] == "/dev/ttyACM0"


def test_steering_none_thrust_serial_is_combined_with_neutral_channel():
    hw = HardwareConfig(
        steering_source="none",
        thrust_source="serial", thrust_port="/dev/ttyUSB2",
    )
    plan = plan_motor_links(hw)
    assert plan.kind == "combined" and plan.source == "serial"
    assert plan.neutral_channel == "steering"
    assert plan.link["port"] == "/dev/ttyUSB2"


def test_both_both_is_combined_with_tee_flag():
    hw = HardwareConfig(steering_source="both", thrust_source="both")
    plan = plan_motor_links(hw)
    assert plan.kind == "combined" and plan.source == "both"
    assert plan.tee is True


def test_none_none_with_sim_motor_stays_combined_sim():
    """Field incident: the Devices panel re-submits every field, so a stray
    persisted steering/thrust = none/none silently replaced the sim motor with
    a NullMotor (100% thrust, boat parked). Channel none+none only means
    motor-off when the combined motor_source is ALSO none."""
    hw = HardwareConfig(steering_source="none", thrust_source="none")
    plan = plan_motor_links(hw)   # default motor_source resolves to sim
    assert plan.kind == "combined" and plan.source == "sim"
    assert plan.neutral_channel is None


def test_none_none_with_motor_none_is_still_off():
    hw = HardwareConfig(steering_source="none", thrust_source="none",
                        motor_source="none")
    plan = plan_motor_links(hw)
    assert plan.kind == "combined" and plan.source == "none"
    assert plan.neutral_channel is None


def test_mixed_sim_and_serial_is_split():
    hw = HardwareConfig(
        steering_source="sim",
        thrust_source="serial", thrust_port="/dev/ttyACM0",
    )
    plan = plan_motor_links(hw)
    assert plan.kind == "split"
    assert plan.steering["source"] == "sim"
    assert plan.thrust["source"] == "serial"
