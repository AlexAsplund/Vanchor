"""FastAPI web UI: a smartphone-friendly map plus a telemetry/command channel.

The server is thin: it serves the static page, streams telemetry over a
WebSocket, and forwards commands to the runtime. All the interesting behaviour
lives in the controller and simulator.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    from ..app import Runtime

logger = logging.getLogger("vanchor.ui")
STATIC_DIR = Path(__file__).parent / "static"


def create_app(runtime: "Runtime", *, telemetry_hz: float = 5.0) -> FastAPI:
    clients: set[WebSocket] = set()

    async def broadcaster() -> None:
        period = 1.0 / telemetry_hz
        while True:
            snapshot = runtime.telemetry()
            runtime.recorder.record(snapshot)
            if clients:
                message = json.dumps(snapshot)
                for ws in list(clients):
                    try:
                        await ws.send_text(message)
                    except Exception:
                        clients.discard(ws)
            await asyncio.sleep(period)

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
    async def log(n: int = 50) -> dict:
        return {"telemetry": runtime.recorder.recent(n)}

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
    async def depth_grid(cell_m: float = 15.0) -> dict:
        """Server-side gridded depth map for the depth overlay.

        Bins all soundings into ~``cell_m`` metre cells (clamped 2..200),
        averaging depth per cell so the UI can paint an averaged colour chart
        instead of thousands of individual dots. The depth map changes slowly, so
        the UI polls this occasionally rather than reading the 5 Hz telemetry.
        Returns ``{ok, cell_m, min_depth, max_depth, count, cells}``.
        """
        return runtime.depth_grid(cell_m)

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
        path = runtime.debug.path_for(file)
        if path is None:
            return {"error": "not found"}
        return FileResponse(path, media_type="application/gzip", filename=file)

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
                    runtime.handle_command(json.loads(raw))
                except Exception:
                    logger.exception("bad command over websocket: %s", raw)
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(websocket)
            runtime.client_disconnected()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
