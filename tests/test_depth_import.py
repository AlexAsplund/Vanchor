"""Depth-map import: parsing open formats (CSV/XYZ/GeoJSON) + the upload endpoint."""

import json

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.nav.depth import parse_depth_features, parse_depth_soundings


def test_parse_csv_with_header():
    pts = parse_depth_soundings("d.csv", b"lat,lon,depth\n59.66,13.32,12.5\n59.67,13.33,8.0\n")
    assert pts == [(59.66, 13.32, 12.5), (59.67, 13.33, 8.0)]


def test_parse_csv_no_header_positional_and_comments():
    pts = parse_depth_soundings("d.csv", b"# soundings\n59.66,13.32,12.5\n")
    assert pts == [(59.66, 13.32, 12.5)]


def test_parse_xyz_is_lon_lat_depth():
    # .xyz convention is x,y,z = lon,lat,depth, so it must come out lat,lon,depth.
    assert parse_depth_soundings("d.xyz", b"13.32 59.66 12.5\n") == [(59.66, 13.32, 12.5)]


def test_parse_geojson_property_and_z():
    gj = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [13.32, 59.66]},
             "properties": {"depth": 12.5}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [13.33, 59.67, 8.0]},
             "properties": {}},
        ],
    }
    pts = parse_depth_soundings("d.geojson", json.dumps(gj).encode())
    assert (59.66, 13.32, 12.5) in pts and (59.67, 13.33, 8.0) in pts


def test_parse_skips_garbage():
    assert parse_depth_soundings("d.csv", b"hello world\nfoo bar baz qux\n") == []


# The /api/depth/import route is thin glue over runtime.import_depth_map (and is
# verified live); we exercise the method directly -- a Runtime under the
# TestClient lifespan portal spins when it carries depth data, so we avoid it.
def _rt(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)  # isolate: empty chart, never touch the repo's depthmap.json
    return Runtime(cfg)


def test_import_merges_then_replaces(tmp_path):
    rt = _rt(tmp_path)
    csv = b"lat,lon,depth\n59.66,13.32,12.5\n59.67,13.33,8.0\n"
    r = rt.import_depth_map("d.csv", csv)
    assert r["ok"] and r["imported"] == 2 and r["total"] == 2
    assert rt.import_depth_map("d.csv", csv)["total"] == 4              # merged
    assert rt.import_depth_map("d.csv", csv, replace=True)["total"] == 2  # replaced


def test_import_rejects_unparseable(tmp_path):
    r = _rt(tmp_path).import_depth_map("d.csv", b"nonsense\n")
    assert r["ok"] is False and "no valid" in r["error"]


def test_parse_geojson_routes_kind_and_captures_hardness():
    gj = json.dumps({"type": "FeatureCollection", "features": [
        {"geometry": {"type": "Point", "coordinates": [18.0, 59.0]},
         "properties": {"depth_m": 12.0, "kind": "sounding", "hardness": 108}},
        {"geometry": {"type": "LineString", "coordinates": [[18.0, 59.0], [18.0, 59.01]]},
         "properties": {"depth_m": 10.0, "kind": "contour"}},
    ]}).encode()
    parsed = parse_depth_features("c.geojson", gj)
    assert parsed["soundings"] == [(59.0, 18.0, 12.0)]   # contour LineString skipped
    assert parsed["hardness"] == [(59.0, 18.0, 108.0)]   # hardness captured from the sounding


def test_import_keeps_hardness(tmp_path):
    rt = _rt(tmp_path)
    gj = json.dumps({"type": "FeatureCollection", "features": [
        {"geometry": {"type": "Point", "coordinates": [18.0, 59.0]},
         "properties": {"depth_m": 12.0, "kind": "sounding", "hardness": 108}},
    ]}).encode()
    r = rt.import_depth_map("c.geojson", gj)
    assert r["ok"] and r["imported"] == 1 and r["hardness"] == 1
    assert rt.depth_map.hardness == [(59.0, 18.0, 108.0)]


def test_parse_geojson_collects_contours():
    gj = json.dumps({"type": "FeatureCollection", "features": [
        {"geometry": {"type": "LineString", "coordinates": [[18.0, 59.0], [18.0, 59.01]]},
         "properties": {"depth_m": 15.0, "kind": "contour"}},
    ]}).encode()
    parsed = parse_depth_features("c.geojson", gj)
    assert parsed["contours"] == [{"d": 15.0, "pts": [[59.0, 18.0], [59.01, 18.0]]}]


def test_import_keeps_and_windows_contours(tmp_path):
    rt = _rt(tmp_path)
    gj = json.dumps({"type": "FeatureCollection", "features": [
        {"geometry": {"type": "LineString", "coordinates": [[18.0, 59.0], [18.0, 59.01]]},
         "properties": {"depth_m": 15.0, "kind": "contour"}},
    ]}).encode()
    r = rt.import_depth_map("c.geojson", gj)
    assert r["ok"] and r["contours"] == 1
    assert rt.depth_contours()["count"] == 1                       # all
    assert rt.depth_contours(bbox=(0, 0, 1, 1))["contours"] == []  # far window -> none


def _poly_fc():
    return json.dumps({"type": "FeatureCollection", "features": [
        {"geometry": {"type": "Polygon",
                      "coordinates": [[[18.0, 59.0], [18.0, 59.01], [18.01, 59.01], [18.0, 59.0]]]},
         "properties": {"composition_pct": 75.0, "kind": "composition"}},
    ]}).encode()


def test_parse_geojson_collects_composition_polygons():
    parsed = parse_depth_features("c.geojson", _poly_fc())
    assert len(parsed["composition"]) == 1
    assert parsed["composition"][0]["pct"] == 75.0
    assert parsed["composition"][0]["ring"][0] == [59.0, 18.0]   # [lat, lon]


def test_import_keeps_and_windows_composition(tmp_path):
    rt = _rt(tmp_path)
    r = rt.import_depth_map("c.geojson", _poly_fc())
    assert r["ok"] and r["composition"] == 1
    assert rt.depth_composition()["count"] == 1
    assert rt.depth_composition(bbox=(0, 0, 1, 1))["polygons"] == []  # far window -> none
