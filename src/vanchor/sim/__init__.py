"""The boat simulator: physics + simulated sensors.

``simulator.py`` steps a boat model under a wind/current/gust environment with
battery drain. The default model is the Fossen 3-DOF surge-sway-yaw maneuvering
model (``fossen.py``, bow-mount aware); a lighter kinematic ``simple`` model is
also available. The simulated GPS/compass/depth devices emit **real NMEA**, so
the navigator, controller and UI behave identically whether the data comes from
here or from wired hardware.
"""
