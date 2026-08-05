"""Composition raster tiles (#117): the pure Pillow renderer + tile math, and
the runtime's write-once disk + RAM-LRU cache."""

import io
import json
import math

from PIL import Image

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.nav import depth_tiles


# --- pure tile math + renderer --------------------------------------------- #

def test_tile_bounds_world_and_ordering():
    w, s, e, n = depth_tiles.tile_bounds(0, 0, 0)
    assert (round(w), round(e)) == (-180, 180)
    assert n > 0 > s and n == -s          # web-mercator is symmetric about 0
    assert round(n, 2) == 85.05
    # a child tile is inside its parent and correctly ordered
    w2, s2, e2, n2 = depth_tiles.tile_bounds(1, 1, 0)   # NE quadrant
    assert w2 == 0 and round(e2) == 180 and n2 > s2 >= 0


def test_padded_bbox_grows_the_tile():
    z, x, y = 13, 4400, 2400
    tw, ts, te, tn = depth_tiles.tile_bounds(z, x, y)
    pw, ps, pe, pn = depth_tiles.padded_query_bbox(z, x, y)
    assert pw < tw and ps < ts and pe > te and pn > tn


def test_composition_rgb_ramp_endpoints():
    assert depth_tiles.composition_rgb(0) == (255, 255, 229)     # YlOrBr low
    assert depth_tiles.composition_rgb(100) == (153, 52, 4)      # YlOrBr high
    mid = depth_tiles.composition_rgb(50)
    assert mid == (254, 153, 41)


def _deg2tile(lat, lon, z):
    n = 2 ** z
    xt = int((lon + 180.0) / 360.0 * n)
    r = math.radians(lat)
    yt = int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)
    return xt, yt


def _ring(lat, lon, half=0.004):
    return [[lat - half, lon - half], [lat - half, lon + half],
            [lat + half, lon + half], [lat + half, lon - half]]


def test_render_returns_opaque_png_for_a_polygon():
    lat, lon, z = 59.0, 18.0, 13
    x, y = _deg2tile(lat, lon, z)
    feats = [{"pct": 75.0, "ring": _ring(lat, lon)}]
    png = depth_tiles.render_composition_tile(feats, z, x, y)
    assert png is not None
    img = Image.open(io.BytesIO(png))
    assert img.size == (256, 256) and img.mode == "RGBA"
    alpha_max = img.split()[3].getextrema()[1]
    assert alpha_max > 0                    # something was actually drawn


def test_render_empty_features_is_none():
    assert depth_tiles.render_composition_tile([], 13, 4400, 2400) is None
    # a polygon entirely outside the tile draws nothing visible but still fills
    # its (off-tile) pixels; a degenerate <3-vertex ring is skipped -> None
    assert depth_tiles.render_composition_tile(
        [{"pct": 50, "ring": [[59.0, 18.0], [59.0, 18.001]]}], 13, 4400, 2400) is None


def test_transparent_tile_is_fully_transparent():
    img = Image.open(io.BytesIO(depth_tiles.transparent_tile()))
    assert img.size == (256, 256)
    assert img.split()[3].getextrema() == (0, 0)


# --- runtime cache (write-once disk + RAM-LRU) ----------------------------- #

def _rt(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    return Runtime(cfg)


_COMP = json.dumps({"type": "FeatureCollection", "features": [
    {"geometry": {"type": "Polygon", "coordinates": [[
        [18.0, 59.0], [18.008, 59.0], [18.008, 59.008], [18.0, 59.008], [18.0, 59.0]]]},
     "properties": {"composition_pct": 75.0, "kind": "composition"}},
]}).encode()


def test_tile_written_once_then_read_from_disk(tmp_path, monkeypatch):
    rt = _rt(tmp_path)
    assert rt.import_depth_map("c.geojson", _COMP, replace=True)["composition"] == 1
    z = 13
    x, y = _deg2tile(59.004, 18.004, z)

    calls = {"n": 0}
    real = depth_tiles.render_composition_tile

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(depth_tiles, "render_composition_tile", counting)

    png1 = rt.composition_tile(z, x, y)
    assert png1 and Image.open(io.BytesIO(png1)).split()[3].getextrema()[1] > 0
    assert calls["n"] == 1

    # persisted to <data_dir>/tiles/composition/<ver>/<z>/<x>/<y>.png
    ver = rt._depth.tiles_info()["version"]
    tile_path = tmp_path / "tiles" / "composition" / ver / str(z) / str(x) / f"{y}.png"
    assert tile_path.exists()

    # drop the RAM cache -> a second request must read DISK, not re-render
    rt._depth._tile_lru.clear()
    png2 = rt.composition_tile(z, x, y)
    assert png2 == png1
    assert calls["n"] == 1                  # write-once: no second render


def test_empty_tile_is_transparent_and_not_written(tmp_path):
    rt = _rt(tmp_path)
    rt.import_depth_map("c.geojson", _COMP, replace=True)
    z = 13
    # a tile far from the (59,18) polygon -> empty
    x, y = _deg2tile(40.0, -100.0, z)
    png = rt.composition_tile(z, x, y)
    assert png == depth_tiles.transparent_tile()
    ver = rt._depth.tiles_info()["version"]
    assert not (tmp_path / "tiles" / "composition" / ver / str(z)).exists()


def test_below_min_zoom_and_out_of_range_are_transparent(tmp_path):
    rt = _rt(tmp_path)
    rt.import_depth_map("c.geojson", _COMP, replace=True)
    tp = depth_tiles.transparent_tile()
    assert rt.composition_tile(3, 4, 2) == tp        # below _TILE_MIN_ZOOM
    assert rt.composition_tile(13, -1, 0) == tp      # x out of range
    assert rt.composition_tile(13, 0, 1 << 13) == tp  # y out of range


def test_no_composition_tile_is_transparent(tmp_path):
    rt = _rt(tmp_path)                                # nothing imported
    info = rt.tiles_info()
    assert info["has_composition"] is False and info["has_contours"] is False
    assert info["tile_size"] == 256
    assert rt.composition_tile(13, 4400, 2400) == depth_tiles.transparent_tile()
    assert rt.contours_tile(13, 4400, 2400) == depth_tiles.transparent_tile()


# --- contour tiles (#118) -------------------------------------------------- #

def test_render_contours_returns_png_for_a_line():
    lat, lon, z = 59.0, 18.0, 13
    x, y = _deg2tile(lat, lon, z)
    feats = [{"d": 5.0, "pts": [[lat - 0.004, lon - 0.004], [lat + 0.004, lon + 0.004]]}]
    png = depth_tiles.render_contours_tile(feats, z, x, y)
    assert png is not None
    img = Image.open(io.BytesIO(png))
    assert img.size == (256, 256) and img.mode == "RGBA"
    assert img.split()[3].getextrema()[1] > 0        # a line was drawn


def test_render_contours_empty_is_none():
    assert depth_tiles.render_contours_tile([], 13, 4400, 2400) is None
    assert depth_tiles.render_contours_tile(
        [{"d": 5, "pts": [[59.0, 18.0]]}], 13, 4400, 2400) is None   # <2 pts skipped


_COMP_AND_CONTOURS = json.dumps({"type": "FeatureCollection", "features": [
    {"geometry": {"type": "Polygon", "coordinates": [[
        [18.0, 59.0], [18.008, 59.0], [18.008, 59.008], [18.0, 59.008], [18.0, 59.0]]]},
     "properties": {"composition_pct": 75.0, "kind": "composition"}},
    {"geometry": {"type": "LineString",
                  "coordinates": [[18.0, 59.0], [18.004, 59.004], [18.008, 59.008]]},
     "properties": {"depth_m": 5.0, "kind": "contour"}},
]}).encode()


def test_contour_tile_written_once_then_read_from_disk(tmp_path, monkeypatch):
    rt = _rt(tmp_path)
    r = rt.import_depth_map("c.geojson", _COMP_AND_CONTOURS, replace=True)
    assert r["contours"] == 1 and r["composition"] == 1
    assert rt.tiles_info()["has_contours"] is True
    z = 13
    x, y = _deg2tile(59.004, 18.004, z)

    calls = {"n": 0}
    real = depth_tiles.render_contours_tile
    monkeypatch.setattr(depth_tiles, "render_contours_tile",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1) or real(*a, **k)))

    png1 = rt.contours_tile(z, x, y)
    assert png1 and Image.open(io.BytesIO(png1)).split()[3].getextrema()[1] > 0
    assert calls["n"] == 1
    ver = rt._depth.tiles_info()["version"]
    # contours persist under their OWN layer dir (distinct from composition's)
    assert (tmp_path / "tiles" / "contours" / ver / str(z) / str(x) / f"{y}.png").exists()
    assert not (tmp_path / "tiles" / "composition").exists()   # not fetched here

    rt._depth._tile_lru.clear()
    assert rt.contours_tile(z, x, y) == png1
    assert calls["n"] == 1                            # write-once: no re-render


def test_version_changes_when_composition_changes(tmp_path):
    rt = _rt(tmp_path)
    rt.import_depth_map("c.geojson", _COMP, replace=True)
    v1 = rt.tiles_info()["version"]
    bigger = json.dumps({"type": "FeatureCollection", "features": [
        {"geometry": {"type": "Polygon", "coordinates": [[
            [18.0, 59.0], [18.02, 59.0], [18.02, 59.02], [18.0, 59.02], [18.0, 59.0]]]},
         "properties": {"composition_pct": 40.0, "kind": "composition"}},
        {"geometry": {"type": "Polygon", "coordinates": [[
            [18.1, 59.1], [18.12, 59.1], [18.12, 59.12], [18.1, 59.12], [18.1, 59.1]]]},
         "properties": {"composition_pct": 90.0, "kind": "composition"}},
    ]}).encode()
    rt.import_depth_map("c2.geojson", bigger, replace=True)
    assert rt.tiles_info()["version"] != v1
