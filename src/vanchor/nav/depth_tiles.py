"""Server-rendered raster tiles for the static bottom-composition overlay (#117).

Pure Pillow renderer + XYZ tile math -- no server / runtime coupling, so it is
unit-testable headless. The runtime layer (``runtime/depth.py``) wraps this with
the write-once disk + RAM-LRU cache; the design lives in ``docs/depth-tiles.md``.

Fills are drawn **opaque** and the client ``L.TileLayer`` applies the ~0.6
opacity, so overlapping polygons never double-darken (matches the vector
overlay's single-alpha look). Edges are blended with a Gaussian blur; a render
margin (``pad_px``) pulls in neighbouring-tile polygons so the blur has no seam
at tile borders. The tile is rendered at ``supersample``x then downsampled for
antialiased, edgeless bands.
"""
from __future__ import annotations

import io
import math

# YlOrBr ramp -- the SAME stops as the JS COMPOSITION_STOPS / cmapper's YlOrBr,
# so the tiles match the vector overlay exactly. (fraction, (r, g, b))
_COMPOSITION_STOPS = (
    (0.00, (255, 255, 229)),
    (0.25, (254, 227, 145)),
    (0.50, (254, 153, 41)),
    (0.75, (217, 95, 14)),
    (1.00, (153, 52, 4)),
)

TILE_PX = 256          # tile edge in CSS px (256, per the #117 decision)
_MERC_LAT_LIMIT = 85.05112878   # web-mercator clamp
# Bump when the RENDERED OUTPUT changes (style/blur/clip) so the cache version
# rolls and old tiles re-render instead of being served stale. 1 = #117/#118,
# 2 = #128 water-mask clip.
RENDERER_VERSION = 2


def composition_rgb(pct: float) -> tuple[int, int, int]:
    """Map a composition percentage (0..100) to an (r, g, b) on the YlOrBr ramp."""
    f = max(0.0, min(1.0, (pct or 0.0) / 100.0))
    a, b = _COMPOSITION_STOPS[0], _COMPOSITION_STOPS[-1]
    for i in range(len(_COMPOSITION_STOPS) - 1):
        if _COMPOSITION_STOPS[i][0] <= f <= _COMPOSITION_STOPS[i + 1][0]:
            a, b = _COMPOSITION_STOPS[i], _COMPOSITION_STOPS[i + 1]
            break
    span = b[0] - a[0]
    t = 0.0 if span <= 0 else (f - a[0]) / span
    return tuple(int(round(a[1][k] + (b[1][k] - a[1][k]) * t)) for k in range(3))


def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(west, south, east, north) WGS84 degrees for slippy/XYZ tile ``z/x/y``."""
    n = 1 << z

    def lon(xt: float) -> float:
        return xt / n * 360.0 - 180.0

    def lat(yt: float) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yt / n))))

    return (lon(x), lat(y + 1), lon(x + 1), lat(y))


def lonlat_to_tile(lat: float, lon: float, z: int) -> tuple[int, int]:
    """The (x, y) slippy tile containing (lat, lon) at zoom ``z`` (clamped)."""
    n = 1 << z
    xt = int((lon + 180.0) / 360.0 * n)
    lat = max(-_MERC_LAT_LIMIT, min(_MERC_LAT_LIMIT, lat))
    r = math.radians(lat)
    yt = int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n)
    return (max(0, min(n - 1, xt)), max(0, min(n - 1, yt)))


def tiles_covering(bbox: tuple[float, float, float, float], z: int):
    """Yield ``(x, y)`` tiles covering the (west, south, east, north) bbox at ``z``."""
    w, s, e, n = bbox
    x0, y0 = lonlat_to_tile(n, w, z)   # NW corner (north -> smaller y)
    x1, y1 = lonlat_to_tile(s, e, z)   # SE corner
    for x in range(min(x0, x1), max(x0, x1) + 1):
        for y in range(min(y0, y1), max(y0, y1) + 1):
            yield x, y


def count_tiles_covering(bbox: tuple[float, float, float, float],
                         zmin: int, zmax: int) -> int:
    """Number of tiles covering ``bbox`` over ``zmin..zmax`` (no materialisation)."""
    w, s, e, n = bbox
    total = 0
    for z in range(zmin, zmax + 1):
        x0, y0 = lonlat_to_tile(n, w, z)
        x1, y1 = lonlat_to_tile(s, e, z)
        total += (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1)
    return total


def _merc_y01(lat_deg: float) -> float:
    """Normalised web-mercator Y in [0, 1] (0 at the north edge of the world)."""
    lat = math.radians(max(-_MERC_LAT_LIMIT, min(_MERC_LAT_LIMIT, lat_deg)))
    return (1.0 - math.log(math.tan(lat) + 1.0 / math.cos(lat)) / math.pi) / 2.0


def padded_query_bbox(z: int, x: int, y: int, *, tile_px: int = TILE_PX,
                      pad_px: int = 8) -> tuple[float, float, float, float]:
    """The (west, south, east, north) to query composition for when rendering
    tile ``z/x/y`` -- the tile bounds grown by the blur margin so edge blur pulls
    in neighbouring polygons (no tile-seam)."""
    w, s, e, n = tile_bounds(z, x, y)
    fx = pad_px / tile_px
    dw = (e - w) * fx
    dn = (n - s) * fx
    return (w - dw, s - dn, e + dw, n + dn)


def render_composition_tile(features, z: int, x: int, y: int, *,
                            tile_px: int = TILE_PX, supersample: int = 2,
                            blur_px: float = 2.0, pad_px: int = 8,
                            water=None) -> bytes | None:
    """Render composition ``features`` -> a ``tile_px`` PNG (bytes), or ``None``
    when nothing is drawn (an empty tile -- the caller serves a shared
    transparent PNG and does not persist it, to spare SD writes).

    ``features`` is an iterable of ``{"pct": float, "ring": [[lat, lon], ...]}``
    (exactly what ``DepthMap.composition_in(bbox)`` yields), for the padded bbox.

    ``water`` (optional, #128) clips the fill to the shoreline: an iterable of
    ``(exterior, holes)`` polygons in **(lon, lat)** -- the blurred fill's alpha
    is masked to the water so it does not bleed onto land. ``None`` -> unclipped.
    """
    from PIL import Image, ImageChops, ImageDraw, ImageFilter   # lazy: keep off hot paths

    n = 1 << z
    ss = max(1, int(supersample))
    S = tile_px * ss
    P = pad_px * ss

    def to_px(lat: float, lon: float) -> tuple[float, float]:
        px = ((lon + 180.0) / 360.0 * n - x) * S + P
        py = (_merc_y01(lat) * n - y) * S + P
        return (px, py)

    img = Image.new("RGBA", (S + 2 * P, S + 2 * P), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    drew = False
    for feat in features:
        ring = feat.get("ring") or []
        if len(ring) < 3:
            continue
        pts = [to_px(float(p[0]), float(p[1])) for p in ring]
        draw.polygon(pts, fill=composition_rgb(feat.get("pct", 0.0)) + (255,))
        drew = True
    if not drew:
        return None

    if blur_px > 0:
        img = img.filter(ImageFilter.GaussianBlur(blur_px * ss))
    if water:
        # Mask the (blurred) fill to the water polygon so it can't bleed onto land
        # (#128). Exterior rings -> keep (255), island holes -> drop (0).
        mask = Image.new("L", img.size, 0)
        md = ImageDraw.Draw(mask)
        painted = False
        for exterior, holes in water:
            if len(exterior) >= 3:
                md.polygon([to_px(lat, lon) for lon, lat in exterior], fill=255)
                painted = True
            for hole in holes or ():
                if len(hole) >= 3:
                    md.polygon([to_px(lat, lon) for lon, lat in hole], fill=0)
        if painted:
            img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
    img = img.crop((P, P, P + S, P + S))
    if ss != 1:
        img = img.resize((tile_px, tile_px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_contours_tile(features, z: int, x: int, y: int, *,
                         tile_px: int = TILE_PX, supersample: int = 2,
                         pad_px: int = 4) -> bytes | None:
    """Render depth-contour ``features`` (isobaths) -> a ``tile_px`` PNG, or
    ``None`` when nothing is drawn.

    Thin dark semi-transparent lines (the nautical "composition + contours" chart
    look) that read cleanly over the soft composition fill -- major isobaths
    (every 5 m) a touch stronger. ``features`` is ``{"d": depth, "pts": [[lat,
    lon], ...]}`` (``DepthMap.contours_in(bbox)``). Rendered at ``supersample``x
    then downsampled for crisp antialiased lines; a render margin keeps lines
    continuous across tile seams. No blur (lines stay sharp)."""
    from PIL import Image, ImageDraw

    n = 1 << z
    ss = max(1, int(supersample))
    S = tile_px * ss
    P = pad_px * ss

    def to_px(lat: float, lon: float) -> tuple[float, float]:
        px = ((lon + 180.0) / 360.0 * n - x) * S + P
        py = (_merc_y01(lat) * n - y) * S + P
        return (px, py)

    img = Image.new("RGBA", (S + 2 * P, S + 2 * P), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    drew = False
    for feat in features:
        ll = feat.get("pts") or []
        if len(ll) < 2:
            continue
        major = round(float(feat.get("d", 0.0))) % 5 == 0
        colour = (28, 28, 28, 210 if major else 150)     # #1c1c1c, darker for major
        width = int(round((1.5 if major else 1.0) * ss))
        draw.line([to_px(float(p[0]), float(p[1])) for p in ll],
                  fill=colour, width=width, joint="curve")
        drew = True
    if not drew:
        return None

    img = img.crop((P, P, P + S, P + S))
    if ss != 1:
        img = img.resize((tile_px, tile_px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def transparent_tile(tile_px: int = TILE_PX) -> bytes:
    """A fully-transparent ``tile_px`` PNG -- served for empty tiles (and cached
    in RAM, never written to disk)."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (tile_px, tile_px), (0, 0, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()
