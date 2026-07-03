"""Roadmap #37: first-class, default-OFF sim fault injection + a runtime trigger.

Each fault must produce the intended degradation:
  * GPS dropout / EOF -> no fix emitted (the navigator's fix ages out / stays stale);
  * NMEA garbage -> the parser rejects the sentence;
  * GPS glitch -> a large position jump (spike-guard bait);
  * baud-saturation latency -> fixes are published late (stale);
  * compass freeze -> heading stuck at the latched value.

Plus the ``Runtime.set_sim_fault`` trigger, which must be a guarded no-op off-sim.
"""

from __future__ import annotations

import asyncio

import pytest

from vanchor.app import Runtime
from vanchor.core.config import AppConfig, HardwareConfig
from vanchor.core.events import EventBus
from vanchor.core.models import BoatState, GeoPoint
from vanchor.nav import nmea
from vanchor.sim.devices import SimCompass, SimGps

_HERE = GeoPoint(59.3293, 18.0686)


def _truth() -> BoatState:
    return BoatState(point=_HERE, heading_deg=42.0)


class _RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, object]] = []

    async def publish(self, topic: str, payload: object) -> None:
        self.published.append((topic, payload))


# --- defaults are OFF --------------------------------------------------- #
def test_faults_default_off() -> None:
    gps = SimGps(_truth, bus=None)
    assert gps.fault_dropout is False
    assert gps.fault_glitch is False
    assert gps.fault_garbage is False
    assert gps.fault_latency_s == 0.0
    compass = SimCompass(_truth, bus=None)
    assert compass.fault_freeze is False
    assert compass.fault_garbage is False
    # A clean sample parses fine (baseline).
    assert nmea.parse(gps.sample()) is not None
    assert nmea.parse(compass.sample()) is not None


# --- NMEA garbage -> parser rejects ------------------------------------- #
def test_gps_garbage_is_rejected_by_parser() -> None:
    gps = SimGps(_truth, bus=None)
    assert gps.set_fault("garbage", True) is True
    with pytest.raises(nmea.NmeaError):
        nmea.parse(gps.sample())


def test_compass_garbage_is_rejected_by_parser() -> None:
    compass = SimCompass(_truth, bus=None)
    compass.set_fault("garbage", True)
    with pytest.raises(nmea.NmeaError):
        nmea.parse(compass.sample())


# --- GPS glitch -> large position jump ---------------------------------- #
def test_gps_glitch_jumps_position() -> None:
    gps = SimGps(_truth, bus=None, position_noise_m=0.0)
    clean = nmea.parse(gps.sample())
    gps.set_fault("glitch", True, glitch_m=100.0)
    glitched = nmea.parse(gps.sample())
    from vanchor.core.geo import haversine_m

    assert haversine_m(clean.point, glitched.point) > 50.0


# --- compass freeze -> heading stuck ------------------------------------ #
def test_compass_freeze_sticks_heading() -> None:
    state = BoatState(point=_HERE, heading_deg=10.0)
    compass = SimCompass(lambda: state, bus=None, heading_noise_deg=0.0)
    compass.set_fault("freeze", True)
    first = nmea.parse(compass.sample())
    # Truth heading swings hard; the frozen compass must ignore it.
    state.heading_deg = 200.0
    second = nmea.parse(compass.sample())
    assert first.heading_deg == pytest.approx(second.heading_deg)
    # Releasing the fault lets it track truth again.
    compass.set_fault("freeze", False)
    third = nmea.parse(compass.sample())
    assert third.heading_deg == pytest.approx(200.0)


# --- GPS dropout -> nothing published (stale fix) ----------------------- #
async def test_gps_dropout_stops_publishing() -> None:
    bus = _RecordingBus()
    gps = SimGps(_truth, bus, update_hz=50.0)
    gps.fault_dropout = True
    await gps.start()
    await asyncio.sleep(0.12)
    await gps.stop()
    assert bus.published == []  # no fix emitted while dropped out


async def test_gps_recovers_after_dropout_cleared() -> None:
    bus = _RecordingBus()
    gps = SimGps(_truth, bus, update_hz=50.0)
    gps.set_fault("dropout", True)
    await gps.start()
    await asyncio.sleep(0.06)
    assert bus.published == []
    gps.set_fault("dropout", False)
    await asyncio.sleep(0.06)
    await gps.stop()
    assert len(bus.published) >= 1  # emitting again


# --- baud-saturation latency -> delayed publish ------------------------- #
async def test_gps_latency_delays_publish() -> None:
    bus = _RecordingBus()
    gps = SimGps(_truth, bus, update_hz=50.0)
    gps.set_fault("latency", True, latency_s=0.2)
    await gps.start()
    # Well under the latency window: nothing has been released yet.
    await asyncio.sleep(0.08)
    early = len(bus.published)
    # Past the latency window: buffered fixes start coming out.
    await asyncio.sleep(0.25)
    await gps.stop()
    late = len(bus.published)
    assert early == 0
    assert late >= 1


# --- runtime trigger ---------------------------------------------------- #
def test_runtime_set_sim_fault_toggles_device() -> None:
    rt = Runtime(AppConfig())
    res = rt.set_sim_fault("nmea_garbage", True)
    assert res["applied"] is True
    assert rt.gps.fault_garbage is True
    # via handle_command (the command channel used by the API).
    rt.handle_command({"type": "sim_fault", "name": "compass_freeze", "enabled": True})
    assert rt.compass.fault_freeze is True
    # Params flow through.
    rt.set_sim_fault("gps_glitch", True, glitch_m=25.0)
    assert rt.gps.fault_glitch is True
    assert rt.gps.fault_glitch_m == pytest.approx(25.0)


def test_runtime_set_sim_fault_unknown_is_noop() -> None:
    rt = Runtime(AppConfig())
    res = rt.set_sim_fault("does_not_exist", True)
    assert res["applied"] is False
    assert res["reason"] == "unknown fault"


def test_runtime_set_sim_fault_guarded_off_sim() -> None:
    """On real hardware (no simulator) the trigger must be a safe no-op."""
    cfg = AppConfig()
    cfg.hardware = HardwareConfig(
        enabled=True, gps_source="serial", compass_source="serial",
        depth_source="nmea", motor_source="serial",
    )
    from unittest.mock import patch

    class _Fake:
        def apply(self, command):
            pass

        async def flush(self):
            pass

    with patch.object(Runtime, "_build_serial_motor", lambda self, c: _Fake()), \
         patch.object(Runtime, "_build_serial_gps", lambda self, c: _Fake()), \
         patch.object(Runtime, "_build_serial_compass", lambda self, c: _Fake()):
        rt = Runtime(cfg)
    assert rt.simulator is None
    res = rt.set_sim_fault("gps_dropout", True)
    assert res["applied"] is False
    assert res["reason"] == "no simulator"
