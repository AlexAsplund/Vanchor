"""NMEA 2000 connector — CAN transport seam + ingress/egress bridge.

Bridges a CAN bus running NMEA 2000 to the Vanchor event bus:

**Ingress** (CAN → bus):

* PGN 129025 (Position) + 129026 (COG/SOG) are paired within a 1.0 s window
  (using the injectable clock) to build a :class:`~vanchor.core.models.GpsFix`
  and publish it on ``"gps.fix_in"``.
* PGN 127250 (Vessel Heading) is published as an NMEA HDT sentence on
  ``"nmea.in"``. The sentence is always emitted as HDT regardless of the
  heading reference field — when the source is not a true-heading reference
  (ref ≠ 0) no magnetic→true correction is applied and the value is forwarded
  as-is. Downstream navigator code should treat it as approximate in that case.
* PGN 128267 (Water Depth) is encoded as an NMEA DPT sentence and published
  on ``"nmea.in"``, including the transducer offset field.
* PGN 130306 (Wind) is decoded and exposed in :meth:`debug` only; there is no
  existing ingest path for wind data in the navigator.
* Unknown PGNs are silently ignored.

**Egress** (bus → CAN):

* Subscribes to ``"telemetry"`` via the context. At most every
  ``egress_interval_s`` (default 0.5 s = 2 Hz) the last received telemetry
  frame is encoded as PGN 129025 (position) and PGN 129026 (COG/SOG) and
  written to the transport. Frames are skipped when ``position`` is ``None``.
  COG is taken from ``heading_deg`` (the closest approximation in the telemetry
  dict; no dedicated COG key is present).

**Reconnect** on transport errors: mirrors the serial-device reconnect loop.
Open failures back off 2 s; read loop errors back off ``reconnect_delay_s``
(default 1 s, injectable for tests).

**CanTransport seam**: :class:`CanTransport` is the abstract interface; tests
use :class:`FakeCanTransport`. A real SocketCAN implementation is provided at
the bottom of the module, guarded so it only runs on Linux with a CAN socket
available.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import math
import time
from typing import Any, Callable

from ..core.models import GeoPoint, GpsFix
from ..nav import n2k
from ..nav import nmea
from .base import Connector, ConnectorManifest
from .context import ConnectorContext
from .registry import register_connector

logger = logging.getLogger("vanchor.connectors.nmea2000")

_MPS_TO_KNOTS: float = 1.9438445

# CAN source address we claim when sending our own frames.
_OWN_SRC: int = 0xFF  # unclaimed / self-configurable address (N2K address claim not implemented)

# ─────────────────────────────────────────────────────────────────────────────
# Manifest
# ─────────────────────────────────────────────────────────────────────────────

MANIFEST = ConnectorManifest(
    name="nmea2000",
    label="NMEA 2000 Bridge",
    description=(
        "Bridges a NMEA 2000 CAN bus to the internal event bus: receives "
        "position, heading, depth and wind data from connected devices and "
        "optionally re-broadcasts the boat's own position back onto the bus."
    ),
    consumes=("telemetry",),
    produces=("gps.fix_in", "nmea.in"),
    control=False,
    grant_lines=(
        "Read position, COG/SOG, heading and depth from the NMEA 2000 bus and "
        "inject them onto the internal event bus",
        "Send the boat's current position and speed back onto the NMEA 2000 bus "
        "(at most 2 Hz)",
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# CanTransport seam
# ─────────────────────────────────────────────────────────────────────────────


class CanTransport(abc.ABC):
    """Abstract CAN transport.  Tests use :class:`FakeCanTransport`; production
    code uses the SocketCAN implementation below.
    """

    @abc.abstractmethod
    async def open(self) -> None:
        """Open / connect the CAN interface."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Close the CAN interface."""

    @abc.abstractmethod
    async def recv(self) -> tuple[int, bytes]:
        """Receive one CAN frame.  Returns ``(can_id, data)``.

        Blocks until a frame is available.  Raises :exc:`EOFError` when the
        interface is closed; raises :exc:`asyncio.CancelledError` when the
        calling task is cancelled.
        """

    @abc.abstractmethod
    async def send(self, can_id: int, data: bytes) -> None:
        """Send one CAN frame ``(can_id, data)``."""


# ─────────────────────────────────────────────────────────────────────────────
# FakeCanTransport — for tests
# ─────────────────────────────────────────────────────────────────────────────


class FakeCanTransport(CanTransport):
    """In-memory CAN transport for deterministic tests.

    Push inbound frames with :meth:`feed` (picked up by :meth:`recv`) and
    inspect everything sent by the connector via :attr:`sent`.  Mirrors the
    ``FakeSerialTransport`` style from :mod:`vanchor.hardware.serial_link`.
    """

    def __init__(self) -> None:
        # Queue items: (can_id, data) | None (EOF) | BaseException
        self._inbound: asyncio.Queue[tuple[int, bytes] | None | BaseException] = (
            asyncio.Queue()
        )
        self.sent: list[tuple[int, bytes]] = []
        self.opened: bool = False
        self.closed: bool = False
        self.open_calls: int = 0

    # -- test helpers -------------------------------------------------------- #

    def feed(self, can_id: int, data: bytes) -> None:
        """Enqueue an inbound CAN frame for the next :meth:`recv` call."""
        self._inbound.put_nowait((can_id, bytes(data)))

    def feed_eof(self) -> None:
        """Signal end-of-stream; the next :meth:`recv` call will raise :exc:`EOFError`."""
        self._inbound.put_nowait(None)

    def feed_exception(self, exc: BaseException) -> None:
        """Inject ``exc`` to be raised by the next :meth:`recv` call."""
        self._inbound.put_nowait(exc)

    # -- CanTransport -------------------------------------------------------- #

    async def open(self) -> None:
        self.open_calls += 1
        self.opened = True
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def recv(self) -> tuple[int, bytes]:
        item = await self._inbound.get()
        if item is None:
            raise EOFError("FakeCanTransport closed")
        if isinstance(item, BaseException):
            raise item
        return item  # narrowed to tuple[int, bytes] by the None/BaseException guards above

    async def send(self, can_id: int, data: bytes) -> None:
        self.sent.append((can_id, bytes(data)))


# ─────────────────────────────────────────────────────────────────────────────
# SocketCAN implementation (Linux only, guarded)
# ─────────────────────────────────────────────────────────────────────────────


class _SocketCanTransport(CanTransport):  # pragma: no cover
    """SocketCAN transport using ``python-can`` if available, else stdlib
    ``socket.AF_CAN``.  Runs on Linux only and is never instantiated in tests.

    ``channel`` is the CAN interface name, e.g. ``"can0"``.
    """

    def __init__(self, channel: str = "can0") -> None:  # pragma: no cover
        self._channel = channel
        self._bus: Any = None  # python-can Bus or None
        self._sock: Any = None  # raw socket or None
        self._use_python_can: bool = False

    async def open(self) -> None:  # pragma: no cover
        try:
            import can  # noqa: PLC0415
            self._bus = can.interface.Bus(self._channel, interface="socketcan")
            self._use_python_can = True
            logger.info("nmea2000: opened %s via python-can", self._channel)
        except ImportError:
            import socket  # noqa: PLC0415
            # Fall back to stdlib AF_CAN
            sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            sock.bind((self._channel,))
            sock.setblocking(False)
            self._sock = sock
            logger.info("nmea2000: opened %s via AF_CAN socket", self._channel)

    async def close(self) -> None:  # pragma: no cover
        if self._bus is not None:
            try:
                self._bus.shutdown()
            except Exception:  # noqa: BLE001
                pass
            self._bus = None
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:  # noqa: BLE001
                pass
            self._sock = None

    async def recv(self) -> tuple[int, bytes]:  # pragma: no cover
        if self._use_python_can and self._bus is not None:
            loop = asyncio.get_event_loop()
            msg = await loop.run_in_executor(None, self._bus.recv, 1.0)
            if msg is None:
                raise EOFError("CAN timeout / no frame")
            # python-can: is_extended_id for 29-bit IDs
            if not msg.is_extended_id:
                return await self.recv()  # skip 11-bit frames
            return msg.arbitration_id, bytes(msg.data)
        # AF_CAN path: raw 16-byte frame format
        import select  # noqa: PLC0415
        import struct  # noqa: PLC0415
        while True:
            r, _, _ = select.select([self._sock], [], [], 1.0)
            if not r:
                raise EOFError("CAN timeout / no frame")
            raw = self._sock.recv(16)
            # Unpack: can_id (u32 LE) + dlc (u8) + pad (3) + data (8)
            can_id_raw, dlc = struct.unpack_from("<IB", raw, 0)
            # Bit 31 of can_id_raw set for extended (29-bit) ID
            if not (can_id_raw & 0x8000_0000):
                continue  # skip 11-bit frames
            can_id = can_id_raw & 0x1FFF_FFFF
            data = raw[8 : 8 + (dlc & 0xF)]
            return can_id, bytes(data)

    async def send(self, can_id: int, data: bytes) -> None:  # pragma: no cover
        if self._use_python_can and self._bus is not None:
            import can  # noqa: PLC0415
            msg = can.Message(
                arbitration_id=can_id,
                data=data,
                is_extended_id=True,
            )
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._bus.send, msg)
            return
        import struct  # noqa: PLC0415
        # AF_CAN raw write: can_id (u32 BE with EFF flag) + dlc + 3 pad + data padded to 8
        can_id_raw = (can_id & 0x1FFF_FFFF) | 0x8000_0000
        payload = bytes(data)[:8].ljust(8, b"\x00")
        frame = struct.pack("<IB3x8s", can_id_raw, len(data) & 0xF, payload)
        self._sock.send(frame)


# ─────────────────────────────────────────────────────────────────────────────
# Nmea2000Connector
# ─────────────────────────────────────────────────────────────────────────────


class Nmea2000Connector(Connector):
    """Bridges a NMEA 2000 CAN bus to the Vanchor event bus.

    See module docstring for the full ingress/egress description.

    Parameters
    ----------
    transport:
        The CAN transport to use.  Defaults to a :class:`_SocketCanTransport`
        on ``"can0"``.  Inject a :class:`FakeCanTransport` in tests.
    mono_fn:
        Injectable monotonic clock (default :func:`time.monotonic`).  Used for
        the 1.0 s ingress pairing window.
    egress_interval_s:
        Minimum seconds between egress CAN sends (default 0.5 = 2 Hz max).
    reconnect_delay_s:
        Back-off delay after a read-loop error before reconnecting
        (default 1.0 s).
    """

    manifest = MANIFEST

    def __init__(
        self,
        transport: CanTransport | None = None,
        *,
        mono_fn: Callable[[], float] = time.monotonic,
        egress_interval_s: float = 0.5,
        reconnect_delay_s: float = 1.0,
    ) -> None:
        self._transport: CanTransport = (
            transport if transport is not None else _SocketCanTransport()  # pragma: no cover
        )
        self._mono = mono_fn
        self._egress_interval = egress_interval_s
        self._reconnect_delay = reconnect_delay_s

        # Runtime state
        self._stop: bool = False
        self._task: asyncio.Task | None = None
        self._egress_task: asyncio.Task | None = None
        self._ctx: ConnectorContext | None = None

        # Ingress pairing state
        self._last_pos: tuple[float, float, float] | None = None   # (lat, lon, ts)
        self._last_cogsog: tuple[float, float, float] | None = None  # (cog_rad, sog_mps, ts)

        # Egress state
        self._last_telemetry: dict | None = None

        # Debug counters / last values
        self._rx_frames: int = 0
        self._tx_frames: int = 0
        self._last_129025: n2k.Pgn129025 | None = None
        self._last_129026: n2k.Pgn129026 | None = None
        self._last_127250: n2k.Pgn127250 | None = None
        self._last_128267: n2k.Pgn128267 | None = None
        self._last_130306: n2k.Pgn130306 | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────── #

    async def start(self, ctx: ConnectorContext) -> None:
        """Start the ingress and egress loops."""
        self._ctx = ctx
        self._stop = False
        ctx.subscribe("telemetry", self._on_telemetry)
        self._task = asyncio.ensure_future(self._run_ingress())
        self._egress_task = asyncio.ensure_future(self._run_egress())

    async def stop(self) -> None:
        """Stop both loops and close the transport."""
        self._stop = True
        for task in (self._task, self._egress_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = None
        self._egress_task = None
        try:
            await self._transport.close()
        except Exception:  # noqa: BLE001
            pass

    # ── Ingress loop ──────────────────────────────────────────────────────── #

    async def _run_ingress(self) -> None:
        """Reconnect loop for the CAN ingress reader."""
        while not self._stop:
            try:
                await self._transport.open()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("nmea2000: open failed (%s); retrying in 2 s", exc)
                await asyncio.sleep(2.0)
                continue
            try:
                await self._read_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "nmea2000: read loop ended (%s); reconnecting in %.1f s",
                    exc,
                    self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)

    async def _read_loop(self) -> None:
        """Inner loop: read CAN frames and dispatch until EOF or cancellation."""
        while not self._stop:
            can_id, data = await self._transport.recv()
            self._rx_frames += 1
            _, pgn, _ = n2k.unpack_id(can_id)
            await self._dispatch(pgn, data)

    async def _dispatch(self, pgn: int, data: bytes) -> None:
        """Decode one PGN and publish any resulting bus events."""
        now = self._mono()
        ctx = self._ctx
        if ctx is None:
            return

        if pgn == 129025:
            f129025 = n2k.decode_129025(data)
            if f129025 is not None:
                self._last_129025 = f129025
                if f129025.lat is not None and f129025.lon is not None:
                    self._last_pos = (f129025.lat, f129025.lon, now)
                    await self._try_emit_fix()

        elif pgn == 129026:
            f129026 = n2k.decode_129026(data)
            if f129026 is not None:
                self._last_129026 = f129026
                if f129026.cog_rad is not None and f129026.sog_mps is not None:
                    self._last_cogsog = (f129026.cog_rad, f129026.sog_mps, now)
                    await self._try_emit_fix()

        elif pgn == 127250:
            f127250 = n2k.decode_127250(data)
            if f127250 is not None and f127250.heading_rad is not None:
                self._last_127250 = f127250
                deg = math.degrees(f127250.heading_rad) % 360.0
                sentence = nmea.encode_hdt(deg)
                try:
                    await ctx.publish("nmea.in", sentence)
                except Exception:  # noqa: BLE001
                    pass

        elif pgn == 128267:
            f128267 = n2k.decode_128267(data)
            if f128267 is not None and f128267.depth_m is not None:
                self._last_128267 = f128267
                offset_m = f128267.offset_m if f128267.offset_m is not None else 0.0
                body = f"SDDPT,{f128267.depth_m:.2f},{offset_m:.3f}"
                sentence = f"${body}*{nmea.checksum(body)}"
                try:
                    await ctx.publish("nmea.in", sentence)
                except Exception:  # noqa: BLE001
                    pass

        elif pgn == 130306:
            f130306 = n2k.decode_130306(data)
            if f130306 is not None:
                self._last_130306 = f130306
        # All other PGNs are silently ignored.

    async def _try_emit_fix(self) -> None:
        """Pair 129025 + 129026 within the 1.0 s window and publish a GpsFix."""
        if self._last_pos is None or self._last_cogsog is None:
            return
        lat, lon, ts_pos = self._last_pos
        cog_rad, sog_mps, ts_cogsog = self._last_cogsog
        if abs(ts_pos - ts_cogsog) > 1.0:
            return
        ctx = self._ctx
        if ctx is None:
            return
        fix = GpsFix(
            point=GeoPoint(lat, lon),
            sog_knots=sog_mps * _MPS_TO_KNOTS,
            cog_deg=math.degrees(cog_rad) % 360.0,
            timestamp=max(ts_pos, ts_cogsog),
            valid=True,
        )
        try:
            await ctx.publish("gps.fix_in", fix)
        except Exception:  # noqa: BLE001
            pass

    # ── Egress loop ───────────────────────────────────────────────────────── #

    def _on_telemetry(self, payload: dict) -> None:
        """Telemetry bus handler (sync): cache the latest frame for egress."""
        self._last_telemetry = payload

    async def _run_egress(self) -> None:
        """Periodic egress loop: encode and send position at most 2 Hz."""
        while not self._stop:
            await asyncio.sleep(self._egress_interval)
            if self._stop:
                break
            telem = self._last_telemetry
            if telem is None:
                continue
            pos = telem.get("position")
            if pos is None:
                continue
            lat = pos.get("lat") if isinstance(pos, dict) else None
            lon = pos.get("lon") if isinstance(pos, dict) else None
            if lat is None or lon is None:
                continue
            sog_knots = float(telem.get("sog_knots") or 0.0)
            heading_deg = float(telem.get("heading_deg") or 0.0)
            sog_mps = sog_knots / _MPS_TO_KNOTS
            cog_rad = math.radians(heading_deg)
            try:
                pos_data = n2k.encode_129025(lat, lon)
                cogsog_data = n2k.encode_129026(0xFF, 0, cog_rad, sog_mps)
                pos_can_id = n2k.pack_id(6, 129025, _OWN_SRC)
                cogsog_can_id = n2k.pack_id(6, 129026, _OWN_SRC)
                await self._transport.send(pos_can_id, pos_data)
                await self._transport.send(cogsog_can_id, cogsog_data)
                self._tx_frames += 2
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("nmea2000: egress send failed: %s", exc)

    # ── Introspection ─────────────────────────────────────────────────────── #

    def status(self) -> dict:
        return {
            "rx_frames": self._rx_frames,
            "tx_frames": self._tx_frames,
        }

    def debug(self) -> str:
        """Human-readable debug string. Never raises."""
        try:
            lines = [
                "Nmea2000Connector",
                f"  rx_frames : {self._rx_frames}",
                f"  tx_frames : {self._tx_frames}",
            ]
            if self._last_129025 is not None:
                d129025 = self._last_129025
                lines.append(
                    f"  PGN 129025: lat={d129025.lat!r}  lon={d129025.lon!r}"
                )
            if self._last_129026 is not None:
                d129026 = self._last_129026
                cog_deg = (
                    round(math.degrees(d129026.cog_rad), 2)
                    if d129026.cog_rad is not None
                    else None
                )
                lines.append(
                    f"  PGN 129026: cog_deg={cog_deg!r}  sog_mps={d129026.sog_mps!r}"
                )
            if self._last_127250 is not None:
                d127250 = self._last_127250
                hdg_deg = (
                    round(math.degrees(d127250.heading_rad), 2)
                    if d127250.heading_rad is not None
                    else None
                )
                lines.append(
                    f"  PGN 127250: heading_deg={hdg_deg!r}  ref={d127250.ref!r}"
                )
            if self._last_128267 is not None:
                d128267 = self._last_128267
                lines.append(
                    f"  PGN 128267: depth_m={d128267.depth_m!r}  offset_m={d128267.offset_m!r}"
                )
            if self._last_130306 is not None:
                d130306 = self._last_130306
                spd = (
                    round(d130306.speed_mps, 2) if d130306.speed_mps is not None else None
                )
                ang_deg = (
                    round(math.degrees(d130306.angle_rad), 2)
                    if d130306.angle_rad is not None
                    else None
                )
                lines.append(
                    f"  PGN 130306: wind_speed_mps={spd!r}  wind_angle_deg={ang_deg!r}"
                )
            return "\n".join(lines)
        except Exception:  # noqa: BLE001
            return "Nmea2000Connector: debug error"


# ─────────────────────────────────────────────────────────────────────────────
# Factory + registration
# ─────────────────────────────────────────────────────────────────────────────


def _build(settings: dict) -> Connector:
    """Factory: build an :class:`Nmea2000Connector` from persisted settings."""
    channel = str(settings.get("channel", "can0"))
    return Nmea2000Connector(_SocketCanTransport(channel))  # pragma: no cover


register_connector(
    "nmea2000",
    _build,
    label="NMEA 2000 Bridge",
)
