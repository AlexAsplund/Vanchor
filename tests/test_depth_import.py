"""Depth-map import: parsing open formats (CSV/XYZ/GeoJSON) + the upload endpoint."""

import json

import pytest

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
    # contours are now stored columnar; materialise to the frozen {d, pts} shape.
    # Coords are packed float32, so compare with a small tolerance (~1 m).
    cs = list(parsed["contours"])
    assert len(cs) == 1
    assert cs[0]["d"] == 15.0
    flat = [x for pt in cs[0]["pts"] for x in pt]
    assert flat == pytest.approx([59.0, 18.0, 59.01, 18.0], abs=1e-4)


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


# --- newline-delimited GeoJSON (JSONL/NDJSON) --------------------------------
# cmapper's real chart export writes one Feature per line (hundreds of thousands
# of lines), NOT a single FeatureCollection. A representative ~mixed subset:
_JSONL_SUBSET = "\n".join([
    '{"type": "Feature", "properties": {"depth_m": 35.0, "kind": "sounding", "hardness": 18}, "geometry": {"type": "Point", "coordinates": [12.317056, 59.729343]}}',
    '{"type": "Feature", "properties": {"depth_m": 10.0, "kind": "contour"}, "geometry": {"type": "LineString", "coordinates": [[12.308441, 59.72086], [12.308441, 59.720933], [12.308531, 59.721033]]}}',
    '{"type": "Feature", "properties": {"composition_pct": 75.0, "kind": "composition"}, "geometry": {"type": "Polygon", "coordinates": [[[12.310318, 59.721141], [12.310318, 59.721146], [12.310327, 59.721141], [12.310318, 59.721141]]]}}',
    "",                     # blank line (must be skipped, not fail the parse)
    "not json at all",     # garbage line (skipped)
    '{"type": "Feature", "properties": {"depth_m": 37.0, "kind": "sounding", "hardness": 13}, "geometry": {"type": "Point", "coordinates": [12.317083, 59.729343]}}',
])


def test_parse_jsonl_mixed_kinds():
    """A JSONL/NDJSON stream (one Feature per line) must parse -- the real export
    format. Previously the whole-document json.loads failed and imported NOTHING."""
    parsed = parse_depth_features("all.new.geojsonl", _JSONL_SUBSET.encode())
    assert len(parsed["soundings"]) == 2
    assert len(parsed["hardness"]) == 2
    assert len(parsed["contours"]) == 1
    assert len(parsed["composition"]) == 1
    assert parsed["soundings"][0] == (59.729343, 12.317056, 35.0)
    assert parsed["hardness"][0] == (59.729343, 12.317056, 18.0)
    assert parsed["composition"][0]["pct"] == 75.0
    assert list(parsed["composition"][0]["ring"][0]) == pytest.approx(
        [59.721141, 12.310318], abs=1e-4)  # [lat, lon], float32-packed


def test_parse_jsonl_detected_without_extension():
    """JSONL detected from the leading ``{`` even when the filename is generic."""
    parsed = parse_depth_features("upload.txt", _JSONL_SUBSET.encode())
    assert len(parsed["soundings"]) == 2 and len(parsed["composition"]) == 1


def test_import_jsonl_end_to_end(tmp_path):
    rt = _rt(tmp_path)
    r = rt.import_depth_map("all.new.geojsonl", _JSONL_SUBSET.encode())
    assert r["ok"]
    assert r["imported"] == 2          # soundings
    assert r["hardness"] == 2
    assert r["contours"] == 1
    assert r["composition"] == 1
    assert rt.depth_composition()["count"] == 1


def test_parse_malformed_geojson_returns_full_shape():
    """Malformed JSON must return the same four-key dict shape as a successful parse.

    Without the fix, the error path only returned ``soundings`` and ``hardness``,
    so callers that access ``contours`` or ``composition`` would get a KeyError
    instead of an empty list (currently safe only because the one caller uses
    ``.get``, but the contract should be explicit and consistent).
    """
    result = parse_depth_features("c.geojson", b"this is not json {{{")
    assert set(result.keys()) == {"soundings", "hardness", "contours", "composition"}, (
        "error path must return the same shape as the success path"
    )
    assert result["soundings"] == []
    assert len(result["contours"]) == 0       # empty columnar layer
    assert len(result["composition"]) == 0
