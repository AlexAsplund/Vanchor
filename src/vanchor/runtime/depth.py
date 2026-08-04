"""Depth cluster extracted from Runtime (issue #71).

All 12 methods that handle depth soundings, depth-divergence, depth-map
persistence, contour routing, chart management, depth grid/query, and depth
import live here. ``DepthService`` holds a back-reference to ``Runtime`` via
``self._rt`` for shared state that remains on Runtime (config, state, bus,
depth_map, depth_sounder, simulator, replay, _depth_map_path,
_depth_chart_path, _depth_saved_n, _depth_save_in_flight, etc.).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.models import GeoPoint

logger = logging.getLogger("vanchor.app")


class DepthService:
    """Depth soundings, chart management, contour routing -- split out of Runtime."""

    def __init__(self, rt) -> None:
        self._rt = rt   # back-reference to Runtime for shared state

    # ------------------------------------------------------------------ #
    # Live sounding + divergence
    # ------------------------------------------------------------------ #

    def record_depth_sounding(self) -> None:
        """Accumulate one depth sounding at the boat's DRAWN position.

        Called by the WS broadcaster at the telemetry rate (~5 Hz) so soundings
        keep their original cadence now that ``telemetry()`` is a pure snapshot.
        A no-op during replay (replayed depth must not pollute the live map).

        Record each sounding at the SAME position the boat marker is drawn at, so
        the depth dots sit under the boat. In the sim the marker uses ground truth
        -- and the sounder samples the bottom at that true position too -- whereas
        the GPS fix carries noise that would offset the dots beside the boat. On
        real hardware there is no truth, so both use the GPS fix."""
        rt = self._rt
        if rt.replay.active:
            return
        sounding_pos = (
            rt.simulator.truth().point
            if rt.simulator is not None
            else rt.state.position
        )
        # Sonar-vs-chart grounding-divergence alert (#45): compare the live
        # sounder depth against the charted depth at the boat BEFORE this sounding
        # is folded into the map (so the just-taken sample can't self-cancel the
        # comparison), then set the divergence state fields for telemetry/UI.
        self._update_depth_divergence(sounding_pos)
        rt.depth_map.record(sounding_pos, rt.state.depth_m)

    def _update_depth_divergence(self, position: "GeoPoint | None" = None) -> None:
        """Wire nav/sonar.py into the running app (#45): look up the charted depth
        from the ``DepthMap`` at the boat's position, compare it against the
        measured sounder depth (``state.depth_m``), and set the sonar/divergence
        state fields (``sonar_depth_m`` / ``charted_depth_m`` /
        ``depth_divergence_m`` / ``depth_divergence_alert``) so the shallow-side
        grounding alert can fire in telemetry.

        A clean no-op when there is no chart, no fix, or no live depth: a
        non-positive depth (lost bottom lock) or a null/absent position leaves the
        previous alert untouched rather than false-tripping."""
        from ..nav import sonar

        rt = self._rt
        state = rt.state
        pos = position if position is not None else state.position
        depth = state.depth_m
        if pos is None or pos.is_null() or depth is None or float(depth) <= 0.0:
            return
        sonar.ingest(
            state, sonar.Sounding(depth_m=float(depth)), rt.depth_map, position=pos
        )

    async def _maybe_persist_depth(self) -> None:
        """Checkpoint newly-accumulated soundings to disk OFF the event loop
        (finding M3), at most one save in flight at a time.

        ``depth_map.save`` does an atomic JSON write; on a large map that is a
        real blocking cost, so it runs in a worker thread. The in-flight guard
        stops the 1 Hz supervisor from stacking overlapping saves."""
        rt = self._rt
        if rt._depth_save_in_flight:
            return
        n = len(rt.depth_map.points)
        if n - rt._depth_saved_n < 25:
            return
        rt._depth_save_in_flight = True
        try:
            await asyncio.to_thread(rt.depth_map.save, rt._depth_map_path)
            rt._depth_saved_n = n
        except Exception:  # noqa: BLE001 - a failed checkpoint must not wedge saves
            logger.exception("depth map checkpoint failed")
        finally:
            rt._depth_save_in_flight = False

    # ------------------------------------------------------------------ #
    # Contour-following route builder (#45)
    # ------------------------------------------------------------------ #

    def contour_route(self, lat: float, lon: float, window_m: float = 700.0) -> dict:
        """Build a route that follows the imported depth contour nearest
        (lat, lon), chaining same-depth pieces into a continuous track (a closed
        isobath comes back as a loop). Pure CPU (shapely); the UI endpoint calls it
        in an executor. Returns ``{ok, waypoints, depth_m, loop, message}`` -- the
        UI loads the waypoints as a route (patrol optional)."""
        from ..nav import contour_route as cr

        rt = self._rt
        dlat = window_m / 111_320.0
        dlon = window_m / (111_320.0 * max(0.1, math.cos(math.radians(lat))))
        bbox = (lon - dlon, lat - dlat, lon + dlon, lat + dlat)  # (w, s, e, n)
        contours = rt.depth_map.contours_in(bbox=bbox)
        if not contours:
            return {"ok": False, "waypoints": [],
                    "message": "No depth contours loaded around there."}
        return cr.contour_route_near(lat, lon, contours)

    # ------------------------------------------------------------------ #
    # Offline chart prefetch + management (#52)
    # ------------------------------------------------------------------ #

    def prefetch_chart(self, bbox: list) -> dict:
        """Fetch + cache the water polygon for a bbox so the boat can route
        offline later. ``bbox`` is ``[south, west, north, east]``.

        Synchronous and IO-heavy (Overpass fetch); call it in an executor.
        Handles network failure gracefully.
        """
        from ..nav import water

        rt = self._rt
        try:
            south, west, north, east = (float(v) for v in bbox)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "cached": False,
                "vertices": 0,
                "message": "bbox must be [south, west, north, east].",
            }
        box = (south, west, north, east)
        cache = water.WaterCache(rt.config.data_dir)
        existing = cache.find_covering(box)
        if existing is not None:
            return {
                "ok": True,
                "cached": True,
                "vertices": water._count_vertices(existing),
                "message": "Area already cached.",
            }
        try:
            elements = water.fetch_overpass(*box)
        except Exception as exc:  # network / endpoint failure
            logger.warning("chart prefetch fetch failed: %s", exc)
            return {
                "ok": False,
                "cached": False,
                "vertices": 0,
                "message": f"Could not download chart (offline?): {exc}",
            }
        water_ll = water.assemble_water(elements)
        if water_ll.is_empty:
            return {
                "ok": False,
                "cached": False,
                "vertices": 0,
                "message": "No mapped water found in that area.",
            }
        try:
            cache.store(box, water_ll)
        except OSError as exc:  # pragma: no cover - disk failure
            logger.warning("chart cache store failed: %s", exc)
            return {
                "ok": False,
                "cached": False,
                "vertices": water._count_vertices(water_ll),
                "message": f"Fetched chart but failed to cache it: {exc}",
            }
        return {
            "ok": True,
            "cached": True,
            "vertices": water._count_vertices(water_ll),
            "message": "Chart downloaded and cached for offline routing.",
        }

    def list_charts(self) -> dict:
        """List cached chart bboxes + on-disk sizes (for the UI to show/manage)."""
        import json as _json

        from ..nav import water

        rt = self._rt
        cache = water.WaterCache(rt.config.data_dir)
        charts: list[dict] = []
        if cache.dir.exists():
            for meta_path in sorted(cache.dir.glob("*.json")):
                try:
                    meta = _json.loads(meta_path.read_text())
                    bbox = meta["bbox"]
                except (OSError, ValueError, KeyError):
                    continue
                wkb_path = meta_path.with_suffix(".wkb")
                size = wkb_path.stat().st_size if wkb_path.exists() else 0
                charts.append(
                    {
                        "bbox": bbox,
                        "vertices": meta.get("vertices", 0),
                        "size_bytes": size,
                    }
                )
        return {"charts": charts}

    def clear_charts(self) -> dict:
        """Delete every cached chart. Returns how many were removed."""
        from ..nav import water

        rt = self._rt
        cache = water.WaterCache(rt.config.data_dir)
        removed = 0
        if cache.dir.exists():
            for path in list(cache.dir.glob("*.wkb")) + list(cache.dir.glob("*.json")):
                try:
                    path.unlink()
                    if path.suffix == ".wkb":
                        removed += 1
                except OSError as exc:  # pragma: no cover - disk failure
                    logger.warning("could not remove cached chart %s: %s", path, exc)
        return {"ok": True, "removed": removed, "message": f"Cleared {removed} cached chart(s)."}

    # ------------------------------------------------------------------ #
    # Depth-map gridding (server-side averaging for the depth overlay)
    # ------------------------------------------------------------------ #

    def depth_grid(self, cell_m: float = 15.0, bbox=None, field: str = "depth") -> dict:
        """Server-side gridded chart: bins soundings into ~``cell_m`` metre cells
        averaging the value per cell, so the UI can paint an averaged colour chart
        instead of 100k individual dots. ``cell_m`` is clamped to 2..200.

        ``bbox`` = (west, south, east, north) limits the grid to that viewport
        window (Tier-1 windowing) so a large chart only ships what's on screen.
        ``field`` selects the layer: ``"depth"`` (default) or ``"hardness"``
        (bottom-hardness, raw 0..127) -- same gridding, different source.

        Returns ``{ok, field, cell_m, min_depth, max_depth, count, cells}``; the
        chart changes slowly, so the UI polls this rather than the 5 Hz telemetry.
        """
        rt = self._rt
        try:
            cell = float(cell_m)
        except (TypeError, ValueError):
            cell = 15.0
        cell = max(2.0, min(200.0, cell))
        source = rt.depth_map.hardness if field == "hardness" else None
        grid = rt.depth_map.as_grid(cell, bbox=bbox, source=source)
        grid["ok"] = True
        grid["field"] = field
        return grid

    def depth_at(self, lat: float, lon: float) -> dict:
        """Best-known depth at a point (nearest sounding within ~100 m, else the
        nearest imported contour vertex) for the map long-press menu.
        ``{ok, depth_m?, source?, dist_m?}``."""
        rt = self._rt
        hit = rt.depth_map.depth_at(lat, lon)
        if hit is None:
            return {"ok": False}
        return {"ok": True, **hit}

    def depth_contours(self, bbox=None, limit: int = 20000) -> dict:
        """Imported depth contours (isobath polylines) windowed to a
        (west, south, east, north) bbox. Returns ``{ok, count, contours}`` where
        each contour is ``{d: depth_m, pts: [[lat, lon], ...]}``."""
        rt = self._rt
        cs = rt.depth_map.contours_in(bbox=bbox, limit=limit)
        return {"ok": True, "count": len(cs), "contours": cs}

    def depth_composition(self, bbox=None, limit: int = 30000) -> dict:
        """Imported bottom-composition polygons, windowed to a
        (west, south, east, north) bbox. Returns ``{ok, count, polygons}`` where
        each is ``{pct: 0..100, ring: [[lat, lon], ...]}`` -- rendered FILLED
        (a vector polygon layer; not rasterised)."""
        rt = self._rt
        ps = rt.depth_map.composition_in(bbox=bbox, limit=limit)
        return {"ok": True, "count": len(ps), "polygons": ps}

    def import_depth_map(self, filename: str, data: bytes, replace: bool = False) -> dict:
        """Import soundings from an uploaded open-format depth file (CSV/XYZ or
        GeoJSON, optionally gzip-compressed -- detected by the magic bytes, not
        the extension). ``replace`` swaps the whole chart; otherwise the
        soundings are merged in. Persists to ``depthmap.json`` so the import
        survives restarts.

        Memory: a large GeoJSON/JSONL CHART upload is spilled to a temp file, the
        in-RAM HTTP body is freed, and the file is parsed with the BOUNDED
        streaming reader (columnar builders, one feature at a time) -- so the
        parse never adds a second full decoded-string copy + all-feature dict
        lists on top of the body. NOTE: the uploaded ``data`` bytes are inherently
        resident (FastAPI reads the whole HTTP body before this runs), so the UI
        upload's transient peak is bounded by the body size itself (~= the file);
        for a 512 MB device the on-device MIGRATION / offline .npz path (which
        never holds the body in RAM) is the fully-bounded route -- see
        ``DepthMap._migrate_json_chart``. Small CSV/XYZ soundings stay on the
        in-memory path (they're tiny)."""
        from ..nav.depth import (ColumnarFeatures, open_depth_text,
                                 parse_depth_features, sniff_depth_head,
                                 stream_parse_depth_features)

        rt = self._rt
        name = (filename or "").lower()
        # Classify by the DECOMPRESSED leading byte so a gzipped upload is routed
        # by its real content, not a (possibly generic or double) extension.
        head = sniff_depth_head(data) if data else b""
        is_geojson = name.endswith((".geojson", ".json", ".geojsonl", ".ndjson", ".jsonl")) \
            or head in (b"{", b"[")
        try:
            if is_geojson:
                import tempfile

                tmp = tempfile.NamedTemporaryFile(
                    mode="wb", suffix=".chartupload",
                    dir=rt.config.data_dir, delete=False)
                tmp_name = tmp.name
                try:
                    tmp.write(data)
                    tmp.flush()
                    tmp.close()
                    del data                    # free the HTTP body ASAP
                    # ``open_depth_text`` gunzips lazily when the spilled file
                    # starts with the gzip magic bytes, so the streaming parse
                    # stays bounded on a gzipped chart too.
                    with open_depth_text(tmp_name) as fh:
                        parsed = stream_parse_depth_features(fh)
                finally:
                    try:
                        os.remove(tmp_name)
                    except OSError:             # pragma: no cover - defensive
                        pass
            else:
                parsed = parse_depth_features(filename, data)
        except Exception as exc:  # noqa: BLE001 - any parse error -> clean message
            logger.warning("depth import parse failed: %s", exc)
            return {"ok": False, "error": f"could not parse the file: {exc}", "imported": 0}
        pts = parsed["soundings"]
        hard = parsed["hardness"]
        cont = parsed.get("contours", [])
        comp = parsed.get("composition", [])
        if not pts and not hard and not cont and not comp:
            return {"ok": False, "error": "no valid (lat, lon, depth) soundings found in the file",
                    "imported": 0}
        dm = rt.depth_map
        if replace:
            dm.points = []
            dm.hardness = []
            # Reset the vector layers to EMPTY COLUMNAR stores (not plain lists):
            # ``extend`` then concatenates the parsed columnar arrays in place, so
            # a large replace-import stays bounded (a plain-list ``extend`` would
            # iterate the columnar result back into a full dict list -- the ~1.7 GB
            # shape this store exists to avoid).
            dm.contours = ColumnarFeatures.empty("d", "pts")
            dm.composition = ColumnarFeatures.empty("pct", "ring")
            dm._last = None
        dm.points.extend(pts)
        dm.hardness.extend(hard)
        dm.contours.extend(cont)
        dm.composition.extend(comp)
        if len(dm.points) > dm.max_points:
            dm.points = dm.points[-dm.max_points:]
        if len(dm.hardness) > dm.max_points:
            dm.hardness = dm.hardness[-dm.max_points:]
        dm.save(rt._depth_map_path)           # soundings
        dm.save_chart(rt._depth_chart_path)   # static chart (hardness/contours/composition)
        rt._depth_saved_n = len(dm.points)
        logger.info("imported %d soundings + %d hardness + %d contours + %d composition from %s",
                    len(pts), len(hard), len(cont), len(comp), filename)
        return {"ok": True, "imported": len(pts), "hardness": len(hard),
                "contours": len(cont), "composition": len(comp), "total": len(dm.points)}
