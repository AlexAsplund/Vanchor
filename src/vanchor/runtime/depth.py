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
import hashlib
import logging
import math
import os
import shutil
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING

from ..nav import depth_tiles

if TYPE_CHECKING:
    from ..core.models import GeoPoint

logger = logging.getLogger("vanchor.app")

# Composition raster-tile cache (#117): RAM-LRU in front of a write-once disk
# cache. Static data -> each tile is written at most once (SD-friendly); the LRU
# serves hot tiles with no disk read and bounds RAM.
_TILE_LRU_MAX = 256          # ~a few MB of PNG bytes
_TILE_MIN_ZOOM = 9           # below this a tile spans too much data to render


class DepthService:
    """Depth soundings, chart management, contour routing -- split out of Runtime."""

    def __init__(self, rt) -> None:
        self._rt = rt   # back-reference to Runtime for shared state
        self._tile_lru: "OrderedDict[tuple, bytes]" = OrderedDict()
        self._tile_lru_lock = threading.Lock()
        self._transparent_tile: bytes | None = None

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
        self._gc_stale_tiles()                # drop the previous chart's raster tiles (#119)
        logger.info("imported %d soundings + %d hardness + %d contours + %d composition from %s",
                    len(pts), len(hard), len(cont), len(comp), filename)
        return {"ok": True, "imported": len(pts), "hardness": len(hard),
                "contours": len(cont), "composition": len(comp), "total": len(dm.points)}

    # ------------------------------------------------------------------ #
    # Static-chart raster tiles (#116) -- server-rendered, write-once cached.
    # Composition (#117) + contours (#118) share the cache + version machinery.
    # ------------------------------------------------------------------ #

    def _transparent(self) -> bytes:
        if self._transparent_tile is None:
            self._transparent_tile = depth_tiles.transparent_tile()
        return self._transparent_tile

    def _tiles_root(self) -> str:
        return os.path.join(self._rt.config.data_dir, "tiles")

    def _pin_path(self) -> str:
        return os.path.join(self._tiles_root(), "pinned")

    def _auto_version(self) -> str:
        """Version derived from the persisted ``.npz`` (mtime + size) -- changes
        on a real re-import, not on every request. Shared by both tiled layers
        (same ``.npz``)."""
        from ..nav.depth import DepthMap

        npz = DepthMap._npz_path(self._rt._depth_chart_path)
        try:
            st = os.stat(npz)
            sig = f"{st.st_mtime_ns}-{st.st_size}"
        except OSError:
            dm = self._rt.depth_map
            sig = (f"mem-{len(getattr(dm, 'composition', ()) or ())}"
                   f"-{len(getattr(dm, 'contours', ()) or ())}")
        return hashlib.sha1(sig.encode()).hexdigest()[:12]

    def _pinned_version(self) -> str | None:
        """The frozen version in 'static' mode, or ``None`` (auto)."""
        try:
            with open(self._pin_path(), "r", encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            return None

    def _chart_tiles_version(self) -> str:
        """The EFFECTIVE tile version: the pinned value in 'static' mode (tiles
        never re-key/re-render on a chart change), else the auto value. Doubles
        as the on-disk cache subdir + the client ``?v=``."""
        return self._pinned_version() or self._auto_version()

    def tiles_mode(self) -> str:
        return "static" if self._pinned_version() else "auto"

    def set_tiles_mode(self, mode: str) -> dict:
        """``auto`` -> tiles re-render when the chart changes; ``static`` ->
        freeze the current tile version (no re-key / re-render / writes on a
        change, until cleared). Prunes now-stale version dirs."""
        want_static = str(mode).lower() in ("static", "pinned")
        if want_static:
            os.makedirs(self._tiles_root(), exist_ok=True)
            self._write_atomic_text(self._pin_path(), self._auto_version())
        else:
            try:
                os.remove(self._pin_path())
            except OSError:
                pass
        self._gc_stale_tiles()
        return {"ok": True, "mode": self.tiles_mode(),
                "version": self._chart_tiles_version()}

    def clear_tiles(self) -> dict:
        """Wipe the whole server tile cache (all layers + versions) and reset to
        auto; tiles re-render lazily on demand afterwards."""
        try:
            shutil.rmtree(self._tiles_root())
        except FileNotFoundError:
            pass
        except OSError as exc:              # pragma: no cover - disk failure
            logger.warning("could not clear tile cache: %s", exc)
        with self._tile_lru_lock:
            self._tile_lru.clear()
        return {"ok": True, "message": "Cleared the server tile cache."}

    def _gc_stale_tiles(self) -> None:
        """Remove tile version dirs that are no longer the effective version --
        after a re-import the old version's tiles are orphaned (SD hygiene)."""
        keep = self._chart_tiles_version()
        for layer in ("composition", "contours"):
            ldir = os.path.join(self._tiles_root(), layer)
            try:
                versions = os.listdir(ldir)
            except OSError:
                continue
            for ver in versions:
                p = os.path.join(ldir, ver)
                if ver != keep and os.path.isdir(p):
                    try:
                        shutil.rmtree(p)
                    except OSError as exc:  # pragma: no cover - disk failure
                        logger.warning("could not GC stale tiles %s: %s", p, exc)

    @staticmethod
    def _write_atomic_text(path: str, text: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)

    def tiles_info(self) -> dict:
        """Metadata the client needs to mount the tile layers: which static
        layers exist, the cache-busting version, tile size, min render zoom,
        and the invalidation mode (auto/static)."""
        dm = self._rt.depth_map
        return {"ok": True,
                "has_composition": bool(getattr(dm, "composition", None)),
                "has_contours": bool(getattr(dm, "contours", None)),
                "version": self._chart_tiles_version(),
                "mode": self.tiles_mode(),
                "tile_size": depth_tiles.TILE_PX, "min_zoom": _TILE_MIN_ZOOM}

    def _layer_tile(self, layer: str, z: int, x: int, y: int,
                    features_fn, render_fn) -> bytes:
        """Shared tile path for a static layer: RAM-LRU -> disk (write-once) ->
        render. Empty tiles return a shared transparent PNG cached in RAM only
        (never written, to spare SD writes). Out-of-range / too-far-out (< min
        zoom) tiles are transparent."""
        rt = self._rt
        transparent = self._transparent()
        n = (1 << z) if 0 <= z <= 24 else 0
        if z < _TILE_MIN_ZOOM or n == 0 or not (0 <= x < n and 0 <= y < n):
            return transparent

        version = self._chart_tiles_version()
        key = (layer, version, z, x, y)
        hit = self._tile_lru_get(key)
        if hit is not None:
            return hit

        path = os.path.join(rt.config.data_dir, "tiles", layer,
                            version, str(z), str(x), f"{y}.png")
        try:
            with open(path, "rb") as fh:
                png = fh.read()
            self._tile_lru_put(key, png)
            return png
        except OSError:
            pass   # not cached on disk yet -> render below

        feats = features_fn(depth_tiles.padded_query_bbox(z, x, y))
        png = render_fn(feats, z, x, y)
        if png is None:                       # empty tile: RAM-cache, no SD write
            self._tile_lru_put(key, transparent)
            return transparent
        self._write_tile_atomic(path, png)    # write-once
        self._tile_lru_put(key, png)
        return png

    def composition_tile(self, z: int, x: int, y: int) -> bytes:
        """A bottom-composition raster-tile PNG for slippy tile ``z/x/y`` (#117)."""
        if not getattr(self._rt.depth_map, "composition", None):
            return self._transparent()
        return self._layer_tile(
            "composition", z, x, y,
            lambda bbox: self._rt.depth_map.composition_in(bbox, limit=200000),
            depth_tiles.render_composition_tile)

    def contours_tile(self, z: int, x: int, y: int) -> bytes:
        """A depth-contour (isobath) raster-tile PNG for slippy tile ``z/x/y`` (#118)."""
        if not getattr(self._rt.depth_map, "contours", None):
            return self._transparent()
        return self._layer_tile(
            "contours", z, x, y,
            lambda bbox: self._rt.depth_map.contours_in(bbox, limit=200000),
            depth_tiles.render_contours_tile)

    def _tile_lru_get(self, key: tuple) -> bytes | None:
        with self._tile_lru_lock:
            png = self._tile_lru.get(key)
            if png is not None:
                self._tile_lru.move_to_end(key)
            return png

    def _tile_lru_put(self, key: tuple, png: bytes) -> None:
        with self._tile_lru_lock:
            self._tile_lru[key] = png
            self._tile_lru.move_to_end(key)
            while len(self._tile_lru) > _TILE_LRU_MAX:
                self._tile_lru.popitem(last=False)

    @staticmethod
    def _write_tile_atomic(path: str, png: bytes) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(png)
            os.replace(tmp, path)             # atomic; matches the chart writers
        except OSError as exc:                # pragma: no cover - defensive
            logger.warning("could not persist depth tile %s: %s", path, exc)
