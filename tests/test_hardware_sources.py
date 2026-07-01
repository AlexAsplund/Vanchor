"""Per-device sim/hardware source selection — simulation is one option per
component, and any mix (incl. a real servo on a simulated boat, or GPS from
external NMEA) is buildable. See app.Runtime device construction + HardwareConfig.
"""

from unittest.mock import patch

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
