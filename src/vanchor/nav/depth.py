"""Depth-map recorder: accumulate (position, depth) soundings as the boat moves.

This is the data behind the toggleable depth-map overlay -- a breadcrumb of
soundings that builds up automatically. (Interpolating a continuous contour
surface from these points is a future enhancement.)
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from array import array as _pyarray

import numpy as np

try:
    import orjson
except ImportError:
    orjson = None

from ..core.geo import haversine_m
from ..core.models import GeoPoint

# Metres per degree of latitude (≈ constant); longitude is scaled by cos(lat).
_M_PER_DEG_LAT = 111_320.0

logger = logging.getLogger("vanchor.depth")


def _json_dumps(obj) -> bytes:
    """Serialise to compact JSON bytes (orjson when available, ~9x faster on
    the large chart; falls back to the stdlib json encoded to UTF-8)."""
    if orjson is not None:
        return orjson.dumps(obj)
    return json.dumps(obj).encode("utf-8")


def _json_loads(data):
    """Deserialise JSON from bytes or str (orjson accepts both; the stdlib
    fallback also accepts both)."""
    if orjson is not None:
        return orjson.loads(data)
    return json.loads(data)


# --------------------------------------------------------------------------- #
# Bounded incremental JSON-array reader (streaming migration / import)          #
# --------------------------------------------------------------------------- #
# The legacy chart JSON (259 MB) and a big uploaded FeatureCollection both hold
# ONE giant array we want to walk element-by-element. Decoding the whole document
# (``json.loads`` / ``text = fh.read()``) needs a transient peak of the whole
# file as a Python ``str`` (+ boxed lists) -- ~600 MB / ~1.7 GB, which OOMs a
# 512 MB device. Instead we read the file in fixed chunks into a small sliding
# buffer, locate the target array, and ``JSONDecoder.raw_decode`` ONE element at
# a time, refilling the buffer when an element straddles a chunk boundary and
# compacting away consumed bytes so the buffer stays a few MB. Peak memory is
# O(chunk_size + one element), independent of file size.

_STREAM_CHUNK = 1 << 20  # 1 MiB read granularity for the sliding buffer.


def _stream_json_array(fh, key: str | None, chunk_size: int = _STREAM_CHUNK):
    """Yield the elements of a top-level JSON array from the text stream ``fh``.

    * ``key`` given -> the array is the value of ``"<key>": [ ... ]`` (the array
      is located by scanning for the ``"<key>"`` marker; safe here because the
      target key names never appear inside the numeric feature data).
    * ``key`` is ``None`` -> the document IS a bare top-level array ``[ ... ]``.

    Only ONE element is materialised at a time. ``fh`` must support ``read(n)``
    returning ``str`` (e.g. a text file, or ``io.TextIOWrapper(io.BytesIO(...))``).
    """
    dec = json.JSONDecoder()
    buf = ""
    eof = False

    def refill() -> bool:
        nonlocal buf, eof
        if eof:
            return False
        piece = fh.read(chunk_size)
        if not piece:
            eof = True
            return False
        buf += piece
        return True

    # -- 1. Locate the opening '[' of the target array -------------------- #
    if key is None:
        while True:
            pos = buf.find("[")
            if pos >= 0:
                break
            if not refill():
                return
        idx = pos + 1
    else:
        marker = '"%s"' % key
        while True:
            pos = buf.find(marker)
            if pos >= 0:
                break
            # Not in the current buffer. Compact the scanned head BEFORE pulling
            # the next chunk (so the search buffer can't grow unbounded skimming a
            # huge file), keeping a ``len(marker)-1`` tail so a marker straddling
            # the compaction point is still found once the next chunk lands.
            if len(buf) >= len(marker):
                buf = buf[-(len(marker) - 1):]
            if not refill():
                return
        br = buf.find("[", pos + len(marker))
        while br < 0:
            if not refill():
                return
            br = buf.find("[", pos + len(marker))
        idx = br + 1

    # Drop the located header so raw_decode indices stay small.
    buf = buf[idx:]
    idx = 0

    # -- 2. Pull one element at a time ------------------------------------ #
    while True:
        # Skip whitespace and inter-element commas (raw_decode does NOT skip
        # leading whitespace, so we must land exactly on a value start).
        while idx < len(buf) and buf[idx] in " \t\r\n,":
            idx += 1
        if idx >= len(buf):
            if refill():
                continue
            return                      # ran out mid-array (truncated) -> stop
        if buf[idx] == "]":
            return                      # array closed -> done
        try:
            obj, end = dec.raw_decode(buf, idx)
        except json.JSONDecodeError:
            # The element straddles the chunk boundary: pull more and retry.
            if refill():
                continue
            return                      # incomplete at EOF -> stop
        yield obj
        idx = end
        # Compact consumed bytes so the buffer stays ~O(chunk_size).
        if idx >= chunk_size:
            buf = buf[idx:]
            idx = 0


# --------------------------------------------------------------------------- #
# Columnar store for the STATIC imported vector layers (contours, composition) #
# --------------------------------------------------------------------------- #
# The imported static chart is huge -- ~140k composition polygons + ~84k depth
# contours = ~10M vertices. Held as Python lists-of-lists-of-boxed-floats (the
# old shape) that is ~1.7 GB resident and needs a ~2.9 GB transient peak to load
# via a whole-file json.loads. Packed as float32 arrays it is ~80 MB.
#
# ``ColumnarFeatures`` keeps each layer as three flat numpy arrays -- a packed
# vertex array, per-feature ring offsets, and a per-feature scalar (depth / pct)
# -- plus a precomputed per-feature bbox array so windowing is a vectorised mask
# instead of a 161 ms Python scan. Plain-Python ``{d/pct, pts/ring}`` dicts (the
# frozen API shape) are materialised ONLY for the <= limit selected features.
#
# It also behaves enough like a list (``len``/``bool``/iter/getitem/``extend``)
# for the frozen import path in app.py, which does ``dm.contours.extend(...)``
# and reads ``len(...)`` directly.


class _FeatureBuilder:
    """Accumulate features incrementally into flat ``array('f')`` buffers, so a
    huge import/migration never materialises all-Python intermediate dict lists.
    ``add(val, verts)`` appends one feature; ``build()`` freezes to numpy."""

    __slots__ = ("val_key", "vtx_key", "_coords", "_offsets", "_vals", "_n")

    def __init__(self, val_key: str, vtx_key: str) -> None:
        self.val_key = val_key
        self.vtx_key = vtx_key
        self._coords = _pyarray("f")      # flat [lat, lon, lat, lon, ...] float32
        self._offsets = _pyarray("q", [0])  # int64 ring boundaries
        self._vals = _pyarray("f")        # per-feature scalar (depth / pct)
        self._n = 0                       # vertices so far

    def add(self, val: float, verts) -> None:
        c = self._coords
        n = self._n
        for la, lo in verts:
            c.append(la)
            c.append(lo)
            n += 1
        self._n = n
        self._offsets.append(n)
        self._vals.append(val)

    def build(self) -> "ColumnarFeatures":
        coords = np.frombuffer(self._coords, dtype=np.float32).reshape(-1, 2)
        offsets = np.frombuffer(self._offsets, dtype=np.int64)
        vals = np.frombuffer(self._vals, dtype=np.float32)
        return ColumnarFeatures.from_arrays(coords, offsets, vals,
                                            self.val_key, self.vtx_key)


class ColumnarFeatures:
    """A static vector layer (contours or composition) stored columnar.

    * ``coords``  float32[N, 2]  -- all rings' vertices packed flat, [lat, lon]
    * ``offsets`` int64[F+1]     -- feature i's ring = coords[offsets[i]:offsets[i+1]]
    * ``vals``    float32[F]     -- per-feature scalar (``d`` depth / ``pct``)
    * ``bboxes``  float32[F, 4]  -- (lat_min, lon_min, lat_max, lon_max) per feature
    """

    __slots__ = ("coords", "offsets", "vals", "bboxes", "val_key", "vtx_key")

    @classmethod
    def from_arrays(cls, coords, offsets, vals, val_key: str, vtx_key: str) -> "ColumnarFeatures":
        obj = cls.__new__(cls)
        obj.coords = np.ascontiguousarray(coords, dtype=np.float32).reshape(-1, 2)
        obj.offsets = np.ascontiguousarray(offsets, dtype=np.int64).reshape(-1)
        obj.vals = np.ascontiguousarray(vals, dtype=np.float32).reshape(-1)
        obj.val_key = val_key
        obj.vtx_key = vtx_key
        obj.bboxes = cls._compute_bboxes(obj.coords, obj.offsets)
        return obj

    @classmethod
    def empty(cls, val_key: str, vtx_key: str) -> "ColumnarFeatures":
        return cls.from_arrays(np.empty((0, 2), np.float32), np.zeros(1, np.int64),
                               np.empty(0, np.float32), val_key, vtx_key)

    @staticmethod
    def _compute_bboxes(coords, offsets):
        f = len(offsets) - 1
        if f <= 0:
            return np.empty((0, 4), np.float32)
        starts = offsets[:-1]
        lat, lon = coords[:, 0], coords[:, 1]
        return np.stack(
            [np.minimum.reduceat(lat, starts), np.minimum.reduceat(lon, starts),
             np.maximum.reduceat(lat, starts), np.maximum.reduceat(lon, starts)],
            axis=1,
        ).astype(np.float32, copy=False)

    # -- list-ish surface (the frozen import path treats these like lists) -- #
    def __len__(self) -> int:
        return int(self.vals.shape[0])

    def __bool__(self) -> bool:
        return self.vals.shape[0] > 0

    def _feature(self, i: int) -> dict:
        a, b = int(self.offsets[i]), int(self.offsets[i + 1])
        # Round to 6 dp vectorised (float32 -> float64) to match the original
        # dict producer's precision, then hand out plain-Python nested lists.
        ring = np.round(self.coords[a:b].astype(np.float64), 6).tolist()
        return {self.val_key: round(float(self.vals[i]), 1), self.vtx_key: ring}

    def __getitem__(self, key):
        if isinstance(key, slice):
            return [self._feature(i) for i in range(*key.indices(len(self)))]
        if key < 0:
            key += len(self)
        return self._feature(key)

    def __iter__(self):
        for i in range(len(self)):
            yield self._feature(i)

    def extend(self, other) -> None:
        """Append features from another ColumnarFeatures or an iterable of dicts.
        Concatenates the underlying arrays (no per-feature dict blow-up when the
        source is already columnar)."""
        if isinstance(other, ColumnarFeatures):
            oc, oo, ov = other.coords, other.offsets, other.vals
        else:
            b = _FeatureBuilder(self.val_key, self.vtx_key)
            for d in other:
                verts = d.get(self.vtx_key) or []
                b.add(float(d.get(self.val_key, 0.0)),
                      [(p[0], p[1]) for p in verts
                       if isinstance(p, (list, tuple)) and len(p) >= 2])
            tmp = b.build()
            oc, oo, ov = tmp.coords, tmp.offsets, tmp.vals
        if ov.shape[0] == 0:
            return
        base = int(self.offsets[-1])
        self.coords = np.concatenate([self.coords, oc])
        self.offsets = np.concatenate([self.offsets, oo[1:] + base])
        self.vals = np.concatenate([self.vals, ov])
        self.bboxes = self._compute_bboxes(self.coords, self.offsets)

    def window(self, bbox, limit: int) -> list[dict]:
        """Features whose bbox intersects the (west, south, east, north) query
        box, capped at ``limit``. Vectorised over the precomputed bbox array."""
        if self.vals.shape[0] == 0:
            return []
        w, s, e, n = bbox
        bb = self.bboxes
        mask = (bb[:, 2] >= s) & (bb[:, 0] <= n) & (bb[:, 3] >= w) & (bb[:, 1] <= e)
        idx = np.nonzero(mask)[0]
        if limit is not None and idx.shape[0] > limit:
            idx = idx[:limit]
        return [self._feature(int(i)) for i in idx]

    @property
    def nbytes(self) -> int:
        return int(self.coords.nbytes + self.offsets.nbytes
                   + self.vals.nbytes + self.bboxes.nbytes)


def _as_columnar(store, val_key: str, vtx_key: str) -> ColumnarFeatures:
    """Coerce ``store`` (already-columnar, or a plain list of feature dicts left
    behind by app.py's replace-import path) to a ColumnarFeatures."""
    if isinstance(store, ColumnarFeatures):
        return store
    b = _FeatureBuilder(val_key, vtx_key)
    for d in (store or []):
        verts = d.get(vtx_key) or []
        b.add(float(d.get(val_key, 0.0)),
              [(p[0], p[1]) for p in verts
               if isinstance(p, (list, tuple)) and len(p) >= 2])
    return b.build()


def _window_dict_list(store, bbox, limit: int, vtx_key: str) -> list[dict]:
    """Legacy per-vertex windowing for a plain list of feature dicts (used when
    app.py's replace-import or a test assigns a raw list). Kept for byte-identical
    behaviour on that path; a feature is kept if ANY vertex falls inside."""
    if bbox is None:
        return store[:limit]
    w, s, e, n = bbox
    out: list[dict] = []
    for feat in store:
        for la, lo in feat.get(vtx_key, ()):
            if s <= la <= n and w <= lo <= e:
                out.append(feat)
                break
        if len(out) >= limit:
            break
    return out


class DepthMap:
    def __init__(self, min_distance_m: float = 3.0, max_points: int = 60000) -> None:
        self.min_distance_m = min_distance_m
        self.max_points = max_points
        # Each point is (lat, lon, depth_m).
        self.points: list[tuple[float, float, float]] = []
        # Parallel bottom-hardness layer (lat, lon, hardness_index) from imported
        # charts (bottom-hardness, raw 0..127 index); empty for live sonar,
        # which has no hardness. Gridded/windowed exactly like depth, own field.
        self.hardness: list[tuple[float, float, float]] = []
        # Imported depth contours (isobaths): each {"d": depth_m, "pts":
        # [[lat, lon], ...]}. A vector overlay, served windowed to the viewport.
        # Stored COLUMNAR (ColumnarFeatures) so the huge imported chart is ~80 MB
        # instead of ~1.7 GB; dicts are materialised only for windowed results.
        self.contours: ColumnarFeatures | list[dict] = ColumnarFeatures.empty("d", "pts")
        # Imported bottom-composition polygons: each {"pct": 0..100,
        # "ring": [[lat, lon], ...]}. A vector polygon overlay rendered FILLED
        # (not rasterised), served windowed. Also stored columnar.
        self.composition: ColumnarFeatures | list[dict] = ColumnarFeatures.empty("pct", "ring")
        self._last: GeoPoint | None = None

    # -- persistence ------------------------------------------------------ #
    # Recorded/imported SOUNDINGS live in the small depthmap.json (rewritten
    # often by the recorder). The large STATIC imported chart (hardness /
    # contours / composition) lives in a SEPARATE file written only on import,
    # so the recorder's periodic save stays tiny and never rewrites the big
    # chart (which was both slow and corruption-prone mid-write).
    @staticmethod
    def _atomic_write(path: str, obj: dict) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(_json_dumps(obj))
        os.replace(tmp, path)   # atomic: a kill/power-loss mid-write can't corrupt it

    def save(self, path: str) -> None:
        """Persist the soundings (small; called often by the recorder)."""
        if not self.points:
            return
        try:
            self._atomic_write(path, {"points": self.points})
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("could not save depth soundings: %s", exc)

    @staticmethod
    def _npz_path(chart_path: str) -> str:
        """The columnar sibling of the legacy JSON chart path (depthchart.json ->
        depthchart.npz). Derived internally so callers still pass the .json path."""
        root, _ = os.path.splitext(chart_path)
        return root + ".npz"

    def _save_npz(self, npz_path: str) -> None:
        """Write the static chart as compressed float32 arrays, atomically."""
        comp = _as_columnar(self.composition, "pct", "ring")
        cont = _as_columnar(self.contours, "d", "pts")
        hard = (np.asarray(self.hardness, dtype=np.float32).reshape(-1, 3)
                if self.hardness else np.empty((0, 3), np.float32))
        os.makedirs(os.path.dirname(npz_path) or ".", exist_ok=True)
        tmp = npz_path + ".tmp"
        with open(tmp, "wb") as fh:
            np.savez_compressed(
                fh,
                comp_coords=comp.coords, comp_offsets=comp.offsets, comp_vals=comp.vals,
                cont_coords=cont.coords, cont_offsets=cont.offsets, cont_vals=cont.vals,
                hard=hard,
            )
        os.replace(tmp, npz_path)   # atomic: matches the JSON writer's tmp+replace

    def _load_npz(self, npz_path: str) -> None:
        with np.load(npz_path) as z:
            self.composition = ColumnarFeatures.from_arrays(
                z["comp_coords"], z["comp_offsets"], z["comp_vals"], "pct", "ring")
            self.contours = ColumnarFeatures.from_arrays(
                z["cont_coords"], z["cont_offsets"], z["cont_vals"], "d", "pts")
            hard = z["hard"]
        self.hardness = [(round(float(r[0]), 6), round(float(r[1]), 6), round(float(r[2]), 1))
                         for r in hard][-self.max_points:]

    def _migrate_json_chart(self, chart_path: str, npz_path: str) -> None:
        """Parse a legacy depthchart.json into the columnar store with a BOUNDED
        peak and write the .npz.

        The legacy file is a single JSON object
        ``{"hardness": [...], "contours": [...], "composition": [...]}`` whose
        values are each a huge array. We stream it with :func:`_stream_json_array`
        -- read the file in fixed chunks, locate each target array, and
        ``raw_decode`` ONE element at a time (compacting consumed bytes) -- so the
        whole 259 MB document is never held as a Python ``str``. Peak stays at the
        columnar arrays (~91 MB) plus a few-MB sliding buffer, NOT + 259 MB. One
        pass per key (the file is re-opened + rescanned; the target key names
        never appear inside the numeric feature data, so a plain scan is safe)."""
        hard: list[tuple[float, float, float]] = []
        with open(chart_path, "r", encoding="utf-8", errors="replace") as fh:
            for el in _stream_json_array(fh, "hardness"):
                if isinstance(el, (list, tuple)) and len(el) == 3:
                    try:
                        hard.append((round(float(el[0]), 6), round(float(el[1]), 6),
                                     round(float(el[2]), 1)))
                    except (TypeError, ValueError):
                        continue
        cont_b = _FeatureBuilder("d", "pts")
        with open(chart_path, "r", encoding="utf-8", errors="replace") as fh:
            for el in _stream_json_array(fh, "contours"):
                if not isinstance(el, dict):
                    continue
                verts = [(p[0], p[1]) for p in (el.get("pts") or [])
                         if isinstance(p, (list, tuple)) and len(p) >= 2]
                if len(verts) >= 2:
                    cont_b.add(float(el.get("d", 0.0)), verts)
        comp_b = _FeatureBuilder("pct", "ring")
        with open(chart_path, "r", encoding="utf-8", errors="replace") as fh:
            for el in _stream_json_array(fh, "composition"):
                if not isinstance(el, dict):
                    continue
                verts = [(p[0], p[1]) for p in (el.get("ring") or [])
                         if isinstance(p, (list, tuple)) and len(p) >= 2]
                if len(verts) >= 3:
                    comp_b.add(float(el.get("pct", 0.0)), verts)
        self.hardness = hard[-self.max_points:]
        self.contours = cont_b.build()
        self.composition = comp_b.build()
        self._save_npz(npz_path)

    def save_chart(self, path: str) -> None:
        """Persist the STATIC imported chart (hardness/contours/composition) as a
        compressed columnar .npz beside the given (legacy .json) path; written
        only on import, not on every recorded sounding."""
        if not self.hardness and not self.contours and not self.composition:
            return
        try:
            self._save_npz(self._npz_path(path))
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("could not save depth chart: %s", exc)

    def load(self, path: str, chart_path: str | None = None) -> None:
        if os.path.exists(path):                      # soundings
            try:
                with open(path, "rb") as fh:
                    obj = _json_loads(fh.read())
                self.points = [tuple(p) for p in obj.get("points", []) if len(p) == 3][-self.max_points:]
                if self.points:
                    la, lo, _ = self.points[-1]
                    self._last = GeoPoint(la, lo)
            except (OSError, ValueError, TypeError) as exc:  # pragma: no cover
                logger.warning("could not load depth soundings: %s", exc)
        if chart_path:                                # static chart (separate file)
            npz_path = self._npz_path(chart_path)
            try:
                if os.path.exists(npz_path):
                    self._load_npz(npz_path)
                elif os.path.exists(chart_path):
                    # Legacy JSON present, no .npz yet: migrate once (bounded peak),
                    # write the .npz, and rename the JSON aside (keep the user's data).
                    logger.info("depth chart: migrating legacy JSON %s -> %s "
                                "(bounded parse)", chart_path, npz_path)
                    self._migrate_json_chart(chart_path, npz_path)
                    migrated = chart_path + ".migrated"
                    os.replace(chart_path, migrated)
                    logger.info("depth chart: migration complete; renamed %s -> %s",
                                chart_path, migrated)
            except (OSError, ValueError, TypeError) as exc:  # pragma: no cover
                logger.warning("could not load depth chart: %s", exc)
        logger.info("loaded %d soundings, %d hardness, %d contours, %d composition",
                    len(self.points), len(self.hardness), len(self.contours), len(self.composition))

    def record(self, point: GeoPoint | None, depth_m: float) -> None:
        if point is None or depth_m <= 0.0:
            return
        if self._last is None or haversine_m(self._last, point) >= self.min_distance_m:
            self.points.append((point.lat, point.lon, depth_m))
            self._last = point
            if len(self.points) > self.max_points:
                self.points = self.points[-self.max_points :]

    def as_list(self, limit: int = 600) -> list[list[float]]:
        """Most recent soundings as [[lat, lon, depth], ...] for the UI."""
        return [[la, lo, d] for la, lo, d in self.points[-limit:]]

    def depth_at(self, lat: float, lon: float, radius_m: float = 100.0) -> dict | None:
        """Best-known depth at a point, for the map long-press menu.

        Prefers the nearest recorded/imported SOUNDING within ``radius_m``;
        falls back to the nearest imported CONTOUR (isobath) vertex within the
        same radius, so areas charted with contour lines but no spot soundings
        still answer. Returns ``{depth_m, source: "sounding"|"contour",
        dist_m}`` or ``None`` when nothing is within range."""
        dlat = radius_m / _M_PER_DEG_LAT
        coslat = max(0.01, math.cos(math.radians(lat)))
        dlon = radius_m / (_M_PER_DEG_LAT * coslat)
        w, s, e, n = lon - dlon, lat - dlat, lon + dlon, lat + dlat

        def dist_m(la: float, lo: float) -> float:
            return math.hypot((la - lat) * _M_PER_DEG_LAT,
                               (lo - lon) * _M_PER_DEG_LAT * coslat)

        # Nearest sounding inside the radius box (cheap prefilter, then exact).
        best: tuple[float, float] | None = None  # (dist, depth)
        for la, lo, d in self.points:
            if s <= la <= n and w <= lo <= e:
                dm = dist_m(la, lo)
                if dm <= radius_m and (best is None or dm < best[0]):
                    best = (dm, d)
        if best is not None:
            return {"depth_m": round(best[1], 1), "source": "sounding",
                    "dist_m": round(best[0], 1)}

        # Fall back to the nearest imported contour vertex (isobath depth).
        for c in self.contours_in(bbox=(w, s, e, n), limit=200):
            d = c.get("d")
            for la, lo in c.get("pts", []):
                dm = dist_m(la, lo)
                if dm <= radius_m and (best is None or dm < best[0]):
                    best = (dm, float(d))
        if best is not None:
            return {"depth_m": round(best[1], 1), "source": "contour",
                    "dist_m": round(best[0], 1)}
        return None

    def contours_in(
        self,
        bbox: tuple[float, float, float, float] | None = None,
        limit: int = 5000,
    ) -> list[dict]:
        """Imported depth contours, windowed to a (west, south, east, north)
        bbox -- a contour polyline is kept if its bbox intersects the view.
        Capped at ``limit`` so a zoomed-out view can't ship the whole (huge)
        chart. Returns freshly-materialised ``{d, pts}`` dicts."""
        if not self.contours:
            return []
        if isinstance(self.contours, ColumnarFeatures):
            if bbox is None:
                return self.contours[:limit]
            return self.contours.window(bbox, limit)
        return _window_dict_list(self.contours, bbox, limit, "pts")

    def composition_in(
        self,
        bbox: tuple[float, float, float, float] | None = None,
        limit: int = 4000,
    ) -> list[dict]:
        """Imported composition polygons, windowed to a (west, south, east,
        north) bbox -- a polygon is kept if its bbox intersects the view.
        Returns freshly-materialised ``{pct, ring}`` dicts."""
        if not self.composition:
            return []
        if isinstance(self.composition, ColumnarFeatures):
            if bbox is None:
                return self.composition[:limit]
            return self.composition.window(bbox, limit)
        return _window_dict_list(self.composition, bbox, limit, "ring")

    # -- depth-aware routing: shallow no-go mask -------------------------- #
    def shallow_polygons(
        self,
        bbox: tuple[float, float, float, float] | None,
        min_depth_m: float,
        *,
        margin_m: float = 1.0,
        cell_m: float = 20.0,
        contour_band_m: float = 12.0,
        max_features: int = 4000,
    ):
        """Areas shallower than ``min_depth_m + margin_m`` as a shapely geometry
        in **LON/LAT** order (x=lon, y=lat), or ``None`` when the imported depth
        data yields no shallow area (so routing gets no false obstacles).

        Used by the router to proactively avoid shoals instead of relying on the
        reactive shallow-stop. Two complementary, deliberately COARSE sources --
        both DEPTH, never the bottom-composition/hardness layer:

        * **Soundings grid.** Windowed soundings (``bbox``) are binned into
          ~``cell_m`` metre square cells; a cell whose MEAN depth is below the
          threshold becomes a square shallow polygon. The cell size is the
          resolution of the shallow-edge approximation.
        * **Contours.** Windowed depth contours (isobaths) with depth below the
          threshold become shallow polygons: a CLOSED isobath is FILLED (its
          interior is the shoal); an OPEN one is BUFFERED into a thin strip
          ``contour_band_m`` wide (we only know the line is shallow, not which
          side is shallower, so a symmetric band is the conservative guess).

        Reuses the columnar bbox windowing and caps feature counts, so it stays
        cheap on a Pi (composition's ~140k polygons are never touched -- that
        layer is bottom hardness, not depth). Buffering/cell sizing converts
        metres to degrees with a flat mean-latitude factor (fine for a coarse
        safety mask).
        """
        import math as _math

        from shapely.geometry import LineString, Polygon
        from shapely.ops import unary_union

        threshold = float(min_depth_m) + float(margin_m)
        if threshold <= 0.0:
            return None
        polys: list = []

        if bbox is not None:
            w, s, e, n = bbox
        else:
            w, s, e, n = -180.0, -90.0, 180.0, 90.0

        # -- soundings -> square shallow cells ------------------------------ #
        pts = [p for p in self.points if s <= p[0] <= n and w <= p[1] <= e]
        if pts:
            mean_lat = sum(p[0] for p in pts) / len(pts)
            dlat = cell_m / _M_PER_DEG_LAT
            dlon = cell_m / (_M_PER_DEG_LAT * max(0.01, _math.cos(_math.radians(mean_lat))))
            lat0 = min(p[0] for p in pts)
            lon0 = min(p[1] for p in pts)
            acc: dict[tuple[int, int], list[float]] = {}
            for la, lo, d in pts:
                i = int((la - lat0) / dlat)
                j = int((lo - lon0) / dlon)
                b = acc.get((i, j))
                if b is None:
                    acc[(i, j)] = [d, 1.0]
                else:
                    b[0] += d
                    b[1] += 1.0
            for (i, j), (sd, cnt) in acc.items():
                if sd / cnt >= threshold:
                    continue
                la_c = lat0 + i * dlat
                lo_c = lon0 + j * dlon
                polys.append(Polygon([
                    (lo_c, la_c), (lo_c + dlon, la_c),
                    (lo_c + dlon, la_c + dlat), (lo_c, la_c + dlat),
                ]))
                if len(polys) >= max_features:
                    break

        # -- contours -> filled shoals / buffered strips -------------------- #
        if len(polys) < max_features and self.contours:
            band_deg = contour_band_m / _M_PER_DEG_LAT
            for c in self.contours_in(bbox=bbox, limit=max_features):
                d = c.get("d")
                pts_c = c.get("pts") or []
                if d is None or float(d) >= threshold or len(pts_c) < 2:
                    continue
                ring = [(p[1], p[0]) for p in pts_c]  # (lat, lon) -> (lon, lat)
                closed = (
                    len(ring) >= 4
                    and abs(ring[0][0] - ring[-1][0]) < 1e-9
                    and abs(ring[0][1] - ring[-1][1]) < 1e-9
                )
                try:
                    if closed:
                        poly = Polygon(ring)
                        if not poly.is_valid:
                            poly = poly.buffer(0)
                    else:
                        poly = LineString(ring).buffer(band_deg)
                except Exception:  # pragma: no cover - defensive geometry guard
                    continue
                if not poly.is_empty:
                    polys.append(poly)
                if len(polys) >= max_features:
                    break

        if not polys:
            return None
        mask = unary_union(polys)
        return None if mask.is_empty else mask

    def as_grid(
        self,
        cell_m: float = 15.0,
        max_cells: int = 3000,
        interpolate: bool = True,
        interp_radius: int = 5,
        interp_min_dirs: int = 6,
        interp_power: float = 2.0,
        radiate: bool = True,
        radiate_radius_m: float = 30.0,
        bbox: tuple[float, float, float, float] | None = None,
        source: list[tuple[float, float, float]] | None = None,
    ) -> dict:
        """Bin every sounding into a square grid (~``cell_m`` metres) and average
        depth per cell, returning a compact structure for the UI to colour-scale.

        Soundings span a small area, so we bin in a local metric frame using a
        flat metres-per-degree conversion at the data's mean latitude (cheap and
        accurate over the breadcrumb's extent -- no pyproj needed). Binning is a
        single O(n) pass over the points.

        The returned cell count is capped at ``max_cells`` by growing the
        effective cell size (doubling until the bins fit), and the cell size
        actually used is reported back so the client can label its colour scale.

        Two fill passes spread the measured data into the empty cells around it,
        in order of confidence:

        * **Radiate (nearest-neighbour / Voronoi).** When ``radiate`` is on
          (default), each empty cell within ``radiate_radius_m`` metres of a
          measured cell is assigned the depth of the *nearest* measured cell --
          the bottom is assumed roughly constant out to the Voronoi boundary
          where a different reading becomes nearer. The radius is bounded (a
          few cells) so one ping can't paint a whole lake and can't bleed into a
          neighbouring waterbody across an empty gap wider than the radius.
          Radiated cells are confident assumptions: ``"kind": "radiated"`` and
          ``"est": false``.

        * **Interpolate (enclosed-gap IDW).** When ``interpolate`` is on
          (default), *enclosed* empty cells -- holes surrounded by measured data
          in at least ``interp_min_dirs`` of the 8 compass directions within
          ``interp_radius`` cells -- get an inverse-distance-weighted estimate
          blended from the *differing* readings around them. These are genuine
          guesses between differing soundings: ``"kind": "interp"`` and
          ``"est": true``. Interp takes priority over radiate on any cell that
          qualifies, since a blend between differing readings is more honest
          there than picking the single nearest one.

        Measured cells carry ``"kind": "measured"`` and ``"est": false``. The
        deep middle of a sparsely-edged lake and any separate, far-away cluster
        stay untouched by both passes.

        Returns ``{cell_m, min_depth, max_depth, count, cells}`` where each cell
        is ``{"lat", "lon", "depth", "n", "est", "kind"}`` at the cell centre.
        """
        cell_m = max(1.0, float(cell_m))
        # ``source`` selects which layer to grid (defaults to depth ``points``;
        # pass ``self.hardness`` for the bottom-hardness grid -- same (lat, lon,
        # value) shape, so the binning/fill/windowing below is identical).
        # Tier-1 viewport windowing: with a bbox (west, south, east, north) only
        # the soundings inside it are gridded, so the client fetches just what is
        # on screen (+ its scroll padding) instead of the whole chart.
        pts = self.points if source is None else source
        if bbox is not None:
            w, s, e, n = bbox
            pts = [p for p in pts if s <= p[0] <= n and w <= p[1] <= e]
        if not pts:
            return {
                "cell_m": cell_m,
                "min_depth": 0.0,
                "max_depth": 0.0,
                "count": 0,
                "cells": [],
            }

        lats = [la for la, _, _ in pts]
        lons = [lo for _, lo, _ in pts]
        mean_lat = sum(lats) / len(lats)
        m_per_deg_lat = _M_PER_DEG_LAT
        m_per_deg_lon = _M_PER_DEG_LAT * max(0.01, math.cos(math.radians(mean_lat)))

        # Reference corner so cell indices are small non-negative integers.
        lat0, lon0 = min(lats), min(lons)

        # Grow the cell size until the binned cell count fits under the cap. The
        # number of distinct cells only ever shrinks as the cell grows, so a few
        # doublings converge quickly.
        while True:
            dlat = cell_m / m_per_deg_lat
            dlon = cell_m / m_per_deg_lon
            # accumulator: (i, j) -> [sum_depth, count]
            acc: dict[tuple[int, int], list[float]] = {}
            for la, lo, d in pts:
                i = int((la - lat0) / dlat)
                j = int((lo - lon0) / dlon)
                bucket = acc.get((i, j))
                if bucket is None:
                    acc[(i, j)] = [d, 1.0]
                else:
                    bucket[0] += d
                    bucket[1] += 1.0
            if len(acc) <= max_cells:
                break
            cell_m *= 2.0

        # Measured cells: (i, j) -> averaged depth.
        measured: dict[tuple[int, int], float] = {
            ij: sum_d / n for ij, (sum_d, n) in acc.items()
        }

        # -- interpolation pass: fill enclosed holes (uncertain guesses) ---- #
        # Estimated cells: (i, j) -> idw depth. Built only for empty cells that
        # are genuinely surrounded by *differing* measured data within
        # ``interp_radius``. These are the honest "between two readings" guesses.
        estimated: dict[tuple[int, int], float] = {}
        if interpolate and len(measured) >= 8 and len(measured) < max_cells:
            estimated = self._interpolate_holes(
                measured,
                radius=max(1, int(interp_radius)),
                min_dirs=max(1, min(8, int(interp_min_dirs))),
                power=float(interp_power),
                budget=max_cells - len(measured),
            )

        # -- radiate pass: nearest-neighbour (Voronoi) footprint ------------ #
        # Each empty cell within ``radiate_radius_m`` of a measured cell takes
        # the depth of the NEAREST measured cell. Bounded radius => one ping
        # paints only a few cells and can't cross an empty gap to a neighbouring
        # waterbody. Interp cells already claimed above take priority and are
        # skipped here. Remaining cell budget is shared after interp.
        radiated: dict[tuple[int, int], float] = {}
        radiate_cells = int(radiate_radius_m / cell_m)  # radius in cells
        budget = max_cells - len(measured) - len(estimated)
        if radiate and radiate_cells >= 1 and measured and budget > 0:
            radiated = self._radiate(
                measured,
                claimed=estimated,
                radius=radiate_cells,
                budget=budget,
            )

        cells: list[dict] = []
        min_depth = math.inf
        max_depth = -math.inf
        for (i, j), avg in measured.items():
            min_depth = min(min_depth, avg)
            max_depth = max(max_depth, avg)
            cells.append(
                {
                    "lat": lat0 + (i + 0.5) * dlat,
                    "lon": lon0 + (j + 0.5) * dlon,
                    "depth": avg,
                    "n": int(acc[(i, j)][1]),
                    "est": False,
                    "kind": "measured",
                }
            )
        for (i, j), avg in radiated.items():
            min_depth = min(min_depth, avg)
            max_depth = max(max_depth, avg)
            cells.append(
                {
                    "lat": lat0 + (i + 0.5) * dlat,
                    "lon": lon0 + (j + 0.5) * dlon,
                    "depth": avg,
                    "n": 0,
                    "est": False,
                    "kind": "radiated",
                }
            )
        for (i, j), avg in estimated.items():
            min_depth = min(min_depth, avg)
            max_depth = max(max_depth, avg)
            cells.append(
                {
                    "lat": lat0 + (i + 0.5) * dlat,
                    "lon": lon0 + (j + 0.5) * dlon,
                    "depth": avg,
                    "n": 0,
                    "est": True,
                    "kind": "interp",
                }
            )

        return {
            "cell_m": cell_m,
            "min_depth": min_depth,
            "max_depth": max_depth,
            "count": len(self.points),
            "cells": cells,
        }

    @staticmethod
    def _interpolate_holes(
        measured: dict[tuple[int, int], float],
        radius: int,
        min_dirs: int,
        power: float,
        budget: int,
    ) -> dict[tuple[int, int], float]:
        """Fill empty cells that are enclosed by measured cells via IDW.

        A cell counts as *enclosed* only when, scanning outward along the 8
        compass directions up to ``radius`` cells, a measured cell is met in at
        least ``min_dirs`` of those 8 directions. This is the guard that keeps
        us inside the data: the deep middle of a lake whose only soundings hug
        the shore fails the test (no measured cell within ``radius`` straight
        out), and an isolated cluster never reaches another cluster's cells.

        ``budget`` caps how many estimates we add so the total cell count stays
        under ``max_cells``; once exhausted we stop filling.
        """
        if budget <= 0:
            return {}

        # 8 compass directions as (di, dj).
        dirs = (
            (1, 0), (-1, 0), (0, 1), (0, -1),
            (1, 1), (1, -1), (-1, 1), (-1, -1),
        )

        # Candidate empty cells: only those within ``radius`` of a measured cell
        # (an enclosed cell must have measured neighbours nearby), collected from
        # the measured cells' neighbourhoods. This bounds the work at
        # O(measured * radius^2) -- INDEPENDENT of how widely the soundings are
        # spread. (Scanning the full measured bounding box is O(bbox_area *
        # radius^2), which explodes for sparse wide data -- a few thousand
        # soundings over a whole lake -> tens of seconds, freezing the single-
        # threaded event loop on one /api/depth/grid request.)
        candidates: set[tuple[int, int]] = set()
        for ci, cj in measured:
            for di in range(-radius, radius + 1):
                for dj in range(-radius, radius + 1):
                    cell = (ci + di, cj + dj)
                    if cell not in measured:
                        candidates.add(cell)

        estimated: dict[tuple[int, int], float] = {}
        for i, j in candidates:
            # Enclosure test: count directions with a measured hit in range.
            hits = 0
            for di, dj in dirs:
                for r in range(1, radius + 1):
                    if (i + di * r, j + dj * r) in measured:
                        hits += 1
                        break
                if hits >= min_dirs:
                    break  # already enclosed; no need to scan the rest
            if hits < min_dirs:
                continue

            # IDW over measured cells within the (square) radius window.
            wsum = 0.0
            dsum = 0.0
            for di in range(-radius, radius + 1):
                for dj in range(-radius, radius + 1):
                    if di == 0 and dj == 0:
                        continue
                    d = measured.get((i + di, j + dj))
                    if d is None:
                        continue
                    dist2 = di * di + dj * dj
                    w = 1.0 / (dist2 ** (power / 2.0))
                    wsum += w
                    dsum += w * d
            if wsum > 0.0:
                estimated[(i, j)] = dsum / wsum
                if len(estimated) >= budget:
                    return estimated
        return estimated

    @staticmethod
    def _radiate(
        measured: dict[tuple[int, int], float],
        claimed: dict[tuple[int, int], float],
        radius: int,
        budget: int,
    ) -> dict[tuple[int, int], float]:
        """Fill empty cells with the depth of the NEAREST measured cell.

        This is a bounded nearest-neighbour (Voronoi) extension: every empty
        cell within ``radius`` cells of at least one measured cell is assigned
        the depth of the closest measured cell (Chebyshev-windowed, Euclidean
        tie-break). The bottom is assumed roughly the same as the nearest
        sounding out to the Voronoi boundary -- the locus where a *different*
        reading becomes nearer.

        The radius is the critical guard: a cell more than ``radius`` cells from
        every sounding is never filled, so a single ping paints only its own
        small neighbourhood (it can't flood a whole lake) and the fill can't
        jump an empty gap wider than ``radius`` into a neighbouring waterbody.

        ``claimed`` cells (already taken by the enclosed-gap interpolation) are
        skipped so interp's honest blend wins where readings differ. ``budget``
        caps how many cells we add to respect ``max_cells``.
        """
        if budget <= 0:
            return {}

        # Iterate the MEASURED cells and paint their square ``radius`` window of
        # empty cells, keeping the NEAREST measured cell per empty cell. This is
        # O(measured * radius^2) -- bounded by ``max_cells`` -- and INDEPENDENT of
        # how widely or sparsely the soundings are spread. (Scanning the measured
        # bounding box instead is O(bbox_area * radius^2), which explodes for
        # sparse wide data -- e.g. a few thousand soundings over a whole lake at a
        # small cell size -- and stalls the single-threaded event loop for
        # seconds per /api/depth/grid request.)
        best: dict[tuple[int, int], tuple[int, float]] = {}  # cell -> (dist2, depth)
        for (i, j), depth in measured.items():
            for di in range(-radius, radius + 1):
                for dj in range(-radius, radius + 1):
                    if di == 0 and dj == 0:
                        continue
                    cell = (i + di, j + dj)
                    if cell in measured or cell in claimed:
                        continue
                    d2 = di * di + dj * dj
                    cur = best.get(cell)
                    if cur is None or d2 < cur[0]:
                        best[cell] = (d2, depth)

        radiated: dict[tuple[int, int], float] = {}
        for cell, (_, depth) in best.items():
            radiated[cell] = depth
            if len(radiated) >= budget:
                break
        return radiated


# --------------------------------------------------------------------------- #
# Importing depth maps from open formats (CSV/XYZ + GeoJSON)                   #
# --------------------------------------------------------------------------- #

_LAT_NAMES = {"lat", "latitude", "y"}
_LON_NAMES = {"lon", "lng", "long", "longitude", "x"}
_DEPTH_NAMES = {"depth", "depth_m", "z", "d", "depthm", "depthmeters"}
_SPLIT = re.compile(r"[,\t; ]+")


def parse_depth_soundings(filename: str, data: bytes) -> list[tuple[float, float, float]]:
    """Parse an imported depth file into ``(lat, lon, depth_m)`` soundings.

    Back-compat wrapper over :func:`parse_depth_features` returning just the
    depth soundings. Supports CSV/XYZ and GeoJSON (see that function).
    """
    return parse_depth_features(filename, data)["soundings"]


def parse_depth_features(filename: str, data: bytes) -> dict:
    """Parse an imported depth file into ``{"soundings", "hardness"}``.

    Supports the common OPEN formats: CSV/XYZ (one ``lat,lon,depth`` row each --
    header auto-detected, else positional; ``.xyz`` treated as ``lon,lat,depth``)
    and GeoJSON (Point/MultiPoint with a depth property or Z coordinate).
    ``soundings`` are ``(lat, lon, depth_m)`` positive-down; ``hardness`` are
    ``(lat, lon, index)`` from a ``hardness`` property on GeoJSON points
    (bottom-hardness, raw 0..127); ``contours`` are ``{d, pts}`` (depth
    + ``[[lat, lon], ...]`` polyline) from LineString features. Unparseable
    rows are skipped.
    """
    text = data.decode("utf-8", errors="replace")
    name = (filename or "").lower()
    # ``.geojsonl``/``.ndjson``/``.jsonl`` are newline-delimited GeoJSON (one
    # Feature per line) -- the format cmapper's chart export writes. Detection by
    # a leading ``{``/``[`` also catches an unlabelled JSONL stream.
    if name.endswith((".geojson", ".json", ".geojsonl", ".ndjson", ".jsonl")) \
            or text.lstrip()[:1] in "{[":
        return _parse_geojson_features(text)
    return {"soundings": _parse_csv_xyz_depth(text, xyz=name.endswith(".xyz")),
            "hardness": [],
            "contours": ColumnarFeatures.empty("d", "pts"),
            "composition": ColumnarFeatures.empty("pct", "ring")}


def _coerce(lat: float, lon: float, depth: float) -> tuple[float, float, float] | None:
    if abs(lat) > 90.0 and abs(lon) <= 90.0:  # looks lon/lat-swapped -> fix
        lat, lon = lon, lat
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return (round(lat, 6), round(lon, 6), round(abs(float(depth)), 1))


def _parse_csv_xyz_depth(text: str, xyz: bool = False) -> list[tuple[float, float, float]]:
    pts: list[tuple[float, float, float]] = []
    lines = text.splitlines()
    if not lines:
        return pts
    ilat = ilon = idep = None
    start = 0
    head = [h.strip().lower() for h in _SPLIT.split(lines[0].strip()) if h]
    if any(any(c.isalpha() for c in h) for h in head):  # header row present
        start = 1
        for i, h in enumerate(head):
            if h in _LAT_NAMES and ilat is None:
                ilat = i
            elif h in _LON_NAMES and ilon is None:
                ilon = i
            elif h in _DEPTH_NAMES and idep is None:
                idep = i
    for ln in lines[start:]:
        ln = ln.strip()
        if not ln or ln[0] in "#;":
            continue
        parts = [p for p in _SPLIT.split(ln) if p]
        try:
            if ilat is not None and ilon is not None and idep is not None:
                lat, lon, dep = float(parts[ilat]), float(parts[ilon]), float(parts[idep])
            elif len(parts) >= 3:
                a, b, dep = float(parts[0]), float(parts[1]), float(parts[2])
                lat, lon = (b, a) if xyz else (a, b)  # .xyz = lon,lat,z ; csv = lat,lon,depth
            else:
                continue
        except (ValueError, IndexError):
            continue
        p = _coerce(lat, lon, dep)
        if p:
            pts.append(p)
    return pts


def _iter_geojson_features(text: str):
    """Yield GeoJSON feature dicts from any of the shapes we accept:

    * a ``FeatureCollection`` object (yields its ``features``),
    * a single ``Feature`` / bare geometry object,
    * **newline-delimited GeoJSON (JSONL/NDJSON)** -- one Feature per line, as
      cmapper's chart export writes (hundreds of thousands of lines).

    JSONL is detected by the whole-document parse failing (a JSONL stream is not
    a single JSON value) and is then parsed **line by line**, so a huge export is
    processed with only one line held in memory at a time rather than decoding a
    giant JSON tree. Blank lines and unparseable lines are skipped."""
    if not text.strip():
        return
    try:
        obj = _json_loads(text)
    except ValueError:
        obj = None
    if obj is not None:
        if isinstance(obj, dict) and obj.get("type") == "FeatureCollection":
            yield from (obj.get("features") or [])
        elif isinstance(obj, list):
            yield from obj
        else:
            yield obj
        return
    # JSONL fallback: one JSON value per line (streaming, bounded memory).
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            feat = _json_loads(line)
        except ValueError:
            continue
        if isinstance(feat, dict) and feat.get("type") == "FeatureCollection":
            yield from (feat.get("features") or [])
        else:
            yield feat


def _geojson_depth_of(props: dict, coords: list) -> float | None:
    for k in ("depth", "depth_m", "z", "d", "DEPTH", "Depth"):
        if isinstance(props, dict) and k in props:
            try:
                return float(props[k])
            except (TypeError, ValueError):
                pass
    if len(coords) >= 3:
        try:
            return float(coords[2])
        except (TypeError, ValueError):
            pass
    return None


def _route_geojson_feature(f, soundings: list, hardness: list,
                           contours: "_FeatureBuilder", composition: "_FeatureBuilder") -> None:
    """Route ONE GeoJSON feature into the accumulators/builders.

    Point/MultiPoint depths -> soundings, a ``hardness`` property -> the hardness
    layer, LineString/MultiLineString -> contour polylines, Polygon/MultiPolygon
    with ``composition_pct`` -> filled composition polygons. Shared verbatim by
    the whole-text parse and the bounded streaming import so both apply identical
    rounding / [lat, lon] order / skip rules (>=2 contour, >=3 ring vertices)."""
    if not isinstance(f, dict):
        return
    geom = f.get("geometry", f)
    props = f.get("properties") if isinstance(f.get("properties"), dict) else {}
    if not isinstance(geom, dict):
        return
    gtype = geom.get("type")
    coords = geom.get("coordinates", [])
    ring = [coords] if gtype == "Point" else (coords if gtype == "MultiPoint" else [])
    for c in ring:
        if not isinstance(c, (list, tuple)) or len(c) < 2:
            continue
        dep = _geojson_depth_of(props, c)
        if dep is not None:
            try:
                p = _coerce(float(c[1]), float(c[0]), dep)
            except (TypeError, ValueError):
                p = None
            if p:
                soundings.append(p)
        h = props.get("hardness")
        if h is not None:
            try:
                hp = _coerce(float(c[1]), float(c[0]), float(h))
            except (TypeError, ValueError):
                hp = None
            if hp:
                hardness.append(hp)
    if gtype in ("LineString", "MultiLineString"):
        dep = _geojson_depth_of(props, [])
        if dep is None:
            return
        lines = [coords] if gtype == "LineString" else coords
        for line in lines:
            if not isinstance(line, (list, tuple)):
                continue
            pts = []
            for c in line:
                if isinstance(c, (list, tuple)) and len(c) >= 2:
                    try:
                        la, lo = float(c[1]), float(c[0])
                    except (TypeError, ValueError):
                        continue
                    if -90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0:
                        pts.append([round(la, 6), round(lo, 6)])
            if len(pts) >= 2:
                contours.add(round(abs(dep), 1), pts)
    if gtype in ("Polygon", "MultiPolygon"):
        # Composition is a VECTOR POLYGON layer: keep the rings + pct and render filled. Do NOT
        # rasterise/interpolate it -- that destroys the boundaries.
        pct = props.get("composition_pct")
        if pct is None:
            return
        try:
            pctv = float(pct)
        except (TypeError, ValueError):
            return
        polys = [coords] if gtype == "Polygon" else coords
        for poly in polys:
            if not isinstance(poly, (list, tuple)) or not poly:
                continue
            ring = []                                  # exterior ring
            for c in poly[0] if isinstance(poly[0], (list, tuple)) else []:
                if not isinstance(c, (list, tuple)) or len(c) < 2:
                    continue
                try:
                    la, lo = float(c[1]), float(c[0])
                except (TypeError, ValueError):
                    continue
                if -90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0:
                    ring.append([round(la, 6), round(lo, 6)])
            if len(ring) >= 3:
                composition.add(round(pctv, 1), ring)


def _parse_geojson_features(text: str) -> dict:
    """Walk a GeoJSON (FeatureCollection, single feature, or JSONL) ONCE, routing
    each feature via :func:`_route_geojson_feature` into soundings / hardness and
    columnar contour / composition builders (so a huge import never materialises
    Python dict lists for every feature -- the peak stays bounded)."""
    soundings: list[tuple[float, float, float]] = []
    hardness: list[tuple[float, float, float]] = []
    contours = _FeatureBuilder("d", "pts")
    composition = _FeatureBuilder("pct", "ring")
    for f in _iter_geojson_features(text):
        _route_geojson_feature(f, soundings, hardness, contours, composition)
    return {"soundings": soundings, "hardness": hardness,
            "contours": contours.build(), "composition": composition.build()}


def _iter_features_streaming(fh, chunk_size: int = _STREAM_CHUNK):
    """Yield GeoJSON feature dicts from a SEEKABLE text stream ``fh`` holding a
    FeatureCollection object, a bare feature array, or JSONL -- WITHOUT decoding
    the whole document at once. Format is sniffed from a small prefix:

    * ``{ ... "features": [ ... ] ... }`` (FeatureCollection) -> stream the
      ``features`` array element-by-element (:func:`_stream_json_array`).
    * ``[ ... ]`` (bare array) -> stream the array element-by-element.
    * otherwise -> JSONL / NDJSON: one JSON value per line (bounded per-line).

    ``fh`` must support ``seek`` + ``read`` + line iteration (a real file, or
    ``io.TextIOWrapper(io.BytesIO(...))``)."""
    fh.seek(0)
    probe = fh.read(4096)
    stripped = probe.lstrip()
    fh.seek(0)
    if not stripped:
        return
    if stripped[0] == "{" and ('"features"' in probe or '"FeatureCollection"' in probe):
        yield from _stream_json_array(fh, "features", chunk_size)
        return
    if stripped[0] == "[":
        yield from _stream_json_array(fh, None, chunk_size)
        return
    # JSONL / NDJSON fallback (one Feature per line -- cmapper's export format).
    for line in fh:
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            feat = _json_loads(line)
        except ValueError:
            continue
        if isinstance(feat, dict) and feat.get("type") == "FeatureCollection":
            yield from (feat.get("features") or [])
        else:
            yield feat


def stream_parse_depth_features(fh) -> dict:
    """Bounded streaming counterpart of :func:`parse_depth_features` for a large
    GeoJSON/JSONL CHART upload: consume the seekable text stream ``fh`` feature-
    by-feature into columnar builders, never building the full Python-dict list.
    Returns the same ``{"soundings", "hardness", "contours", "composition"}``
    shape. Peak memory is O(chunk + one feature) + the columnar arrays."""
    soundings: list[tuple[float, float, float]] = []
    hardness: list[tuple[float, float, float]] = []
    contours = _FeatureBuilder("d", "pts")
    composition = _FeatureBuilder("pct", "ring")
    for f in _iter_features_streaming(fh):
        _route_geojson_feature(f, soundings, hardness, contours, composition)
    return {"soundings": soundings, "hardness": hardness,
            "contours": contours.build(), "composition": composition.build()}
