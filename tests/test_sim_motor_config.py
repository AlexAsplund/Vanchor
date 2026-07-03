"""Roadmap #36: SimMotorController actuation shaping wired to config + the
device-config API.

Covers:
  * the ``SimMotorConfig`` shaping fields flow through ``Simulator`` into the
    ``SimMotorController`` (construction);
  * a ``Runtime`` built from a config with shaping reaches its sim motor;
  * ``Simulator.step`` advances the shaping (reverse-delay actually holds zero);
  * the device-config API (``set_device_config``) validates, persists
    (``devices.json``), reflects, and live-applies the shaping;
  * defaults (all zero) preserve the transparent-passthrough behaviour.
"""

from __future__ import annotations

import json

import pytest

from vanchor.app import Runtime
from vanchor.core.config import DEVICES_FILE, AppConfig, SimMotorConfig
from vanchor.core.models import Environment, MotorCommand
from vanchor.sim.simulator import Simulator


def test_simulator_forwards_shaping_to_motor() -> None:
    sim = Simulator(
        motor_reverse_delay_s=0.9,
        motor_thrust_slew_per_s=1.5,
        motor_thrust_lag_tau_s=0.4,
    )
    assert sim.motor._reverse_delay_s == pytest.approx(0.9)
    assert sim.motor._thrust_slew_per_s == pytest.approx(1.5)
    assert sim.motor._thrust_lag_tau_s == pytest.approx(0.4)
    assert sim.motor._shaping_enabled() is True


def test_simulator_defaults_are_passthrough() -> None:
    sim = Simulator()
    assert sim.motor._shaping_enabled() is False


def test_runtime_config_reaches_sim_motor() -> None:
    cfg = AppConfig()
    cfg.sim_motor = SimMotorConfig(reverse_delay_s=0.7, thrust_lag_tau_s=0.3)
    rt = Runtime(cfg)
    assert rt.simulator is not None
    assert rt.simulator.motor._reverse_delay_s == pytest.approx(0.7)
    assert rt.simulator.motor._thrust_lag_tau_s == pytest.approx(0.3)


def test_step_drives_reverse_delay_hold() -> None:
    """With reverse_delay configured, ``Simulator.step`` must hold the applied
    thrust at zero right after a direction flip (proves step() drives shaping)."""
    sim = Simulator(motor_reverse_delay_s=0.9, physics_hz=20.0)
    env = Environment()
    sim.motor.apply(MotorCommand(thrust=1.0))
    sim.step(0.05)  # establish forward
    sim.motor.apply(MotorCommand(thrust=-1.0))  # flip -> arm the hold
    # Advance 0.5 s of sim time; the applied command must still be gated to zero.
    for _ in range(10):
        sim.step(0.05)
        assert sim.motor.command.thrust == pytest.approx(0.0)


def test_step_default_is_bitwise_passthrough() -> None:
    """Default (no shaping) -> the applied command equals the request exactly."""
    sim = Simulator()
    sim.motor.apply(MotorCommand(thrust=0.42, steering=-0.2))
    sim.step(0.05)
    assert sim.motor.command.thrust == 0.42
    assert sim.motor.command.steering == -0.2


# --- device-config API -------------------------------------------------- #
def test_set_device_config_persists_and_live_applies(tmp_path) -> None:
    cfg = AppConfig(data_dir=str(tmp_path))
    rt = Runtime(cfg)
    out = rt.set_device_config(
        {"sim_motor": {"reverse_delay_s": 0.8, "thrust_slew_per_s": 2.0,
                       "thrust_lag_tau_s": 0.25}}
    )
    assert out["ok"] is True
    # Reflected in the live config.
    assert rt.config.sim_motor.reverse_delay_s == pytest.approx(0.8)
    # Live-applied to the running sim motor (no restart).
    assert rt.simulator.motor._reverse_delay_s == pytest.approx(0.8)
    assert rt.simulator.motor._thrust_slew_per_s == pytest.approx(2.0)
    assert rt.simulator.motor._thrust_lag_tau_s == pytest.approx(0.25)
    # Persisted to devices.json.
    on_disk = json.loads((tmp_path / DEVICES_FILE).read_text())
    assert on_disk["sim_motor"]["reverse_delay_s"] == pytest.approx(0.8)
    # And a fresh read reflects it.
    assert rt.sim_motor_config()["thrust_slew_per_s"] == pytest.approx(2.0)


def test_set_device_config_rejects_negative_shaping(tmp_path) -> None:
    rt = Runtime(AppConfig(data_dir=str(tmp_path)))
    with pytest.raises(ValueError):
        rt.set_device_config({"sim_motor": {"reverse_delay_s": -1.0}})


def test_set_device_config_partial_sim_motor_keeps_others(tmp_path) -> None:
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.sim_motor = SimMotorConfig(reverse_delay_s=0.5, thrust_lag_tau_s=0.4)
    rt = Runtime(cfg)
    rt.set_device_config({"sim_motor": {"thrust_slew_per_s": 3.0}})
    assert rt.config.sim_motor.reverse_delay_s == pytest.approx(0.5)  # untouched
    assert rt.config.sim_motor.thrust_lag_tau_s == pytest.approx(0.4)  # untouched
    assert rt.config.sim_motor.thrust_slew_per_s == pytest.approx(3.0)  # updated
