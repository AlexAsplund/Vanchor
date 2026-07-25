"""Back-compat facade for the old monolithic ``vanchor.app`` module.

The :class:`Runtime` and the ``vanchor`` console-script ``main()`` were relocated
into the ``vanchor.runtime`` package (issue #80, the capstone of decomposing this
module -- epic #68). This module now only re-exports their new homes so that every
historical ``from vanchor.app import <name>`` keeps working unchanged.

New code should import from the real homes:

* ``from vanchor.runtime.runtime import Runtime``
* ``from vanchor.runtime.cli import main``
"""
from __future__ import annotations

# The Runtime + all the module-level symbols the old app.py exposed. runtime.runtime
# imports these same names from ..core / ..nav / ..sim / ..hardware / sibling
# collaborators, so re-exporting from it preserves the full historical namespace
# (vanchor.app.Controller, vanchor.app.registry, vanchor.app._TeeMotor, ...).
from .runtime.runtime import (  # noqa: F401
    AnchorConfig,
    AppConfig,
    Bathymetry,
    BatteryLadder,
    BoatSetup,
    BoatState,
    CalibrationRunner,
    CommandDispatcher,
    Controller,
    ControlModeName,
    DepthMap,
    DepthService,
    DeviceManager,
    DriftConfig,
    Environment,
    EventBus,
    FollowApbConfig,
    GainSchedule,
    GeoPoint,
    HardwareGlue,
    HardwareScan,
    HardwareWatchdog,
    Helm,
    NavGlue,
    NavigationState,
    Navigator,
    PID,
    Runtime,
    SafetyConfig,
    SafetyFloor,
    SafetyRuntime,
    SensorGuardConfig,
    SessionService,
    SimCompass,
    SimDepthSounder,
    SimGps,
    Simulator,
    TelemetryBuilder,
    TripLog,
    Waypoint,
    WaypointConfig,
    _ENV_PERSIST_KEYS,
    _MANUAL_UNDERWAY_THRUST_EPS,
    _NeutralChannelMotor,
    _SimChannelState,
    _SimSteeringChannel,
    _SimThrustChannel,
    _TeeMotor,
    _UNDERWAY_MODES,
    _build_battery_config,
    _build_boat_params,
    _make_fusion,
    _make_gps_filter,
    _mask_connector_settings,
    _overlay_menu_values,
    _start_motor,
    _stop_motor,
    _thrust_yaw_ff_norm,
    apply_demo_mode,
    asdict,
    demo_route_waypoints,
    events,
    load,
    load_connectors,
    load_drivers,
    logger,
    observability,
    registry,
)
from .runtime.cli import main  # noqa: F401

__all__ = [
    "Runtime",
    "main",
    "apply_demo_mode",
    "demo_route_waypoints",
    "_TeeMotor",
    "_NeutralChannelMotor",
    "_SimChannelState",
    "_SimThrustChannel",
    "_SimSteeringChannel",
    "_start_motor",
    "_stop_motor",
    "_UNDERWAY_MODES",
    "_MANUAL_UNDERWAY_THRUST_EPS",
    "_ENV_PERSIST_KEYS",
]


if __name__ == "__main__":
    main()
