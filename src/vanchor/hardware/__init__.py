"""Hardware abstraction: real devices + the pluggable driver registry.

``interfaces.py`` defines the ``Sensor`` / ``MotorController`` / ``Actuator``
seam that :mod:`vanchor.sim` mirrors, so nothing above the device layer can tell
simulation from hardware. ``serial_devices.py`` / ``serial_link.py`` implement
wired GPS/compass/motor over NMEA/serial. ``registry.py`` + the ``drivers/``
package are a plugin system: a driver drops a self-registering module and becomes
a selectable device source — no edit to the runtime's build seam. See
``docs/adding-a-device.md``.
"""
