"""Water geometry: fetch, assemble, project and cache navigable-water polygons.

The smart router needs a polygon of *navigable water* (lake/sea minus islands)
to plan a route that never crosses land. The authoritative free source is
OpenStreetMap, queried through the Overpass API.

Two non-obvious things this module gets right (both verified on the sim area):

1. **Relation assembly.** Many lakes (including the sim's lake *Visten*, OSM
   relation 287548) are stored as ``natural=water`` *multipolygon relations*,
   not as single closed ways. A naive "closed ways only" extractor finds zero
   ways containing the boat and wrongly reports it as *not in water*. We stitch
   each relation's ``outer`` member ways into rings with
   :func:`shapely.ops.polygonize`, and subtract the ``inner`` rings (islands).

2. **Metric projection.** All routing maths (buffering, distances, simplify)
   happens in a metre-based UTM CRS, never in degrees.

A successfully assembled polygon is cached as WKB under
``<data_dir>/water_cache/`` so the boat can plan routes offline after a single
online fetch ("fetch at the dock, run on the water").
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from shapely.geometry import LineString, MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, transform, unary_union

logger = logging.getLogger("vanchor.nav.water")

# Public Overpass endpoints, tried in order on error / rate-limit. Override at
# deploy-time with the comma-separated ``VANCHOR_OVERPASS_URLS`` env var.
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
# The public endpoint returns HTTP 406 without a real User-Agent (verified).
# Override with ``VANCHOR_USER_AGENT``.
USER_AGENT = "vanchor-ng/2.0 (+https://github.com/your-org/vanchor-ng)"


def overpass_endpoints() -> tuple[str, ...]:
    """The Overpass endpoints to try, in order.

    Reads ``VANCHOR_OVERPASS_URLS`` (comma-separated) at call time, falling back
    to the built-in :data:`OVERPASS_ENDPOINTS` when it is unset/empty.
    """
    raw = os.environ.get("VANCHOR_OVERPASS_URLS")
    if raw:
        urls = tuple(u.strip() for u in raw.split(",") if u.strip())
        if urls:
            return urls
    return OVERPASS_ENDPOINTS


def user_agent() -> str:
    """The HTTP User-Agent for Overpass requests.

    Reads ``VANCHOR_USER_AGENT`` at call time, falling back to the built-in
    :data:`USER_AGENT`.
    """
    return os.environ.get("VANCHOR_USER_AGENT") or USER_AGENT


# --------------------------------------------------------------------------- #
# Coordinate projection (lat/lon <-> local metric UTM)
# --------------------------------------------------------------------------- #
def utm_epsg_for(lon: float, lat: float) -> int:
    """EPSG code of the UTM zone containing ``(lon, lat)``."""
    zone = int((lon + 180.0) // 6.0) + 1
    return (32600 if lat >= 0 else 32700) + zone


@dataclass
class Projection:
    """A reusable lat/lon <-> metric transform pair around an area of interest."""

    epsg: int
    _to_m: object
    _to_ll: object

    @classmethod
    def for_point(cls, lon: float, lat: float) -> "Projection":
        import pyproj

        epsg = utm_epsg_for(lon, lat)
        to_m = pyproj.Transformer.from_crs(4326, epsg, always_xy=True).transform
        to_ll = pyproj.Transformer.from_crs(epsg, 4326, always_xy=True).transform
        return cls(epsg=epsg, _to_m=to_m, _to_ll=to_ll)

    def to_metric(self, geom: BaseGeometry) -> BaseGeometry:
        return transform(self._to_m, geom)

    def to_lonlat(self, geom: BaseGeometry) -> BaseGeometry:
        return transform(self._to_ll, geom)

    def point_to_metric(self, lon: float, lat: float) -> tuple[float, float]:
        return self._to_m(lon, lat)

    def point_to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        return self._to_ll(x, y)


# --------------------------------------------------------------------------- #
# Overpass query construction + parsing
# --------------------------------------------------------------------------- #
def overpass_query(south: float, west: float, north: float, east: float) -> str:
    """Overpass QL fetching water ways + relations (and coastline) in a bbox."""
    bbox = f"{south},{west},{north},{east}"
    return (
        "[out:json][timeout:60];"
        "("
        f'way["natural"="water"]({bbox});'
        f'relation["natural"="water"]({bbox});'
        f'way["natural"="coastline"]({bbox});'
        ");"
        "out geom;"
    )


def _coords_of(geometry: Iterable[dict]) -> list[tuple[float, float]]:
    """Convert Overpass ``geometry`` (list of {lat,lon}) to (lon,lat) tuples."""
    return [(g["lon"], g["lat"]) for g in geometry if "lon" in g and "lat" in g]


def assemble_water(elements: list[dict]) -> MultiPolygon:
    """Assemble a navigable-water polygon from raw Overpass elements.

    Handles closed ways directly, stitches multipolygon relations from their
    ``outer`` member ways (the critical step -- see module docstring), and
    subtracts island (``inner`` / standalone) rings as holes.
    """
    water_polys: list[Polygon] = []
    island_polys: list[Polygon] = []

    for el in elements:
        etype = el.get("type")
        if etype == "way":
            coords = _coords_of(el.get("geometry", []))
            if len(coords) >= 4 and coords[0] == coords[-1]:
                poly = Polygon(coords)
                if poly.is_valid or not poly.is_empty:
                    water_polys.append(poly)
        elif etype == "relation":
            outer_lines: list[LineString] = []
            inner_lines: list[LineString] = []
            for member in el.get("members", []):
                if member.get("type") != "way":
                    continue
                coords = _coords_of(member.get("geometry", []))
                if len(coords) < 2:
                    continue
                line = LineString(coords)
                if member.get("role") == "inner":
                    inner_lines.append(line)
                else:  # "outer" or unspecified
                    outer_lines.append(line)
            if outer_lines:
                merged = unary_union(outer_lines)
                for ring in polygonize(merged):
                    water_polys.append(ring)
            if inner_lines:
                merged = unary_union(inner_lines)
                for ring in polygonize(merged):
                    island_polys.append(ring)

    if not water_polys:
        return MultiPolygon()

    water = unary_union(water_polys)
    if island_polys:
        islands = unary_union(island_polys)
        water = water.difference(islands)

    # Repair any self-intersections from messy OSM data.
    if not water.is_valid:
        water = water.buffer(0)
    if water.geom_type == "Polygon":
        water = MultiPolygon([water])
    elif water.geom_type != "MultiPolygon":
        # GeometryCollection etc.: keep only polygonal parts.
        polys = [g for g in getattr(water, "geoms", []) if g.geom_type == "Polygon"]
        water = MultiPolygon(polys)
    return water


# --------------------------------------------------------------------------- #
# Network fetch (lazy: never imported at module load, never hit in tests)
# --------------------------------------------------------------------------- #
def fetch_overpass(
    south: float, west: float, north: float, east: float, *, timeout: float = 60.0
) -> list[dict]:
    """Fetch raw water elements from Overpass (tries endpoints in order)."""
    import requests

    query = overpass_query(south, west, north, east)
    last_exc: Exception | None = None
    for url in overpass_endpoints():
        try:
            resp = requests.post(
                url,
                data={"data": query},
                headers={"User-Agent": user_agent()},
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json().get("elements", [])
        except Exception as exc:  # pragma: no cover - network path
            logger.warning("Overpass fetch from %s failed: %s", url, exc)
            last_exc = exc
    raise RuntimeError(f"all Overpass endpoints failed: {last_exc}")


# --------------------------------------------------------------------------- #
# Bounding box helpers
# --------------------------------------------------------------------------- #
def bbox_around(
    a_lat: float, a_lon: float, b_lat: float, b_lon: float, *, pad_m: float = 2000.0
) -> tuple[float, float, float, float]:
    """A padded (south, west, north, east) bbox covering both points.

    Padding grows with the point separation (so a long route has room to go
    around obstacles), capped so we never request an enormous area.
    """
    sep_m = math.hypot(
        (b_lat - a_lat) * 111_320.0,
        (b_lon - a_lon) * 111_320.0 * math.cos(math.radians((a_lat + b_lat) / 2)),
    )
    pad = max(pad_m, min(20_000.0, sep_m))
    mid_lat = (a_lat + b_lat) / 2
    dlat = pad / 111_320.0
    dlon = pad / (111_320.0 * max(0.1, math.cos(math.radians(mid_lat))))
    south = min(a_lat, b_lat) - dlat
    north = max(a_lat, b_lat) + dlat
    west = min(a_lon, b_lon) - dlon
    east = max(a_lon, b_lon) + dlon
    return (south, west, north, east)


# --------------------------------------------------------------------------- #
# WKB cache
# --------------------------------------------------------------------------- #
class WaterCache:
    """Persists assembled water polygons (lon/lat WGS84) as WKB on disk.

    A cache entry covers a bbox; a lookup succeeds when a cached polygon's bbox
    covers the requested bbox, so a single dock-side fetch serves many routes.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.dir = Path(data_dir) / "water_cache"

    def _ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _key(bbox: tuple[float, float, float, float]) -> str:
        rounded = tuple(round(v, 2) for v in bbox)
        return hashlib.sha1(repr(rounded).encode()).hexdigest()[:16]

    def store(
        self, bbox: tuple[float, float, float, float], water_ll: BaseGeometry
    ) -> Path:
        from shapely import wkb

        self._ensure_dir()
        key = self._key(bbox)
        wkb_path = self.dir / f"{key}.wkb"
        wkb_path.write_bytes(wkb.dumps(water_ll))
        (self.dir / f"{key}.json").write_text(
            json.dumps(
                {
                    "bbox": list(bbox),
                    "vertices": _count_vertices(water_ll),
                }
            )
        )
        return wkb_path

    def find_covering(
        self, bbox: tuple[float, float, float, float]
    ) -> BaseGeometry | None:
        """Return a cached polygon whose bbox covers ``bbox``, else None."""
        from shapely import wkb

        if not self.dir.exists():
            return None
        s, w, n, e = bbox
        for meta_path in self.dir.glob("*.json"):
            try:
                meta = json.loads(meta_path.read_text())
                cs, cw, cn, ce = meta["bbox"]
            except (OSError, ValueError, KeyError):
                continue
            if cs <= s and cw <= w and cn >= n and ce >= e:
                wkb_path = meta_path.with_suffix(".wkb")
                if wkb_path.exists():
                    return wkb.loads(wkb_path.read_bytes())
        return None


def _count_vertices(geom: BaseGeometry) -> int:
    total = 0
    for poly in getattr(geom, "geoms", [geom]):
        ext = getattr(poly, "exterior", None)
        if ext is not None:
            total += len(ext.coords)
            for ring in poly.interiors:
                total += len(ring.coords)
    return total


def load_geojson(path: str | Path) -> MultiPolygon:
    """Load a water polygon saved as GeoJSON (used by tests / fixtures)."""
    data = json.loads(Path(path).read_text())
    geom = shape(data["geometry"] if "geometry" in data else data)
    if geom.geom_type == "Polygon":
        geom = MultiPolygon([geom])
    return geom
