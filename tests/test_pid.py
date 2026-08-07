import math

import pytest

from vanchor.core.pid import PID


def test_proportional_only():
    pid = PID(kp=2.0, ki=0.0, kd=0.0, setpoint=10.0, output_min=-100, output_max=100)
    # error = 10 - 4 = 6 -> output 12
    assert pid.update(4.0, dt=1.0) == pytest.approx(12.0)


def test_output_clamped():
    pid = PID(kp=1.0, setpoint=100.0, output_min=-1.0, output_max=1.0)
    assert pid.update(0.0, dt=1.0) == 1.0
    assert pid.update(200.0, dt=1.0) == -1.0


def test_integral_accumulates_then_anti_windup_holds():
    pid = PID(kp=0.0, ki=1.0, kd=0.0, setpoint=0.0, output_min=-1.0, output_max=1.0)
    # Constant positive error drives the integral until it saturates at +1.
    for _ in range(100):
        out = pid.update_error(1.0, dt=0.1)
    assert out == pytest.approx(1.0)
    # Reverse the error: anti-windup means it unwinds promptly, not after a
    # huge accumulated backlog.
    out = pid.update_error(-1.0, dt=0.1)
    assert out < 1.0


def test_derivative_responds_to_change():
    pid = PID(kp=0.0, ki=0.0, kd=1.0, setpoint=0.0, output_min=-100, output_max=100)
    pid.update_error(0.0, dt=1.0)
    # error jumps from 0 to 5 over dt=1 -> derivative 5
    assert pid.update_error(5.0, dt=1.0) == pytest.approx(5.0)


def test_reset_clears_state():
    pid = PID(kp=0.0, ki=1.0, kd=0.0, output_min=-10, output_max=10)
    pid.update_error(1.0, dt=1.0)
    pid.reset()
    # After reset the integral is gone, so a fresh small error gives a small out.
    assert pid.update_error(0.5, dt=1.0) == pytest.approx(0.5)


def test_nonfinite_error_returns_neutral_and_freezes_state():
    # A NaN/inf error must not poison the integral (NaN + x = NaN forever) nor
    # saturate the output via the clamp. It returns the safe neutral (0.0) and
    # leaves prior state intact, so the next good sample behaves normally.
    pid = PID(kp=1.0, ki=1.0, kd=0.0, setpoint=0.0, output_min=-1.0, output_max=1.0)
    pid.update_error(0.3, dt=1.0)          # build some integral
    assert pid.update_error(float("nan"), dt=1.0) == 0.0
    assert pid.update_error(float("inf"), dt=1.0) == 0.0
    # Integral was not poisoned: a zero error now yields only the kept integral,
    # a finite number (not NaN).
    out = pid.update_error(0.0, dt=1.0)
    assert math.isfinite(out) and out == pytest.approx(0.3)


def test_nonfinite_dt_returns_neutral():
    pid = PID(kp=1.0, setpoint=0.0, output_min=-1.0, output_max=1.0)
    assert pid.update_error(0.5, dt=float("nan")) == 0.0
