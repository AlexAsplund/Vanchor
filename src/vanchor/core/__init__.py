"""Core domain types and shared services.

The vocabulary every other layer speaks, with no dependency on the sim, hardware
or UI layers: the immutable-ish data models (``GeoPoint``, ``BoatState``,
``MotorCommand``, setpoints), the mutable ``NavigationState``, the async event
bus, application config + persisted boat profiles, geodesy helpers, the reusable
PID controller, and observability (the in-memory log ring behind "View logs" and
the telemetry recorder).
"""
