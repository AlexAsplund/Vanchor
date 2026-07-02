"""FastAPI web UI: a smartphone-friendly map plus a telemetry/command channel.

The server is thin: it serves the static page, streams telemetry over a
WebSocket, and forwards commands to the runtime. All the interesting behaviour
lives in the controller and simulator.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request as _Request

if TYPE_CHECKING:
    from ..app import Runtime

logger = logging.getLogger("vanchor.ui")
STATIC_DIR = Path(__file__).parent / "static"

# Keys that are large array payloads; stripped from /api/log frames by default
# to keep the response lightweight (depth_points is ~28 KB per frame).
_BULK_KEYS: frozenset[str] = frozenset({"depth_points"})

# Default limits for viewport-windowed vector overlays (must match depth.py defaults).
_CONTOURS_DEFAULT_LIMIT: int = 5000
_COMPOSITION_DEFAULT_LIMIT: int = 4000

# A big /api/depth/{contours,composition} response materialises up to ``limit``
# feature dicts (each a nested lat/lon list) from the columnar store; once the
# response is serialised those transient dicts are freed by Python but glibc may
# hold the arena rather than returning it to the OS. Above this many features we
# ask glibc to release the freed arena so RSS drops back on a 512 MB device.
_TRIM_FEATURE_THRESHOLD: int = 500


def _malloc_trim_if_glibc() -> None:
    """Best-effort return of freed heap arenas to the OS (glibc only).

    Guarded for non-glibc platforms (musl / macOS): any failure to load libc or
    call ``malloc_trim`` is swallowed. Call at most once per LARGE response."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # noqa: BLE001 - no libc / no malloc_trim -> no-op
        pass


def shape_frame(snapshot: dict, full: bool) -> dict:
    """Shape a telemetry snapshot for WebSocket broadcast.

    Full frames (every 5th) are returned as-is.  Non-full frames strip the
    bulky array payloads that change infrequently:

    * ``depth_points`` -- omitted entirely (client retains the last copy).
    * ``waypoints`` -- omitted entirely (absent, not null/empty; a concurrent
      client guard mirrors this contract: waypoints is only applied when the
      key is present in the frame).
    * ``track`` -- present but with only its scalar keys (``recording``,
      ``count``, etc.); the ``points`` array is dropped so the UI updates the
      breadcrumb count readout every frame but redraws the trail only on full
      frames.
    """
    if full:
        return snapshot
    out: dict = {}
    for k, v in snapshot.items():
        if k in ("depth_points", "waypoints"):
            continue
        if k == "track" and isinstance(v, dict):
            out[k] = {sk: sv for sk, sv in v.items() if sk != "points"}
        else:
            out[k] = v
    return out


def _extract_hostname(host: str) -> str:
    """Return just the hostname from a ``Host`` header value (strips port, brackets)."""
    host = host.lower().strip()
    if host.startswith("["):
        # IPv6 bracketed notation: [::1] or [::1]:8080
        end = host.find("]")
        return host[1:end] if end > 0 else ""
    # IPv4 or name, possibly with port
    return host.rsplit(":", 1)[0] if ":" in host else host


# Hostname suffixes that only exist on a private LAN and can never be a public
# domain an attacker controls -- so accepting them keeps DNS-rebinding
# protection intact (the attack needs a *public* name resolving to the LAN IP).
# Covers the common router/mDNS conventions: .local (mDNS/Bonjour), and the
# private zones routers hand out (.lan, .home, .internal, .localdomain).
_PRIVATE_HOST_SUFFIXES: tuple[str, ...] = (
    ".local", ".lan", ".home", ".internal", ".localdomain",
)


def _is_allowed_host(hostname: str, extra: frozenset[str]) -> bool:
    """Return True when ``hostname`` is an acceptable value for the Host header.

    Allowed classes:
    * Any IP literal (v4 or v6) -- direct-IP access from the boat LAN.
    * ``localhost`` -- loopback development / SSH-tunnel access.
    * A bare single-label hostname (no dot, e.g. ``vanchor``, ``spark-11a6``) --
      cannot be a public domain, so it's a LAN machine name.
    * Any name under a private-LAN suffix (``.local`` mDNS, ``.lan``, ``.home``,
      ``.internal``, ``.localdomain``) -- the names routers assign on a LAN.
    * Any name listed in the ``extra`` set (populated from ``VANCHOR_ALLOWED_HOSTS``).

    Everything else (a public FQDN like ``evil.com`` pointed at the LAN IP for a
    DNS-rebinding attack) is rejected.
    """
    if not hostname:
        return False
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    if hostname == "localhost":
        return True
    if "." not in hostname:  # bare LAN machine name, cannot be a public domain
        return True
    if hostname.endswith(_PRIVATE_HOST_SUFFIXES):
        return True
    return hostname in extra


def create_app(runtime: "Runtime", *, telemetry_hz: float = 5.0) -> FastAPI:
    clients: set[WebSocket] = set()

    # Hosts accepted in the Host header beyond the built-in rules (IP literals,
    # localhost, *.local).  Read at app-creation time so tests can override via
    # monkeypatch.setenv before calling create_app.
    _extra_allowed: frozenset[str] = frozenset(
        h.strip().lower()
        for h in os.environ.get("VANCHOR_ALLOWED_HOSTS", "").split(",")
        if h.strip()
    )

    async def broadcaster() -> None:
        period = 1.0 / telemetry_hz
        frame_n = 0
        while True:
            try:
                snapshot = runtime.telemetry()
                # The ring keeps ~600 frames for /api/log history; storing the
                # bulky ``depth_points`` array (~28 KB each) in every frame would
                # pin ~17 MB of stale soundings. Strip it before storing -- the
                # live layer keeps the authoritative copy and the WS full-frame
                # path below still ships depth_points off the fresh snapshot.
                runtime.recorder.record(
                    {k: v for k, v in snapshot.items() if k not in _BULK_KEYS}
                )
                # telemetry() is a pure snapshot now, so the broadcaster (the ~5 Hz
                # heartbeat) drives depth-sounding accumulation -- keeping the
                # original per-frame cadence -- and records the frame into the debug
                # session. The debug write does gzip compression, so it runs off the
                # event loop (write() is lock-guarded / thread-safe).
                runtime.record_depth_sounding()
                if runtime.debug.active:
                    await asyncio.to_thread(
                        runtime.debug.write, "telemetry", snapshot, time.time()
                    )
                if clients:
                    frame_n += 1
                    # Full frames (every 5th, ~1 Hz) carry depth_points, waypoints and
                    # track.points.  Non-full frames strip those bulky arrays so the
                    # high-rate 5 Hz WS stream stays lean.  /api/state always returns
                    # the complete snapshot; shape_frame only applies to the broadcaster.
                    out = shape_frame(snapshot, full=(frame_n % 5 == 1))
                    message = json.dumps(out)

                    # Send to all clients concurrently so one stalled client
                    # doesn't delay telemetry for others.  Per-client timeout
                    # evicts connections that won't drain within 2 s.
                    async def _send(ws: WebSocket, msg: str = message) -> None:
                        try:
                            await asyncio.wait_for(ws.send_text(msg), timeout=2.0)
                        except Exception:
                            clients.discard(ws)

                    await asyncio.gather(
                        *(_send(ws) for ws in list(clients)),
                        return_exceptions=True,
                    )
                await asyncio.sleep(period)
            except asyncio.CancelledError:
                raise  # allow the lifespan shutdown to propagate
            except Exception:
                logger.exception("broadcaster loop error — will retry")
                await asyncio.sleep(1.0)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.start()
        task = asyncio.ensure_future(broadcaster())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await runtime.stop()

    app = FastAPI(title="Vanchor-NG", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # DNS-rebinding protection: reject requests whose Host header hostname is
    # not an IP literal, localhost, a .local mDNS name, or an entry in
    # VANCHOR_ALLOWED_HOSTS.  Added after GZipMiddleware so it is outermost
    # (runs first) and bad requests are rejected before decompression.
    # Note: BaseHTTPMiddleware does NOT run for WebSocket upgrades; see the
    # /ws handler below for the equivalent WS-level check.
    class _HostCheckMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: _Request, call_next):
            host = request.headers.get("host", "")
            if not _is_allowed_host(_extract_hostname(host), _extra_allowed):
                return Response(
                    content="Host not allowed",
                    status_code=400,
                    media_type="text/plain",
                )
            return await call_next(request)

    app.add_middleware(_HostCheckMiddleware)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/sw.js")
    async def service_worker() -> FileResponse:
        """Serve the service worker at root scope so it controls the whole
        origin (not just /static). The ``Service-Worker-Allowed`` header lets it
        claim a scope above its own URL (#82)."""
        return FileResponse(
            STATIC_DIR / "sw.js",
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    @app.get("/manifest.webmanifest")
    async def manifest() -> FileResponse:
        """Serve the web app manifest at root scope so install/start_url work."""
        return FileResponse(
            STATIC_DIR / "manifest.webmanifest",
            media_type="application/manifest+json",
        )

    @app.get("/api/state")
    async def state() -> dict:
        return runtime.telemetry()

    @app.get("/api/log")
    async def log(n: int = 50, full: int = 0) -> dict:
        """Recent telemetry frames from the in-memory ring.

        By default, bulky array fields (``depth_points``, ~28 KB each) are
        stripped so n=50 doesn't balloon to 1.4 MB.  Pass ``?full=1`` to get
        every field untrimmed (e.g. for diagnostics / replay tooling).
        """
        frames = runtime.recorder.recent(n)
        if not full:
            frames = [
                {k: v for k, v in f.items() if k not in _BULK_KEYS}
                for f in frames
            ]
        return {"telemetry": frames}

    @app.get("/api/logs")
    async def app_logs(level: str = "INFO", n: int = 300,
                       contains: str | None = None) -> dict:
        """Recent in-memory application log records for the 'View logs' UI, at or
        above ``level`` (DEBUG/INFO/WARNING/ERROR), newest last, optionally
        text-filtered by ``contains``."""
        import logging as _logging

        from ..core.observability import log_ring

        minno = _logging.getLevelName(level.upper())
        if not isinstance(minno, int):
            minno = _logging.INFO
        n = max(1, min(int(n), 1000))
        return {"ok": True, "level": level.upper(),
                "records": log_ring().dump(minno, n, contains)}

    @app.post("/api/device/setting")
    async def device_setting(payload: dict) -> dict:
        """Apply a device-menu setting (from a driver's device_menu) to the
        active device. Body: ``{device, key, value}``."""
        return runtime.apply_device_setting(
            str(payload.get("device", "")), str(payload.get("key", "")),
            payload.get("value"),
        )

    @app.post("/api/device/action")
    async def device_action(payload: dict) -> dict:
        """Run a device-menu action (e.g. sensor profile / calibrate) on the
        active device. Body: ``{device, action, params?}``. Runs in an executor
        since an action may talk to the hardware."""
        device = str(payload.get("device", ""))
        action = str(payload.get("action", ""))
        params = payload.get("params") or {}
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: runtime.run_device_action(device, action, params)
        )

    @app.get("/api/tune/jobs")
    async def tune_jobs() -> dict:
        from ..analysis.tuning import TUNING_JOBS

        return {"jobs": [{"name": n, "description": b().description} for n, b in TUNING_JOBS.items()]}

    @app.post("/api/tune")
    async def run_tune(payload: dict) -> dict:
        """Run an auto-tuning job (off the event loop) and optionally apply it.

        Body: ``{"job": "heading", "max_evals": 50, "apply": false}``.
        """
        import dataclasses

        from ..analysis.tuning import tune

        job = str(payload.get("job", "heading"))
        max_evals = int(payload.get("max_evals", 50))
        apply = bool(payload.get("apply", False))
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, lambda: tune(job, max_evals=max_evals)
            )
        except ValueError as exc:
            return {"error": str(exc)}
        out = dataclasses.asdict(result)
        if apply:
            runtime.apply_tuned_gains(job, result.tuned_params)
            out["applied"] = True
        return out

    @app.post("/api/command")
    async def command(payload: dict) -> dict:
        runtime.handle_command(payload)
        return {"ok": True}

    @app.post("/api/restart")
    async def restart() -> dict:
        """Restart the server process in place (applies device/config changes).

        Re-execs with the same argv after flushing the response. The listening
        socket is non-inheritable (closed on exec), so the fresh process rebinds
        the port. Works whether launched bare, under nohup, or via a supervisor.
        """
        import os
        import sys

        async def _reexec() -> None:
            await asyncio.sleep(0.4)  # let the HTTP response flush first
            with contextlib.suppress(Exception):
                await runtime.stop()
            logger.warning("restart requested -- re-execing %s", sys.argv)
            os.execv(sys.executable, [sys.executable, *sys.argv])

        asyncio.ensure_future(_reexec())
        return {"ok": True, "restarting": True}

    @app.post("/api/route/plan")
    async def route_plan(payload: dict) -> dict:
        """Plan a water-only route to a destination and return waypoints.

        Body: ``{dest_lat, dest_lon, mode, shoreline_offset_m}``. The boat's
        current position (or the sim start) is the start. This does NOT start
        navigation -- it only returns waypoints for the UI's route editor to
        load unstarted for review. The heavy shapely/networkx work runs in an
        executor so the telemetry loop isn't blocked.
        """
        try:
            dest_lat = float(payload["dest_lat"])
            dest_lon = float(payload["dest_lon"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "waypoints": [], "message": "dest_lat and dest_lon are required."}
        mode = str(payload.get("mode", "fastest"))
        try:
            offset_m = float(payload.get("shoreline_offset_m", 25.0))
        except (TypeError, ValueError):
            offset_m = 25.0
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: runtime.plan_route(dest_lat, dest_lon, mode, offset_m)
        )
        return result

    @app.post("/api/route/island")
    async def route_island(payload: dict) -> dict:
        """Plan a closed loop route around the island under a clicked point (#77).

        Body: ``{lat, lon, offset_m?}``. The boat's current position (or the sim
        start) decides which water body is the basin; the click must fall inside
        an island (a land hole) of that basin. Returns
        ``{ok, waypoints, loop, message}`` -- it does NOT start navigation; the
        UI loads the waypoints into its route editor (with the loop flag). The
        shapely work runs in an executor so the telemetry loop isn't blocked.
        """
        try:
            lat = float(payload["lat"])
            lon = float(payload["lon"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "waypoints": [], "loop": True, "message": "lat and lon are required."}
        try:
            offset_m = float(payload.get("offset_m", 20.0))
        except (TypeError, ValueError):
            offset_m = 20.0
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: runtime.plan_island_loop(lat, lon, offset_m)
        )

    @app.post("/api/route/plan/cancel")
    async def route_plan_cancel() -> dict:
        """Abort an in-progress route plan (#54)."""
        runtime.cancel_route_plan()
        return {"cancelled": True}

    @app.post("/api/route/rtl")
    async def route_rtl() -> dict:
        """Return-to-Launch (#61): plan a water route home and follow it.

        The heavy water-fetch + routing runs in an executor so the telemetry loop
        isn't blocked. Returns ``{ok, waypoints, message}``.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, runtime.return_to_launch)

    @app.post("/api/route/survey")
    async def route_survey(payload: dict) -> dict:
        """Plan a boustrophedon area-survey ("map mode") coverage route (#47).

        Body: ``{polygon: [[lat,lon],...], spacing_m: <f>, angle_deg: <f|null>}``.
        Returns ``{ok, waypoints, message}``. Does NOT start navigation -- the UI
        loads the waypoints into its route editor. The shapely work runs in an
        executor so the telemetry loop isn't blocked.
        """
        polygon = payload.get("polygon")
        if not isinstance(polygon, list) or len(polygon) < 3:
            return {
                "ok": False,
                "waypoints": [],
                "message": "polygon must be a list of at least 3 [lat,lon] points.",
            }
        try:
            spacing_m = float(payload.get("spacing_m"))
        except (TypeError, ValueError):
            return {"ok": False, "waypoints": [], "message": "spacing_m is required."}
        angle_raw = payload.get("angle_deg")
        try:
            angle_deg = None if angle_raw is None else float(angle_raw)
        except (TypeError, ValueError):
            angle_deg = None
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: runtime.plan_survey(polygon, spacing_m, angle_deg)
        )

    @app.post("/api/route/work_area")
    async def route_work_area(payload: dict) -> dict:
        """Generate Work Area spots: an even serpentine grid over a drawn area,
        clipped to water. Body: ``{polygon: [[lat,lon],...], spacing_m: <f>}``.
        Returns ``{ok, waypoints, message}`` -- the UI loads these as the spots
        (then starts Work Area mode with a `work_area` command). Runs in an
        executor (shapely)."""
        polygon = payload.get("polygon")
        if not isinstance(polygon, list) or len(polygon) < 3:
            return {"ok": False, "waypoints": [],
                    "message": "polygon must be a list of at least 3 [lat,lon] points."}
        try:
            spacing_m = float(payload.get("spacing_m"))
        except (TypeError, ValueError):
            return {"ok": False, "waypoints": [], "message": "spacing_m is required."}
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: runtime.plan_work_spots(polygon, spacing_m)
        )

    @app.post("/api/route/contour")
    async def route_contour(payload: dict) -> dict:
        """Build a route that follows the imported depth contour nearest a clicked
        point (chaining same-depth pieces into a continuous track). Body:
        ``{lat, lon}``. Returns ``{ok, waypoints, depth_m, loop, message}`` -- the
        UI loads the waypoints as a route (patrol optional). Runs in an executor."""
        try:
            lat = float(payload["lat"])
            lon = float(payload["lon"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "waypoints": [], "message": "lat and lon are required."}
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: runtime.contour_route(lat, lon))

    @app.post("/api/route/prefetch")
    async def route_prefetch(payload: dict) -> dict:
        """Pre-download + cache the water/routing chart for an area (#52).

        Body: ``{bbox: [south, west, north, east]}``. Returns
        ``{ok, cached, vertices, message}``. The Overpass fetch runs in an
        executor and network failure is handled gracefully.
        """
        bbox = payload.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            return {
                "ok": False,
                "cached": False,
                "vertices": 0,
                "message": "bbox must be [south, west, north, east].",
            }
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: runtime.prefetch_chart(bbox))

    @app.get("/api/route/charts")
    async def route_charts() -> dict:
        """List cached charts (bbox + size) for the offline-chart manager (#52)."""
        return runtime.list_charts()

    @app.post("/api/route/charts/clear")
    async def route_charts_clear() -> dict:
        """Clear all cached charts (#52)."""
        return runtime.clear_charts()

    @app.get("/api/depth/grid")
    async def depth_grid(
        cell_m: float = 15.0,
        west: float | None = None,
        south: float | None = None,
        east: float | None = None,
        north: float | None = None,
        field: str = "depth",
    ) -> dict:
        """Server-side gridded chart for the depth / bottom-hardness overlay.

        Bins soundings into ~``cell_m`` metre cells (clamped 2..200), averaging
        the value per cell so the UI can paint an averaged colour chart instead
        of thousands of individual dots. When ``west``/``south``/``east``/
        ``north`` are given, only that viewport window is gridded (Tier-1
        windowing) so a large chart ships just what's on screen. ``field`` is
        ``depth`` (default) or ``hardness`` (bottom-hardness, 0..127).
        Returns ``{ok, field, cell_m, min_depth, max_depth, count, cells}``.
        """
        bbox = None
        if None not in (west, south, east, north):
            bbox = (west, south, east, north)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: runtime.depth_grid(cell_m, bbox=bbox, field=field)
        )

    @app.get("/api/depth/contours")
    async def depth_contours(
        west: float | None = None,
        south: float | None = None,
        east: float | None = None,
        north: float | None = None,
        limit: int | None = None,
    ) -> dict:
        """Imported depth-contour polylines (isobaths) for the contour
        overlay, windowed to the viewport. With ``west``/``south``/``east``/
        ``north`` only contours intersecting that window are returned (a large
        chart has 80k+ lines). ``limit`` caps the returned count (clamped to
        [100, 8000]; defaults to 5000). Returns ``{ok, count, truncated,
        contours}`` where each is ``{d: depth_m, pts: [[lat, lon], ...]}``; a
        ``truncated: true`` flag means the chart has more results -- zoom in
        for full detail.
        """
        bbox = None
        if None not in (west, south, east, north):
            bbox = (west, south, east, north)
        clamp_limit = max(100, min(8000, limit if limit is not None else _CONTOURS_DEFAULT_LIMIT))
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: runtime.depth_contours(bbox=bbox, limit=clamp_limit)
        )
        result["truncated"] = result["count"] == clamp_limit
        if result["count"] >= _TRIM_FEATURE_THRESHOLD:
            _malloc_trim_if_glibc()
        return result

    @app.get("/api/depth/composition")
    async def depth_composition(
        west: float | None = None,
        south: float | None = None,
        east: float | None = None,
        north: float | None = None,
        limit: int | None = None,
    ) -> dict:
        """Imported bottom-composition POLYGONS, windowed to the viewport.
        ``limit`` caps the returned count (clamped to [100, 8000]; defaults to
        4000). Returns ``{ok, count, truncated, polygons}`` where each polygon
        is ``{pct: 0..100, ring: [[lat, lon], ...]}`` -- rendered filled,
        YlOrBr. ``truncated: true`` means more polygons exist outside the cap;
        zoom in for full detail.
        """
        bbox = None
        if None not in (west, south, east, north):
            bbox = (west, south, east, north)
        clamp_limit = max(100, min(8000, limit if limit is not None else _COMPOSITION_DEFAULT_LIMIT))
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: runtime.depth_composition(bbox=bbox, limit=clamp_limit)
        )
        result["truncated"] = result["count"] == clamp_limit
        if result["count"] >= _TRIM_FEATURE_THRESHOLD:
            _malloc_trim_if_glibc()
        return result

    @app.get("/api/depth/water")
    async def depth_water(
        west: float, south: float, east: float, north: float,
    ) -> dict:
        """OSM water polygon(s) for the bbox, to CLIP overlays to water (don't
        draw composition over land). Cached; fetched from Overpass if absent.
        Returns ``{ok, water}`` (GeoJSON MultiPolygon coords, lon/lat)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: runtime.water_polygon((west, south, east, north))
        )

    @app.post("/api/depth/import")
    async def depth_import(file: UploadFile = File(...), replace: bool = False) -> dict:
        """Import an open-format depth file (CSV/XYZ or GeoJSON) into the depth
        chart. ``replace=true`` swaps the whole chart; the default merges."""
        data = await file.read()
        filename = file.filename or ""
        replace_flag = bool(replace)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: runtime.import_depth_map(filename, data, replace=replace_flag)
        )

    @app.get("/api/weather/presets")
    async def weather_presets() -> dict:
        from ..sim.weather import WEATHER_PRESETS

        return {"presets": [p.to_dict() for p in WEATHER_PRESETS.values()]}

    @app.get("/api/boat")
    async def get_boat() -> dict:
        return runtime.boat_profile()

    @app.post("/api/boat")
    async def set_boat(payload: dict) -> dict:
        return runtime.update_boat(payload)

    # -- Named boat profiles (#75) --------------------------------------- #
    @app.get("/api/boat/profiles")
    async def boat_profiles() -> dict:
        """List profiles: ``{active_id, profiles:[{id,name,...specs}, ...]}``."""
        return runtime.boat_profiles_list()

    @app.post("/api/boat/profiles")
    async def boat_profiles_create(payload: dict) -> dict:
        """Create a profile. Body ``{name, specs?}``; specs default to the
        current active boat. Returns ``{id, name, specs}``."""
        name = str(payload.get("name", "")).strip() or "Boat"
        specs = payload.get("specs")
        return runtime.boat_profiles_create(name, specs if isinstance(specs, dict) else None)

    @app.post("/api/boat/profiles/{profile_id}")
    async def boat_profiles_update(profile_id: str, payload: dict):
        """Update a profile. Body ``{name?, specs?}``. 404 if unknown."""
        name = payload.get("name")
        specs = payload.get("specs")
        result = runtime.boat_profiles_update(
            profile_id,
            None if name is None else str(name),
            specs if isinstance(specs, dict) else None,
        )
        if result is None:
            return Response(status_code=404)
        return result

    @app.post("/api/boat/profiles/{profile_id}/activate")
    async def boat_profiles_activate(profile_id: str):
        """Activate a profile + apply it live. Returns the applied boat
        profile. 404 if unknown."""
        result = runtime.boat_profiles_activate(profile_id)
        if result is None:
            return Response(status_code=404)
        return result

    @app.delete("/api/boat/profiles/{profile_id}")
    async def boat_profiles_delete(profile_id: str) -> dict:
        """Delete a profile. Refuses to delete the last remaining one."""
        return {"ok": runtime.boat_profiles_delete(profile_id)}

    # -- Device / hardware config (persisted, editable) ------------------ #
    @app.get("/api/config/devices")
    async def get_device_config() -> dict:
        """Current device/hardware config + selectable options.

        Returns ``{hardware:{...}, nmea_tcp:{...}, options:{sensor:[...],
        motor:[...]}, restart_required:false}``."""
        return runtime.device_config()

    @app.post("/api/config/devices")
    async def set_device_config(payload: dict):
        """Validate + persist a device-config edit to ``devices.json`` and update
        the in-memory config. Body ``{hardware:{...}, nmea_tcp:{...}}``. Returns
        ``{ok:true, restart_required:true}``; 400 on a bad source/type. Devices
        apply on the next restart — a live hot-swap (``Runtime.reload_devices``)
        exists but isn't auto-invoked yet because recreating sensor I/O tasks
        in-place isn't reliable (it can trip the fix-loss failsafe)."""
        try:
            return runtime.set_device_config(payload)
        except ValueError as exc:
            return Response(
                content=json.dumps({"ok": False, "error": str(exc)}),
                media_type="application/json",
                status_code=400,
            )

    # -- Versioned backup / restore -------------------------------------- #
    @app.post("/api/backup")
    async def backup_create(payload: dict | None = None):
        """Build + download a versioned backup ZIP of all persistent state.

        Body (optional): ``{"client": {...}}`` -- the UI's ``localStorage`` slice
        (keys prefixed ``vanchor-``) to embed as ``client.json``. Returns the zip
        with ``Content-Disposition: attachment`` so the browser saves it. The
        manifest records ``format/schema_version/app_version/created_at/contents``."""
        client = (payload or {}).get("client") if isinstance(payload, dict) else None
        if not isinstance(client, dict):
            client = None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        data = runtime.create_backup(client=client, created_at=created_at)
        return Response(
            content=data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="vanchor-backup-{stamp}.zip"'
            },
        )

    @app.post("/api/restore")
    async def backup_restore(file: UploadFile = File(...)):
        """Restore a previously-downloaded backup ZIP (multipart upload).

        Reads the uploaded ``file`` bytes, restores them into the data dir, and
        live-reloads what it can. Returns the restore result
        (``{ok, schema_version, app_version, created_at, restored, client,
        warnings, restart_required}``). 400 on a corrupt / non-vanchor zip."""
        data = await file.read()
        try:
            return runtime.restore_backup(data)
        except ValueError as exc:
            return Response(
                content=json.dumps({"ok": False, "error": str(exc)}),
                media_type="application/json",
                status_code=400,
            )

    @app.post("/api/calibrate")
    async def calibrate(payload: dict) -> dict:
        mode = str(payload.get("mode", "quick"))
        started = runtime.calibration.start(mode)
        return {"started": started}

    @app.post("/api/calibrate/cancel")
    async def calibrate_cancel() -> dict:
        runtime.calibration.cancel()
        return {"cancelled": True}

    # -- debug session recording + replay -------------------------------- #
    @app.post("/api/debug/start")
    async def debug_start(payload: dict) -> dict:
        return runtime.start_debug(payload.get("name"))

    @app.post("/api/debug/stop")
    async def debug_stop() -> dict:
        return runtime.stop_debug()

    @app.get("/api/debug/sessions")
    async def debug_sessions() -> dict:
        return {"sessions": runtime.debug.sessions(), "status": runtime.debug.status()}

    @app.get("/api/debug/download")
    async def debug_download(file: str):
        import os as _os

        path = runtime.debug.path_for(file)
        if path is None:
            return {"error": "not found"}
        if _os.path.isfile(path):
            return FileResponse(path, media_type="application/gzip", filename=file)
        # Chunked session: stream the parts concatenated (gzip members concatenate
        # into one valid .gz), so a multi-part session downloads as a single file.
        parts = [_os.path.join(path, p) for p in sorted(_os.listdir(path))
                 if p.endswith(".ndjson.gz")]

        def _stream():
            for part in parts:
                with open(part, "rb") as fh:
                    while True:
                        block = fh.read(65536)
                        if not block:
                            break
                        yield block

        return StreamingResponse(
            _stream(), media_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{file}.ndjson.gz"'},
        )

    @app.post("/api/debug/replay")
    async def debug_replay(payload: dict) -> dict:
        ok = runtime.start_replay(str(payload.get("file", "")))
        return {"replaying": ok}

    @app.post("/api/debug/replay/stop")
    async def debug_replay_stop() -> dict:
        runtime.stop_replay()
        return {"replaying": False}

    # --- Trip log (#66) ------------------------------------------------- #
    @app.get("/api/trips")
    async def trips_list() -> dict:
        return {"trips": runtime.trip_list()}

    @app.get("/api/trips/{trip_id}.gpx")
    async def trip_gpx(trip_id: str):
        gpx = runtime.trip_gpx(trip_id)
        if gpx is None:
            return Response(status_code=404)
        return Response(
            content=gpx,
            media_type="application/gpx+xml",
            headers={"Content-Disposition": f'attachment; filename="{trip_id}.gpx"'},
        )

    @app.get("/api/trips/{trip_id}")
    async def trip_get(trip_id: str):
        trip = runtime.trip_get(trip_id)
        if trip is None:
            return Response(status_code=404)
        return trip

    @app.delete("/api/trips/{trip_id}")
    async def trip_delete(trip_id: str) -> dict:
        return {"ok": runtime.trip_delete(trip_id)}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        # DNS-rebinding check for WebSocket: BaseHTTPMiddleware doesn't run for
        # WS upgrades, so we replicate the host validation here.  Reject after
        # accept (pre-accept close is unreliable across ASGI servers).
        host = websocket.headers.get("host", "")
        if not _is_allowed_host(_extract_hostname(host), _extra_allowed):
            await websocket.close(code=1008)  # 1008 = Policy Violation
            return
        clients.add(websocket)
        # Mark the link alive for the lost-connection failsafe (#64).
        runtime.client_connected()
        try:
            # Send an immediate snapshot so the UI paints without waiting.
            await websocket.send_text(json.dumps(runtime.telemetry()))
            while True:
                raw = await websocket.receive_text()
                runtime.client_activity()
                try:
                    msg = json.loads(raw)
                except Exception:
                    logger.exception("bad command over websocket: %s", raw)
                    continue
                if msg.get("type") == "ping":
                    # Application-level heartbeat: liveness already updated above.
                    # Do NOT forward to the controller (it would log "unknown command").
                    await websocket.send_text('{"type":"pong"}')
                    continue
                try:
                    runtime.handle_command(msg)
                except Exception:
                    logger.exception("bad command over websocket: %s", raw)
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(websocket)
            runtime.client_disconnected()

    class _NoCacheStatic(StaticFiles):
        """Static assets with ``Cache-Control: no-cache`` so browsers + the
        service worker always revalidate (cheap ETag 304s) and never keep serving
        a heuristically-cached stale shell after an update."""

        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            response.headers["Cache-Control"] = "no-cache"
            return response

    app.mount("/static", _NoCacheStatic(directory=STATIC_DIR), name="static")
    return app
