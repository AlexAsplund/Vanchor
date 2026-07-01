"""Control layer: turn navigation state into motor commands.

Each control mode (manual, anchor-hold, heading-hold, waypoint, work-area,
along-contour, drift, orbit, trolling, …) implements ``activate(state)`` +
``update(state, dt) -> Setpoint``. The controller runs the active mode at a fixed
rate, converts its setpoint through the PID/steering logic into a
``MotorCommand`` under the safety limits, and drives the motor. Also home to the
autopilot calibration routines and the ML-based anchor hold.
"""
