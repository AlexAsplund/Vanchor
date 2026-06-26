"""A small, dependency-free PID controller.

Kept deliberately simple and explicit so its behaviour is easy to reason about
and to unit-test. Supports output clamping with integral anti-windup.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PID:
    kp: float = 1.0
    ki: float = 0.0
    kd: float = 0.0
    setpoint: float = 0.0
    output_min: float = -1.0
    output_max: float = 1.0

    _integral: float = field(default=0.0, init=False, repr=False)
    _prev_error: float | None = field(default=None, init=False, repr=False)

    def reset(self) -> None:
        self._integral = 0.0
        self._prev_error = None

    def update(self, measurement: float, dt: float) -> float:
        """Standard form: error = setpoint - measurement."""
        return self.update_error(self.setpoint - measurement, dt)

    def update_error(self, error: float, dt: float) -> float:
        """Drive the loop from a pre-computed error.

        Useful for headings, where the error must be the *shortest* angular
        difference rather than a naive subtraction.
        """
        if dt <= 0:
            dt = 1e-6

        proportional = self.kp * error

        # Tentatively integrate, then clamp the *output* and only keep the
        # integral growth if it did not push us into saturation (anti-windup).
        new_integral = self._integral + error * dt
        integral_term = self.ki * new_integral

        derivative = 0.0
        if self._prev_error is not None:
            derivative = self.kd * (error - self._prev_error) / dt

        output = proportional + integral_term + derivative
        clamped = max(self.output_min, min(self.output_max, output))

        # Anti-windup (conditional integration): accept the new integral when we
        # are not saturated, or when the error would relieve the saturation
        # (saturated high & error<0, or saturated low & error>0).
        saturated_high = output > clamped
        saturated_low = output < clamped
        if (
            not (saturated_high or saturated_low)
            or (saturated_high and error < 0)
            or (saturated_low and error > 0)
        ):
            self._integral = new_integral

        self._prev_error = error
        return clamped
