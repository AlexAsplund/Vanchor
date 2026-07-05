"""Per-device sim/hardware source selection — simulation is one option per
component, and any mix (incl. a real servo on a simulated boat, or GPS from
external NMEA) is buildable. See app.Runtime device construction + HardwareConfig.
"""

import logging

from unittest.mock import MagicMock, patch

from vanchor.app import Runtime, _TeeMotor
from vanchor.core.config import HardwareConfig, load
from vanchor.core.models import BoatState, GeoPoint
from vanchor.sim.devices import SimGps


class _FakeMotor:
    def apply(self, command):
        pass

    async def flush(self):
        pass


def test_source_resolution():
    assert HardwareConfig().source("gps") == "sim"            # default: sim
    assert HardwareConfig(enabled=True).source("gps") == "serial"
    h = HardwareConfig(motor_source="both", gps_source="nmea")
    assert h.source("motor") == "both"
    assert h.source("gps") == "nmea"
    assert h.source("compass") == "sim"                       # unspecified -> follows enabled


def test_default_is_full_sim():
    rt = Runtime(load(None))
    assert rt.simulator is not None
    assert type(rt.gps).__name__ == "SimGps"
    assert type(rt.compass).__name__ == "SimCompass"


def test_motor_both_tees_sim_and_serial():
    cfg = load(None)
    cfg.hardware.motor_source = "both"
    with patch.object(Runtime, "_build_serial_motor", lambda self, c: _FakeMotor()):
        rt = Runtime(cfg)
    assert isinstance(rt.controller.motor, _TeeMotor)
    assert rt.simulator is not None                           # sim boat still runs
    assert len(rt.controller.motor._motors) == 2              # sim motor + real servo


def test_gps_from_external_nmea_not_blocked():
    cfg = load(None)
    cfg.hardware.gps_source = "nmea"
    rt = Runtime(cfg)
    assert rt.gps is None                                     # no internal GPS device
    assert rt.simulator is not None                          # other sim devices still present
    # the navigator still accepts external NMEA (TCP bridge / inject) as the fix
    sentence = SimGps(lambda: BoatState(point=GeoPoint(59.5, 18.1), heading_deg=0.0)).sample()
    rt.navigator.handle_sentence(sentence)
    assert abs(rt.state.position.lat - 59.5) < 0.01
    assert abs(rt.state.position.lon - 18.1) < 0.01


async def test_start_survives_unopenable_serial_device():
    """A saved hardware config whose serial port doesn't exist must NOT crash
    startup — the device is skipped (logged) so the UI stays reachable to fix it."""
    cfg = load(None)
    cfg.hardware.gps_source = "serial"   # no real /dev/ttyUSB* in CI
    rt = Runtime(cfg)
    await rt.start()                     # must not raise despite the open failure
    await rt.stop()


# --- Per-device GPS baud rate tests ---------------------------------------- #


def test_serial_gps_uses_gps_baud_not_shared_baudrate():
    """_build_serial_gps must open PySerialTransport with hw.gps_baud, not hw.baudrate."""
    cfg = load(None)
    cfg.hardware.gps_baud = 57600
    cfg.hardware.baudrate = 4800   # explicitly different — proves the right key is used

    rt = Runtime(load(None))      # default-sim runtime; we call the builder directly
    mock_transport_cls = MagicMock()

    with patch("vanchor.hardware.serial_link.PySerialTransport", mock_transport_cls):
        with patch("vanchor.hardware.serial_devices.SerialGps", MagicMock()):
            rt._build_serial_gps(cfg)

    mock_transport_cls.assert_called_once_with(
        cfg.hardware.gps_port, baudrate=57600,
        bytesize=8, parity="N", stopbits=1.0)  # default 8N1 framing


def _make_runtime_for_baud_tests():
    """Build a Runtime while suppressing setup_logging so caplog's handler is
    not stripped from the root logger (setup_logging clears ALL root handlers)."""
    with patch("vanchor.core.observability.setup_logging"):
        return Runtime(load(None))


def test_gps_baud_saturation_warning_fires_at_4800(caplog):
    """4800 baud at 5 Hz GPS should trigger the link-saturation warning."""
    cfg = load(None)
    cfg.hardware.gps_baud = 4800
    cfg.sensors.gps_hz = 5.0

    rt = _make_runtime_for_baud_tests()

    with caplog.at_level(logging.WARNING, logger="vanchor.app"):
        with patch("vanchor.hardware.serial_link.PySerialTransport", MagicMock()):
            with patch("vanchor.hardware.serial_devices.SerialGps", MagicMock()):
                rt._build_serial_gps(cfg)

    warning_msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("gps_baud too low" in m for m in warning_msgs), (
        f"Expected 'gps_baud too low' warning, got: {warning_msgs}"
    )


def test_gps_baud_no_saturation_warning_at_38400(caplog):
    """38400 baud (the default) at 5 Hz GPS must NOT trigger the warning."""
    cfg = load(None)
    cfg.hardware.gps_baud = 38400
    cfg.sensors.gps_hz = 5.0

    rt = _make_runtime_for_baud_tests()

    with caplog.at_level(logging.WARNING, logger="vanchor.app"):
        with patch("vanchor.hardware.serial_link.PySerialTransport", MagicMock()):
            with patch("vanchor.hardware.serial_devices.SerialGps", MagicMock()):
                rt._build_serial_gps(cfg)

    warning_msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("gps_baud too low" in m for m in warning_msgs), (
        f"Unexpected 'gps_baud too low' warning at 38400 baud: {warning_msgs}"
    )


def test_gps_baud_warning_threshold_at_boundary(caplog):
    """Verify warning fires just below and clears just above the 70 % threshold.

    Required bits/s = 5 Hz * 2 sentences * 82 bytes * 10 = 8200 bit/s.
    70 % threshold → need baud > 8200 / 0.70 ≈ 11715 baud.
    So 9600 (< 11715) → warning; 19200 (> 11715) → no warning.
    """
    rt = _make_runtime_for_baud_tests()

    def _run(baud, hz=5.0):
        cfg = load(None)
        cfg.hardware.gps_baud = baud
        cfg.sensors.gps_hz = hz
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="vanchor.app"):
            with patch("vanchor.hardware.serial_link.PySerialTransport", MagicMock()):
                with patch("vanchor.hardware.serial_devices.SerialGps", MagicMock()):
                    rt._build_serial_gps(cfg)
        return [r.getMessage() for r in caplog.records if "gps_baud too low" in r.getMessage()]

    assert _run(9600),  "9600 baud should warn at 5 Hz GPS"
    assert not _run(19200), "19200 baud should not warn at 5 Hz GPS"


def test_registry_driver_build_failure_does_not_crash_startup(tmp_path):
    """A saved config selecting a pluggable driver that can't be built (missing
    optional lib, no hardware) must NOT crash startup — building is eager, so the
    failure is caught and the device skipped, leaving the UI reachable to fix it.
    (Regression: compass_source='hwt901b' without the lib crashed __init__.)"""
    from vanchor.hardware import registry

    def _boom(runtime, cfg):
        raise RuntimeError("no hardware / lib here")

    registry.register_driver("compass", "_test_boom", _boom, label="boom")
    try:
        cfg = load(None)
        cfg.data_dir = str(tmp_path)          # isolate from the repo's vanchor_data/
        cfg.hardware.compass_source = "_test_boom"
        rt = Runtime(cfg)                     # must not raise
        assert rt.compass is None             # skipped; the rest of the boat runs
        assert rt.simulator is not None
    finally:
        registry._REGISTRY.pop(("compass", "_test_boom"), None)
