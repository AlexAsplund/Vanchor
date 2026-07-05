"""u-blox M9N GPS driver (UBX binary protocol).

An alternative to the plain-NMEA GPS: it configures the receiver for marine use
(10 Hz, sea dynamic model, UBX-NAV-PVT out, NMEA off) and parses NAV-PVT, which
carries the **NED ground-velocity vector + per-fix accuracy** that NMEA can't --
the data the GNSS/INS fusion (nav.fusion) uses for a tighter spot-lock. Publishes
a rich :class:`GpsFix` on :data:`events.GPS_FIX_IN`.

Selectable as ``gps_source: ublox`` via the driver registry (#43) -- purely
additive, so every existing GPS source (sim / serial-NMEA / nmea-bridge / none)
is unaffected.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ...core import events
from ...core.events import EventBus
from ...core.models import GeoPoint, GpsFix
from ...nav import ubx
from ..interfaces import Sensor
from ..registry import register_driver
from ..serial_link import PySerialTransport, SerialTransport

logger = logging.getLogger(__name__)

_MAX_BUF = 8192  # cap the reassembly buffer so a garbage stream can't grow it


class UbloxGps(Sensor):
    """Reads UBX frames off a byte transport and republishes NAV-PVT as a rich
    :class:`GpsFix`. Reconnects through transport drops, like the NMEA devices."""

    def __init__(self, transport: SerialTransport, bus: EventBus | None = None, *,
                 configure: bool = True, name: str = "ublox-gps") -> None:
        self.transport = transport
        self.bus = bus
        self.configure = configure
        self._name = name
        self._task: asyncio.Task | None = None
        self._buf = b""
        self._stop = False
        # Per-device health for the telemetry block (mirrors the serial devices).
        self.healthy = False
        self.last_data_monotonic: float | None = None
        # Latest RAW decode for the Devices -> Debug live view.
        self._last_pvt: ubx.NavPvt | None = None
        self._last_pvt_monotonic: float | None = None
        self._frames_received = 0

    async def start(self) -> None:
        self._stop = False
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
        try:
            await self.transport.close()
        except Exception:  # noqa: BLE001 - best-effort
            logger.debug("%s: error closing transport", self._name)

    async def _configure(self) -> None:
        """Push the marine config to the receiver (best-effort; a receiver that
        rejects it still streams whatever it's set to)."""
        if not self.configure:
            return
        try:
            await self.transport.write(ubx.cfg_marine_10hz())
        except Exception as exc:  # noqa: BLE001
            logger.info("%s: could not send UBX config (%s); using current setup",
                        self._name, exc)

    async def _run(self) -> None:
        while not self._stop:
            try:
                await self.transport.open()
                await self._configure()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - port not ready -> back off
                logger.warning("%s: open failed (%s); retrying", self._name, exc)
                self.healthy = False
                await asyncio.sleep(2.0)
                continue
            try:
                await self._read_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - EOF / read error -> reconnect
                logger.warning("%s: read loop ended (%s); reconnecting", self._name, exc)
                self.healthy = False
                self._buf = b""
                await asyncio.sleep(1.0)

    async def _read_loop(self) -> None:
        while not self._stop:
            data = await self.transport.read(4096)  # EOFError when the port closes
            self._buf += data
            frames, self._buf = ubx.parse_stream(self._buf)
            if len(self._buf) > _MAX_BUF:  # runaway garbage -> drop it
                self._buf = self._buf[-_MAX_BUF:]
            for msg_cls, msg_id, payload in frames:
                if ubx.is_nav_pvt(msg_cls, msg_id):
                    await self._emit(payload)

    async def _emit(self, payload: bytes) -> None:
        pvt = ubx.decode_nav_pvt(payload)
        if pvt is None:
            return
        # Capture the raw decode BEFORE the validity gate so the Debug view can
        # show a "3D fix but gnssFixOK=0" state (a dropped-here fix is still the
        # most recent raw reading).
        self._last_pvt = pvt
        self._last_pvt_monotonic = time.monotonic()
        self._frames_received += 1
        if not pvt.valid:
            return
        self.healthy = True
        self.last_data_monotonic = time.monotonic()
        fix = GpsFix(
            point=GeoPoint(pvt.lat, pvt.lon),
            sog_knots=pvt.sog_knots, cog_deg=pvt.cog_deg, valid=True,
            vel_n_mps=pvt.vel_n_mps, vel_e_mps=pvt.vel_e_mps, vel_d_mps=pvt.vel_d_mps,
            h_acc_m=pvt.h_acc_m, s_acc_mps=pvt.s_acc_mps,
        )
        if self.bus is not None:
            await self.bus.publish(events.GPS_FIX_IN, fix)

    def debug(self) -> str:
        pvt = self._last_pvt
        if pvt is None:
            return f"{type(self).__name__}: waiting for data…"
        try:
            age = ("?" if self._last_pvt_monotonic is None
                   else f"{time.monotonic() - self._last_pvt_monotonic:.1f}")
            return (
                f"{type(self).__name__}\n"
                f"  frames   : {self._frames_received} (last {age}s ago)\n"
                # fix_type vs valid side-by-side: a 3D fix with valid=False is the
                # gnssFixOK=0 case (valid = gnssFixOK and fix_type>=2).
                f"  fix_type : {pvt.fix_type} (0=none 2=2D 3=3D)  "
                f"valid(gnssFixOK): {pvt.valid}\n"
                f"  num_sv   : {pvt.num_sv}\n"
                f"  lat/lon  : {pvt.lat:.7f}, {pvt.lon:.7f} °\n"
                f"  vel N/E/D: {pvt.vel_n_mps:.2f} / {pvt.vel_e_mps:.2f} / "
                f"{pvt.vel_d_mps:.2f} m/s\n"
                f"  sog/cog  : {pvt.sog_knots:.2f} kn / {pvt.cog_deg:.1f} °\n"
                f"  hAcc/sAcc: {pvt.h_acc_m:.2f} m / {pvt.s_acc_mps:.2f} m/s\n"
                f"  healthy  : {self.healthy}"
            )
        except Exception as exc:  # noqa: BLE001 - debug view must never raise
            return f"{type(self).__name__}: debug error ({exc})"


def _build(runtime: Any, cfg: Any) -> UbloxGps:
    """Registry build hook: a UBX GPS on the configured GPS port + framing."""
    hw = cfg.hardware
    transport = PySerialTransport(
        hw.gps_port, baudrate=hw.gps_baud, bytesize=hw.gps_bytesize,
        parity=hw.gps_parity, stopbits=hw.gps_stopbits,
    )
    saved = (getattr(hw, "device_settings", None) or {}).get("gps", {})
    return UbloxGps(transport, runtime.bus,
                    configure=bool(saved.get("configure", True)))


register_driver("gps", "ublox", _build, label="u-blox M9N (UBX)")
