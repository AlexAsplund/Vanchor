"""Phone-as-sensor devices: use a connected phone's GPS / compass as the boat's.

The PWA can stream the browser's Geolocation fixes and device-orientation
heading over the existing WebSocket; selecting source ``phone`` for GPS and/or
compass turns those streams into ordinary vanchor sensor inputs.

**Disclaimer (surfaced in the UI too):** phone sensors are CRUDE and vary wildly
between devices — browser geolocation can wander tens of metres, phone
compasses are frequently miscalibrated, and update rates are whatever the
browser feels like. Fine for experimenting on a bench or as a get-you-home
fallback; not a navigation-grade source. (Browser geolocation also requires the
HTTPS address — see ``server.https_port``.)

**Single-feeder arbitration:** exactly ONE connected client may feed a sensor
kind at a time. The first client to stream claims the slot; other clients'
samples are rejected. The slot is freed ONLY when the feeding client
disconnects (another phone then takes over automatically on its next sample).
Nothing else reassigns it — in particular, taking the HELM never touches the
feeder role: commanding the boat and feeding it sensors are independent.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Awaitable, Callable

from ...core import events
from ...core.events import EventBus
from ...core.models import GeoPoint, GpsFix
from ...nav import nmea
from ..interfaces import Sensor
from ..registry import register_driver

logger = logging.getLogger("vanchor.phone")

_STALE_S = 5.0  # no sample for this long -> unhealthy

Sink = Callable[[dict], Awaitable[None]]


class PhoneSensorHub:
    """Routes phone-sensor WS messages to the built phone devices, enforcing the
    one-feeder-per-kind rule described in the module docstring."""

    def __init__(self, mono_fn: Callable[[], float] = time.monotonic) -> None:
        self._mono = mono_fn
        self._sinks: dict[str, Sink] = {}
        self._feeder: dict[str, Any] = {}       # kind -> client id
        self._last: dict[str, float] = {}       # kind -> mono of last accepted

    def register_sink(self, kind: str, sink: Sink) -> None:
        self._sinks[kind] = sink

    def unregister_sink(self, kind: str) -> None:
        self._sinks.pop(kind, None)
        self._feeder.pop(kind, None)
        self._last.pop(kind, None)

    async def ingest(self, kind: str, client_id: Any, data: dict) -> str:
        """Feed one sample. Returns ``accepted`` / ``rejected`` (another client
        holds the slot) / ``inactive`` (no phone device built for this kind)."""
        sink = self._sinks.get(kind)
        if sink is None:
            return "inactive"
        holder = self._feeder.get(kind)
        if holder is None:
            self._feeder[kind] = client_id
            logger.info("phone %s feeder claimed by client %s", kind, client_id)
        elif holder != client_id:
            return "rejected"
        self._last[kind] = self._mono()
        try:
            await sink(data)
        except Exception:  # noqa: BLE001 - a bad sample must not kill the WS loop
            logger.exception("phone %s sample failed; dropped", kind)
        return "accepted"

    def on_disconnect(self, client_id: Any) -> None:
        """Free any slots this client held — the ONLY automatic reassignment
        path (the next client to stream takes over)."""
        for kind, holder in list(self._feeder.items()):
            if holder == client_id:
                del self._feeder[kind]
                logger.info("phone %s feeder disconnected; slot open", kind)

    def feeder(self, kind: str) -> Any:
        return self._feeder.get(kind)

    def age_s(self, kind: str) -> float | None:
        t = self._last.get(kind)
        return None if t is None else max(0.0, self._mono() - t)


class _PhoneSensorBase(Sensor):
    kind = ""

    def __init__(self, hub: PhoneSensorHub, bus: EventBus | None,
                 mono_fn: Callable[[], float] = time.monotonic) -> None:
        self.hub = hub
        self.bus = bus
        self._mono = mono_fn
        self._count = 0
        self._last_desc = ""

    async def start(self) -> None:
        self.hub.register_sink(self.kind, self._on_sample)

    async def stop(self) -> None:
        self.hub.unregister_sink(self.kind)

    async def _on_sample(self, data: dict) -> None:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    @property
    def healthy(self) -> bool:
        age = self.hub.age_s(self.kind)
        return age is not None and age < _STALE_S

    def debug(self) -> str:
        try:
            feeder = self.hub.feeder(self.kind)
            age = self.hub.age_s(self.kind)
            return (f"{type(self).__name__}\n"
                    f"  feeder  : {'client ' + str(feeder) if feeder is not None else 'none (waiting for a phone to stream)'}\n"
                    f"  samples : {self._count}"
                    + (f" (last {age:.1f}s ago)" if age is not None else "") + "\n"
                    f"  last    : {self._last_desc or '—'}\n"
                    f"  note    : phone sensors are crude; accuracy varies wildly")
        except Exception:  # noqa: BLE001
            return f"{type(self).__name__}: debug unavailable"


class PhoneGps(_PhoneSensorBase):
    """Browser Geolocation fixes -> rich GpsFix on ``gps.fix_in`` (the accuracy
    field rides along, so the fusion/accuracy-weighted paths see it)."""

    kind = "gps"

    async def _on_sample(self, data: dict) -> None:
        lat, lon = data.get("lat"), data.get("lon")
        if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))
                and math.isfinite(lat) and math.isfinite(lon)):
            return
        speed = data.get("speed")
        heading = data.get("heading")
        acc = data.get("accuracy")
        sog_mps = float(speed) if isinstance(speed, (int, float)) and math.isfinite(speed) and speed >= 0 else 0.0
        cog = float(heading) % 360.0 if isinstance(heading, (int, float)) and math.isfinite(heading) else 0.0
        fix = GpsFix(
            point=GeoPoint(float(lat), float(lon)),
            sog_knots=sog_mps * 1.9438445, cog_deg=cog, valid=True,
            h_acc_m=(float(acc) if isinstance(acc, (int, float)) and math.isfinite(acc) else None),
        )
        self._count += 1
        self._last_desc = (f"{fix.point.lat:.6f},{fix.point.lon:.6f} "
                           f"±{fix.h_acc_m or 0:.0f}m {fix.sog_knots:.1f}kn")
        if self.bus is not None:
            await self.bus.publish(events.GPS_FIX_IN, fix)


class PhoneCompass(_PhoneSensorBase):
    """Browser device-orientation heading (MAGNETIC) -> HDM on ``nmea.in`` so
    the navigator's declination pipeline applies as for any magnetic compass."""

    kind = "compass"

    async def _on_sample(self, data: dict) -> None:
        heading = data.get("heading")
        if not (isinstance(heading, (int, float)) and math.isfinite(heading)):
            return
        h = float(heading) % 360.0
        self._count += 1
        self._last_desc = f"{h:.1f}° magnetic"
        if self.bus is not None:
            await self.bus.publish(events.NMEA_IN, nmea.encode_hdm(h))


def ensure_hub(runtime: Any) -> PhoneSensorHub:
    hub = getattr(runtime, "phone_hub", None)
    if hub is None:
        hub = PhoneSensorHub(getattr(runtime, "_mono_fn", time.monotonic))
        runtime.phone_hub = hub
    return hub


def _build_gps(runtime: Any, cfg: Any) -> PhoneGps:
    return PhoneGps(ensure_hub(runtime), runtime.bus)


def _build_compass(runtime: Any, cfg: Any) -> PhoneCompass:
    return PhoneCompass(ensure_hub(runtime), runtime.bus)


register_driver("gps", "phone", _build_gps, label="Phone (this device)")
register_driver("compass", "phone", _build_compass, label="Phone (this device)")
