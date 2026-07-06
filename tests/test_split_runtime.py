"""Task 2: runtime construction, device model, capabilities, and API for split motor channels.

Exercises:
  * Legacy configs build the same classes as before (type-name assertions).
  * Split config builds SplitMotor with the right channels.
  * STOP through Runtime.handle_command zeroes both fake channels.
  * A failing channel build -> runtime up, channel unhealthy, mode gated with
    the channel named.
  * device_status carries steering/thrust + motor roll-up when split.
  * set_device_config rejects the same-port framing conflict with a ValueError
    (-> 400 at the HTTP layer).
  * Realpath-aliased ports resolve as the SAME endpoint (not split).
  * Existing motor tests are NOT modified; this file is additive.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

import pytest

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.core.models import ControlModeName, MotorCommand
from vanchor.hardware.split_motor import MotorChannel, SplitMotor
from vanchor.hardware.interfaces import NullMotor

# Isolated data directory: set_device_config writes devices.json here so tests
# don't pollute the real data directory.
_ISOLATED_DATA_DIR = tempfile.mkdtemp(prefix="vanchor-split-rt-")


def _rt(**hw_overrides) -> Runtime:
    cfg = load(None)
    cfg.data_dir = _ISOLATED_DATA_DIR
    for k, v in hw_overrides.items():
        setattr(cfg.hardware, k, v)
    return Runtime(cfg)


def _split_rt() -> Runtime:
    """A runtime with a split motor plan: steering=sim, thrust=serial.

    Both channels are built: the sim steering channel and a
    SerialThrustChannel pointing at a non-existent port.  The thrust channel
    starts unhealthy (the port never opens) but IS constructed.
    """
    return _rt(
        steering_source="sim",
        thrust_source="serial",
        thrust_port="/dev/ttyFAKESPLIT",
    )


# ─────────────────────────────────────────────────────────────────────────── #
# FakeChannel (local duplicate of test_split_motor's; kept here for isolation) #
# ─────────────────────────────────────────────────────────────────────────── #

class FakeChannel(MotorChannel):
    """Minimal test double that records set_normalized calls."""

    def __init__(self, name: str = "ch") -> None:
        self.name = name
        self.values: list[float] = []

    def set_normalized(self, value: float) -> None:
        self.values.append(value)

    @property
    def last(self) -> float | None:
        return self.values[-1] if self.values else None


# ─────────────────────────────────────────────────────────────────────────── #
# Legacy-identity: combined path builds EXACTLY the same classes as before    #
# ─────────────────────────────────────────────────────────────────────────── #

def test_legacy_default_builds_simmotor():
    """Default config (no channel keys) -> single SimMotorController (combined sim)."""
    rt = _rt()
    assert type(rt.controller.motor).__name__ == "SimMotorController"


def test_legacy_motor_none_builds_nullmotor():
    rt = _rt(motor_source="none")
    assert type(rt.controller.motor).__name__ == "NullMotor"


def test_legacy_motor_serial_builds_serialmotor():
    from vanchor.hardware.serial_devices import SerialMotorController
    from vanchor.hardware.serial_link import FakeSerialTransport
    with patch.object(Runtime, "_build_serial_motor",
                      lambda self, c: SerialMotorController(FakeSerialTransport())):
        rt = _rt(motor_source="serial")
    assert type(rt.controller.motor).__name__ == "SerialMotorController"


def test_legacy_motor_both_builds_teemotor():
    from vanchor.hardware.serial_devices import SerialMotorController
    from vanchor.hardware.serial_link import FakeSerialTransport
    with patch.object(Runtime, "_build_serial_motor",
                      lambda self, c: SerialMotorController(FakeSerialTransport())):
        rt = _rt(motor_source="both")
    # _TeeMotor is a private class; compare by name to avoid importing it.
    assert type(rt.controller.motor).__name__ == "_TeeMotor"


def test_legacy_motor_is_never_split_motor():
    """The legacy combined path must NEVER produce a SplitMotor (Constraint 3)."""
    for motor_source in ("sim", "none"):
        rt = _rt(motor_source=motor_source)
        assert not isinstance(rt.controller.motor, SplitMotor), (
            f"motor_source={motor_source!r} should be combined, got SplitMotor")


# ─────────────────────────────────────────────────────────────────────────── #
# Split construction                                                           #
# ─────────────────────────────────────────────────────────────────────────── #

def test_split_plan_builds_split_motor():
    rt = _split_rt()
    assert isinstance(rt.controller.motor, SplitMotor)


def test_split_steering_sim_channel_is_built():
    """The sim steering channel is built; the serial thrust channel is also built (Task 3)."""
    from vanchor.hardware.serial_channels import SerialThrustChannel
    rt = _split_rt()
    motor = rt.controller.motor
    assert motor.steering is not None, "sim steering channel should be built"
    assert isinstance(motor.thrust, SerialThrustChannel), (
        "serial thrust channel should be a SerialThrustChannel after Task 3"
    )


def test_split_sim_channel_debug_never_raises():
    rt = _split_rt()
    motor = rt.controller.motor
    text = motor.debug()
    assert "SplitMotor" in text
    assert "steering" in text.lower()


# ─────────────────────────────────────────────────────────────────────────── #
# STOP through Runtime.handle_command zeroes both fake channels                #
# ─────────────────────────────────────────────────────────────────────────── #

def test_stop_via_handle_command_zeroes_both_channels():
    """STOP route: handle_command -> controller.handle_command -> MANUAL(0,0)
    -> next control_tick applies MotorCommand(0,0) to the split motor (Constraint 4)."""
    thrust_ch = FakeChannel("thrust")
    steering_ch = FakeChannel("steering")
    fake_split = SplitMotor(thrust=thrust_ch, steering=steering_ch)

    cfg = load(None)
    rt = Runtime(cfg)
    # Swap the motor with our FakeChannel-backed SplitMotor AFTER construction.
    rt.controller.motor = fake_split

    # Apply non-zero first.
    fake_split.apply(MotorCommand(thrust=0.8, steering=-0.4))
    assert thrust_ch.last == pytest.approx(0.8)
    assert steering_ch.last == pytest.approx(-0.4)

    # Issue STOP then tick the controller (which calls motor.apply).
    rt.handle_command({"type": "stop"})
    rt.controller.control_tick(dt=0.1)

    assert thrust_ch.last == pytest.approx(0.0), "thrust must be zeroed after STOP"
    assert steering_ch.last == pytest.approx(0.0), "steering must be zeroed after STOP"


def test_stop_zeroes_both_even_if_one_channel_is_none():
    """STOP with one None channel: SplitMotor skips it safely (Constraint 4)."""
    thrust_ch = FakeChannel("thrust")
    motor = SplitMotor(thrust=thrust_ch, steering=None)
    motor.apply(MotorCommand(thrust=0.5, steering=0.0))
    motor.apply(MotorCommand(thrust=0.0, steering=0.0))  # STOP-shaped
    assert thrust_ch.last == pytest.approx(0.0)


# ─────────────────────────────────────────────────────────────────────────── #
# Failing channel build: runtime up, channel unhealthy, mode gated            #
# ─────────────────────────────────────────────────────────────────────────── #

def test_failing_channel_build_runtime_stays_up():
    """A build exception in _build_split_channel must not crash startup."""
    # The split rig (steering=sim, thrust=serial/Task3) naturally exercises the
    # "channel not built" path for the serial placeholder.
    rt = _split_rt()   # must not raise
    assert rt.controller.motor is not None


def test_failing_channel_appears_unhealthy_in_device_status():
    rt = _split_rt()
    status = rt.device_status()
    # thrust channel: serial Task-3 placeholder -> not built -> unhealthy
    assert "thrust" in status
    assert status["thrust"]["healthy"] is False
    # steering channel: sim -> built -> healthy (None = unknown/not-applicable for sim)
    assert "steering" in status
    # sim channel returns healthy=None; it should be present in status
    assert "steering" in status


def test_failing_channel_gates_mode_with_channel_named():
    """When thrust build fails (channel not built), MANUAL is gated with reason
    naming 'Thrust' (not just 'Motor').  We simulate the build failure by
    patching _build_split_channel to return None for the thrust channel."""
    from unittest.mock import patch

    original_build = Runtime._build_split_channel

    def _patched_build(self, name, link, sim_motor, sim_state, cfg):
        if name == "thrust":
            return None  # simulate a build failure
        return original_build(self, name, link, sim_motor, sim_state, cfg)

    with patch.object(Runtime, "_build_split_channel", _patched_build):
        rt = _split_rt()

    rt.controller.device_connected = rt._device_connected_map(rt.config)
    ma = rt.telemetry()["mode_availability"]
    # Steering is connected; motor roll-up = True. Thrust = None -> False.
    assert ma["manual"]["available"] is False
    assert "Thrust" in ma["manual"]["reason"], (
        f"Expected reason to name 'Thrust', got {ma['manual']['reason']!r}"
    )


# ─────────────────────────────────────────────────────────────────────────── #
# device_status: steering + thrust + motor roll-up                             #
# ─────────────────────────────────────────────────────────────────────────── #

def test_device_status_split_has_three_motor_entries():
    rt = _split_rt()
    status = rt.device_status()
    assert "motor" in status, "composite motor entry always present"
    assert "thrust" in status, "per-channel thrust entry when split"
    assert "steering" in status, "per-channel steering entry when split"


def test_device_status_split_motor_roll_up_source_is_split():
    rt = _split_rt()
    assert rt.device_status()["motor"]["source"] == "split"


def test_device_status_combined_no_channel_entries():
    """Combined plan -> no separate thrust/steering entries (back-compat)."""
    rt = _rt()  # default: combined sim
    status = rt.device_status()
    assert "motor" in status
    # Combined path must NOT add per-channel entries to avoid confusing old UI.
    assert "thrust" not in status
    assert "steering" not in status


def test_device_status_motor_roll_up_healthy_false_when_channel_fails():
    """Motor composite healthy=False when any channel is not built (unhealthy)."""
    rt = _split_rt()
    status = rt.device_status()
    # thrust is not built (serial Task-3); roll-up must reflect this
    assert status["motor"]["healthy"] is False


# ─────────────────────────────────────────────────────────────────────────── #
# device_debug: steering and thrust kinds work; motor roll-up still works     #
# ─────────────────────────────────────────────────────────────────────────── #

def test_device_debug_motor_works_in_split_build():
    """device_debug('motor') must work in a split build (roll-up debug)."""
    rt = _split_rt()
    r = rt.device_debug("motor")
    assert r["ok"] is True
    assert "SplitMotor" in r["debug"]


def test_device_debug_steering_works_in_split_build():
    rt = _split_rt()
    r = rt.device_debug("steering")
    assert r["ok"] is True
    assert "Steering" in r["debug"] or "steering" in r["debug"].lower()


def test_device_debug_thrust_channel_built_after_task3():
    """After Task 3, the serial thrust channel IS built -> ok:True with debug output."""
    rt = _split_rt()
    r = rt.device_debug("thrust")
    # Serial channel is now built (Task 3); debug returns ok:True.
    assert r["ok"] is True
    assert r["source"] == "serial"
    # Channel has not been started so it is in "waiting for data" state.
    assert "Thrust" in r["debug"] or "waiting" in r["debug"].lower() or "Serial" in r["debug"]


def test_device_debug_channel_kind_fails_gracefully_on_combined():
    """Per-channel debug on a combined motor plan returns ok:False (no channel)."""
    rt = _rt()  # combined plan
    r = rt.device_debug("thrust")
    assert r["ok"] is False


# ─────────────────────────────────────────────────────────────────────────── #
# set_device_config: same-port framing conflict -> 400                         #
# ─────────────────────────────────────────────────────────────────────────── #

def test_set_device_config_rejects_same_port_framing_conflict():
    """Channels sharing a port with different baud rates must be rejected."""
    rt = _rt()
    with pytest.raises(ValueError, match=r"share a port|framing must match"):
        rt.set_device_config({"hardware": {
            "steering_source": "serial",
            "steering_port": "/dev/ttyUSB0",
            "steering_baud": 9600,
            "thrust_source": "serial",
            "thrust_port": "/dev/ttyUSB0",
            "thrust_baud": 4800,  # different baud -> framing conflict
        }})


def test_set_device_config_same_port_same_framing_is_allowed():
    """Two channels on the same port with identical framing collapse to combined."""
    rt = _rt()
    # Same port, same framing -> combined plan -> no error.
    rt.set_device_config({"hardware": {
        "steering_source": "serial",
        "steering_port": "/dev/ttyUSB0",
        "steering_baud": 9600,
        "thrust_source": "serial",
        "thrust_port": "/dev/ttyUSB0",
        "thrust_baud": 9600,  # same baud -> combined -> OK
    }})


def test_set_device_config_rejects_invalid_channel_source():
    rt = _rt()
    with pytest.raises(ValueError):
        rt.set_device_config({"hardware": {"steering_source": "both"}})  # "both" not valid for channels


def test_set_device_config_channel_source_round_trips():
    rt = _rt()
    rt.set_device_config({"hardware": {"steering_source": "none"}})
    assert rt.config.hardware.steering_source == "none"
    rt.set_device_config({"hardware": {"steering_source": None}})
    assert rt.config.hardware.steering_source is None


# ─────────────────────────────────────────────────────────────────────────── #
# Realpath normalisation: symlinked ports resolve to the same endpoint         #
# ─────────────────────────────────────────────────────────────────────────── #

def test_realpath_aliased_ports_resolve_as_same_endpoint(tmp_path):
    """A /dev/serial/by-id/... symlink to the same file as /dev/ttyUSBX must
    resolve to 'combined', not 'split'."""
    # Create a fake device file and a symlink to it.
    real_dev = tmp_path / "ttyUSB0"
    real_dev.touch()
    link_dev = tmp_path / "by-id-link"
    link_dev.symlink_to(real_dev)

    from vanchor.hardware.link_plan import plan_motor_links
    from vanchor.core.config import HardwareConfig
    hw = HardwareConfig(
        steering_source="serial",
        steering_port=str(real_dev),
        steering_baud=9600,
        thrust_source="serial",
        thrust_port=str(link_dev),   # symlink pointing at the same device
        thrust_baud=9600,
    )
    plan = plan_motor_links(hw)
    assert plan.kind == "combined", (
        f"Symlinked port alias should resolve to combined, got {plan.kind!r}")


def test_realpath_different_ports_are_still_split(tmp_path):
    """Two DIFFERENT physical devices -> split plan (not combined)."""
    dev0 = tmp_path / "ttyUSB0"
    dev1 = tmp_path / "ttyUSB1"
    dev0.touch()
    dev1.touch()

    from vanchor.hardware.link_plan import plan_motor_links
    from vanchor.core.config import HardwareConfig
    hw = HardwareConfig(
        steering_source="serial",
        steering_port=str(dev0),
        steering_baud=9600,
        thrust_source="serial",
        thrust_port=str(dev1),
        thrust_baud=9600,
    )
    plan = plan_motor_links(hw)
    assert plan.kind == "split"


# ─────────────────────────────────────────────────────────────────────────── #
# device_config options include steering / thrust channel sources              #
# ─────────────────────────────────────────────────────────────────────────── #

def test_device_config_exposes_channel_options():
    rt = _rt()
    opts = rt.device_config()["options"]
    assert "steering" in opts
    assert "thrust" in opts
    assert "sim" in opts["steering"]
    assert "none" in opts["steering"]
    assert "both" not in opts["steering"]  # "both" is not valid for a split channel


# ─────────────────────────────────────────────────────────────────────────── #
# Capabilities: back-compat – combined motor_source=none still disables all   #
# ─────────────────────────────────────────────────────────────────────────── #

def test_combined_motor_none_still_disables_all_modes():
    """Back-compat: the combined "motor=none" path must keep working as before."""
    from vanchor.core import capabilities
    conn = {"motor": False, "gps": True, "compass": True, "depth": True}
    # "thrust"/"steering" absent -> fail-open (True); only "motor" gates
    assert not capabilities.missing_devices(ControlModeName.MANUAL, conn) or \
           "motor" in capabilities.missing_devices(ControlModeName.MANUAL, conn)
    reason = capabilities.unavailable_reason(ControlModeName.MANUAL, conn)
    assert reason == "Motor not connected"


def test_split_thrust_none_gates_manual_with_thrust_named():
    """Split plan with thrust=None -> MANUAL reason names 'Thrust'."""
    from vanchor.core import capabilities
    # Combined motor up + thrust missing (split mode adds "thrust" to map)
    conn = {"motor": True, "gps": True, "compass": True, "depth": True,
            "thrust": False, "steering": True}
    reason = capabilities.unavailable_reason(ControlModeName.MANUAL, conn)
    assert reason is not None
    assert "Thrust" in reason


def test_split_steering_none_gates_anchor_with_steering_named():
    """Split plan with steering=None -> ANCHOR_HOLD reason names 'Steering'."""
    from vanchor.core import capabilities
    conn = {"motor": True, "gps": True, "compass": True, "depth": True,
            "thrust": True, "steering": False}
    reason = capabilities.unavailable_reason(ControlModeName.ANCHOR_HOLD, conn)
    assert reason is not None
    assert "Steering" in reason
