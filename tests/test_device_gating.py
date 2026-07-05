"""Device-availability gating: a "Not connected" (source "none") device disables
the modes/functions that need it — in telemetry (for the UI) and in the
controller (which refuses to engage them)."""
import tempfile

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.core.models import ControlModeName
from vanchor.core import capabilities
from vanchor.hardware.interfaces import NullMotor

# Isolate the data dir once for the module: several tests here call
# set_device_config(), which PERSISTS -- without this they would write the repo's
# real vanchor_data/devices.json and clobber a live device config.
_ISOLATED_DATA_DIR = tempfile.mkdtemp(prefix="vanchor-gating-")


def _rt():
    cfg = load(None)
    cfg.data_dir = _ISOLATED_DATA_DIR
    return Runtime(cfg)


# --- the capabilities map (pure) ------------------------------------------- #
def test_missing_devices_fail_open_on_unknown():
    # an empty connected map => everything connected (never wrongly disabled)
    assert capabilities.missing_devices(ControlModeName.WAYPOINT, {}) == []


def test_reason_names_the_missing_device():
    conn = {"gps": False, "compass": True, "depth": True, "motor": True}
    assert capabilities.unavailable_reason(ControlModeName.WAYPOINT, conn) == "GPS not connected"
    assert capabilities.unavailable_reason(ControlModeName.MANUAL, conn) is None


def test_manual_needs_motor_only():
    conn = {"gps": False, "motor": True}
    assert capabilities.missing_devices(ControlModeName.MANUAL, conn) == []
    conn = {"motor": False}
    assert capabilities.missing_devices(ControlModeName.MANUAL, conn) == ["motor"]


# --- telemetry surface ----------------------------------------------------- #
def test_default_sim_all_connected_and_available():
    t = _rt().telemetry()
    assert all(v["connected"] for v in t["devices"].values())
    assert all(m["available"] for m in t["mode_availability"].values())


def test_gps_none_disables_gps_modes_only():
    rt = _rt()
    rt.config.hardware.gps_source = "none"
    rt.controller.device_connected = rt._device_connected_map(rt.config)
    t = rt.telemetry()
    assert t["devices"]["gps"]["connected"] is False
    ma = t["mode_availability"]
    assert ma["anchor_hold"]["available"] is False
    assert ma["anchor_hold"]["reason"] == "GPS not connected"
    assert ma["waypoint"]["available"] is False
    # heading-hold + manual don't need GPS
    assert ma["manual"]["available"] is True
    assert ma["heading_hold"]["available"] is True


def test_motor_none_builds_nullmotor_and_disables_everything():
    rt = _rt()
    rt.config.hardware.motor_source = "none"
    dev = rt._construct_devices(rt.config)
    assert isinstance(dev["motor"], NullMotor)
    rt.controller.device_connected = rt._device_connected_map(rt.config)
    ma = rt.telemetry()["mode_availability"]
    assert not any(m["available"] for m in ma.values())
    assert ma["manual"]["reason"] == "Motor not connected"


def test_nullmotor_apply_is_safe_noop():
    from vanchor.core.models import MotorCommand
    NullMotor().apply(MotorCommand(thrust=1.0, steering=0.5))  # must not raise


# --- controller refusal (safety backstop) ---------------------------------- #
def test_controller_refuses_unavailable_mode():
    rt = _rt()
    rt.config.hardware.gps_source = "none"
    rt.controller.device_connected = rt._device_connected_map(rt.config)
    before = rt.state.mode
    rt.controller.handle_command({"type": "goto", "lat": 59.0, "lon": 13.0})
    assert rt.state.mode == before  # never engaged the GPS-dependent mode


def test_controller_allows_available_mode():
    rt = _rt()
    rt.controller.device_connected = rt._device_connected_map(rt.config)  # all connected
    rt.controller.handle_command({"type": "manual", "thrust": 0.0, "steering": 0.0})
    assert rt.state.mode is ControlModeName.MANUAL


# --- config: "none" is a valid source now ---------------------------------- #
def test_none_is_a_valid_device_source():
    rt = _rt()
    assert "none" in rt._SENSOR_SOURCES and "none" in rt._MOTOR_SOURCES
    opts = rt.device_config()["options"]
    assert "none" in opts["sensor"] and "none" in opts["motor"]
    # round-trips through the device-config API
    rt.set_device_config({"hardware": {"gps_source": "none"}})
    assert rt.config.hardware.gps_source == "none"


def test_per_port_serial_framing_round_trips_and_validates():
    rt = _rt()
    rt.set_device_config({"hardware": {"gps_baud": 9600, "gps_bytesize": 7,
                                       "gps_parity": "e", "gps_stopbits": 2}})
    c = rt.config.hardware
    assert (c.gps_baud, c.gps_bytesize, c.gps_parity, c.gps_stopbits) == (9600, 7, "E", 2.0)
    import pytest
    for bad in ({"gps_parity": "X"}, {"gps_bytesize": 9}, {"gps_stopbits": 3}):
        with pytest.raises(ValueError):
            rt.set_device_config({"hardware": bad})


def test_serial_ports_are_enumerated():
    # Auto-detect returns {path, description, stable}; never raises. Stable
    # (by-id) entries sort first so the recommended bind is on top.
    ports = _rt().list_serial_ports()
    assert isinstance(ports, list)
    for p in ports:
        assert {"path", "description", "stable"} <= set(p)
    stables = [i for i, p in enumerate(ports) if p["stable"]]
    unstables = [i for i, p in enumerate(ports) if not p["stable"]]
    if stables and unstables:
        assert max(stables) < min(unstables)  # all stable entries precede raw ones


def test_a_source_can_be_reset_to_auto_null():
    # Regression: a present-but-null source must reset to Auto (was skipped by the
    # merge, so you could never leave "none"/"sim" once set).
    rt = _rt()
    for first in ("none", "sim", "serial"):
        rt.set_device_config({"hardware": {"gps_source": first}})
        assert rt.config.hardware.gps_source == first
        rt.set_device_config({"hardware": {"gps_source": None}})  # Auto
        assert rt.config.hardware.gps_source is None
    # a field absent from the payload is preserved (not wrongly reset)
    rt.set_device_config({"hardware": {"gps_source": "serial"}})
    rt.set_device_config({"hardware": {"compass_source": "sim"}})
    assert rt.config.hardware.gps_source == "serial"
