"""Tests for the depth source: DPT NMEA, bathymetry, sounder, depth map."""

import pytest

from vanchor.core.geo import destination_point
from vanchor.core.models import BoatState, GeoPoint
from vanchor.core.state import NavigationState
from vanchor.nav import nmea
from vanchor.nav.depth import DepthMap
from vanchor.nav.navigator import Navigator
from vanchor.sim.bathymetry import Bathymetry
from vanchor.sim.devices import SimDepthSounder


def test_dpt_roundtrip():
    parsed = nmea.parse(nmea.encode_dpt(7.3))
    assert isinstance(parsed, nmea.Depth)
    assert parsed.depth_m == pytest.approx(7.3, abs=0.1)


def test_dbt_parsed():
    body = "SDDBT,26.2,f,8.0,M,4.3,F"
    parsed = nmea.parse(f"${body}*{nmea.checksum(body)}")
    assert isinstance(parsed, nmea.Depth)
    assert parsed.depth_m == pytest.approx(8.0, abs=0.1)


def test_navigator_sets_depth():
    state = NavigationState()
    nav = Navigator(state, bus=None)
    nav.handle_sentence(nmea.encode_dpt(5.5))
    assert state.depth_m == pytest.approx(5.5, abs=0.1)


def test_bathymetry_within_bounds_and_varies():
    b = Bathymetry()
    depths = [b.depth_at(destination_point(b.origin, off, 45.0)) for off in (0, 50, 120, 250)]
    for d in depths:
        assert b.min_m <= d <= b.max_m
    assert max(depths) - min(depths) > 0.5  # it actually varies


def test_sim_depth_sounder_emits_valid_dpt():
    b = Bathymetry()
    sounder = SimDepthSounder(lambda: BoatState(point=b.origin), b, noise_m=0.0)
    parsed = nmea.parse(sounder.sample())
    assert isinstance(parsed, nmea.Depth)
    assert parsed.depth_m == pytest.approx(b.depth_at(b.origin), abs=0.1)


def test_depth_map_records_by_distance():
    dm = DepthMap(min_distance_m=10.0)
    p = GeoPoint(59.66275, 13.32247)
    dm.record(p, 8.0)
    dm.record(destination_point(p, 5.0, 0.0), 8.0)  # too close, skipped
    dm.record(destination_point(p, 15.0, 0.0), 9.0)
    assert len(dm.points) == 2
    assert dm.as_list()[0][2] == 8.0  # [lat, lon, depth]


def test_depth_map_ignores_zero_depth():
    dm = DepthMap(min_distance_m=0.0)
    dm.record(GeoPoint(59.66, 13.32), 0.0)
    assert dm.points == []


def test_depth_map_persists(tmp_path):
    path = str(tmp_path / "dm.json")
    dm = DepthMap(min_distance_m=0.0)
    dm.record(GeoPoint(59.66, 13.32), 8.0)
    dm.record(GeoPoint(59.67, 13.33), 9.5)
    dm.save(path)
    dm2 = DepthMap()
    dm2.load(path)
    assert len(dm2.points) == 2 and dm2.points[1][2] == 9.5


# ---- contours_in / composition_in: limit and truncation detection --------

def test_contours_in_respects_limit():
    """contours_in caps the returned list at the given limit."""
    dm = DepthMap()
    dm.contours = [{"d": float(i), "pts": [[59.0 + i * 0.001, 18.0]]} for i in range(20)]
    assert len(dm.contours_in(limit=5)) == 5
    assert len(dm.contours_in(limit=100)) == 20   # fewer than cap → all returned


def test_contours_in_truncation_pattern():
    """The server computes truncated = (len(result) == limit); verify the boundary."""
    dm = DepthMap()
    dm.contours = [{"d": float(i), "pts": [[59.0, 18.0 + i * 0.001]]} for i in range(10)]
    assert len(dm.contours_in(limit=10)) == 10    # at limit → truncated
    assert len(dm.contours_in(limit=11)) == 10    # below limit → not truncated


def test_composition_in_respects_limit():
    """composition_in caps the returned list at the given limit."""
    dm = DepthMap()
    dm.composition = [
        {"pct": float(i % 100), "ring": [[59.0, 18.0], [59.001, 18.001], [59.001, 18.0]]}
        for i in range(15)
    ]
    assert len(dm.composition_in(limit=4)) == 4
    assert len(dm.composition_in(limit=100)) == 15   # fewer than cap → all returned


def test_composition_in_truncation_pattern():
    """The server computes truncated = (len(result) == limit); verify the boundary."""
    dm = DepthMap()
    dm.composition = [
        {"pct": float(i % 100), "ring": [[59.0, 18.0], [59.001, 18.001], [59.001, 18.0]]}
        for i in range(8)
    ]
    assert len(dm.composition_in(limit=8)) == 8    # at limit → truncated
    assert len(dm.composition_in(limit=9)) == 8    # below limit → not truncated


# ---- columnar static-chart store: NPZ persistence, migration, windowing -----

import json as _json
import os as _os

import numpy as _np

from vanchor.nav.depth import ColumnarFeatures, _FeatureBuilder, parse_depth_features

_SAMPLE_FC = _json.dumps({"type": "FeatureCollection", "features": [
    {"geometry": {"type": "Point", "coordinates": [18.0, 59.0]},
     "properties": {"depth_m": 12.0, "hardness": 108}},
    {"geometry": {"type": "LineString",
                  "coordinates": [[18.0, 59.0], [18.0, 59.01], [18.01, 59.02]]},
     "properties": {"depth_m": 15.0}},
    {"geometry": {"type": "LineString",
                  "coordinates": [[20.0, 60.0], [20.01, 60.0]]},
     "properties": {"depth_m": 7.0}},
    {"geometry": {"type": "Polygon",
                  "coordinates": [[[18.0, 59.0], [18.0, 59.01], [18.01, 59.01], [18.0, 59.0]]]},
     "properties": {"composition_pct": 75.0}},
]}).encode()


def _flat(pairs):
    return [x for pt in pairs for x in pt]


def _seed_chart(dm):
    """Load the sample chart's layers into ``dm`` via the import parser."""
    parsed = parse_depth_features("chart.geojson", _SAMPLE_FC)
    dm.hardness = list(parsed["hardness"])
    dm.contours = parsed["contours"]
    dm.composition = parsed["composition"]
    return dm


def test_npz_roundtrip_equals_source(tmp_path):
    """save_chart -> load reproduces the accessor output byte-for-byte (within
    float32 precision) and the same feature counts."""
    src = _seed_chart(DepthMap())
    chart_json = str(tmp_path / "depthchart.json")     # save_chart derives .npz
    src.save_chart(chart_json)
    assert _os.path.exists(str(tmp_path / "depthchart.npz"))

    dst = DepthMap()
    dst.load(str(tmp_path / "missing_soundings.json"), chart_json)

    assert len(dst.contours) == len(src.contours) == 2
    assert len(dst.composition) == len(src.composition) == 1
    assert len(dst.hardness) == len(src.hardness) == 1
    # Accessor output matches (materialised dicts) to float32 tolerance.
    for a, b in zip(dst.contours_in(), src.contours_in()):
        assert a["d"] == pytest.approx(b["d"])
        assert _flat(a["pts"]) == pytest.approx(_flat(b["pts"]), abs=1e-4)
    for a, b in zip(dst.composition_in(), src.composition_in()):
        assert a["pct"] == pytest.approx(b["pct"])
        assert _flat(a["ring"]) == pytest.approx(_flat(b["ring"]), abs=1e-4)
    assert dst.hardness[0][2] == pytest.approx(src.hardness[0][2])


def test_legacy_json_migration_matches_and_renames(tmp_path):
    """A legacy depthchart.json migrates to .npz, produces identical accessor
    output, and the original JSON is renamed aside (never deleted)."""
    src = _seed_chart(DepthMap())
    # Write a legacy-format JSON chart (the old whole-file shape).
    legacy = {
        "hardness": [list(h) for h in src.hardness],
        "contours": list(src.contours),        # materialises {d, pts} dicts
        "composition": list(src.composition),  # materialises {pct, ring} dicts
    }
    chart_json = str(tmp_path / "depthchart.json")
    with open(chart_json, "w") as fh:
        _json.dump(legacy, fh)

    dm = DepthMap()
    dm.load(str(tmp_path / "missing.json"), chart_json)

    # Migration side effects: .npz written, .json renamed to .json.migrated.
    assert _os.path.exists(str(tmp_path / "depthchart.npz"))
    assert _os.path.exists(chart_json + ".migrated")
    assert not _os.path.exists(chart_json)

    assert isinstance(dm.contours, ColumnarFeatures)
    assert len(dm.contours) == 2 and len(dm.composition) == 1 and len(dm.hardness) == 1
    for a, b in zip(dm.contours_in(), src.contours_in()):
        assert a["d"] == pytest.approx(b["d"])
        assert _flat(a["pts"]) == pytest.approx(_flat(b["pts"]), abs=1e-4)

    # A subsequent load prefers the .npz (JSON is gone) and yields the same data.
    dm2 = DepthMap()
    dm2.load(str(tmp_path / "missing.json"), chart_json)
    assert len(dm2.contours) == 2 and len(dm2.composition) == 1


def test_bbox_window_matches_bruteforce():
    """Vectorised bbox windowing == an independent brute-force bbox-intersection
    over a small fixture (each feature carries a unique val for identity)."""
    feats = [
        {"pct": 1.0, "ring": [[59.0, 18.0], [59.0, 18.02], [59.02, 18.02]]},   # SW
        {"pct": 2.0, "ring": [[60.0, 20.0], [60.0, 20.02], [60.02, 20.0]]},    # NE, far
        {"pct": 3.0, "ring": [[59.5, 18.5], [59.5, 18.6], [59.6, 18.6]]},      # middle
        {"pct": 4.0, "ring": [[59.0, 18.0], [60.0, 20.0]]},                    # spans both
    ]
    cf = ColumnarFeatures.from_arrays(*_flatten(feats, "pct", "ring"), "pct", "ring")
    for bbox in [(17.9, 58.9, 18.1, 59.1), (19.9, 59.9, 20.1, 60.1),
                 (18.4, 59.4, 18.7, 59.7), (0.0, 0.0, 1.0, 1.0)]:
        w, s, e, n = bbox
        expect = set()
        for f in feats:
            las = [p[0] for p in f["ring"]]
            los = [p[1] for p in f["ring"]]
            if max(las) >= s and min(las) <= n and max(los) >= w and min(los) <= e:
                expect.add(round(f["pct"], 1))
        got = {round(d["pct"], 1) for d in cf.window(bbox, limit=100)}
        assert got == expect, bbox


def _flatten(feats, val_key, vtx_key):
    b = _FeatureBuilder(val_key, vtx_key)
    for f in feats:
        b.add(f[val_key], [(p[0], p[1]) for p in f[vtx_key]])
    built = b.build()
    return built.coords, built.offsets, built.vals


def test_columnar_store_is_compact():
    """Memory guard: a synthetic 100k-feature / 1M-vertex layer must pack into
    well under 60 MB columnar (the old list-of-lists shape was ~1.7 GB for the
    real ~10M-vertex chart)."""
    nverts, nfeat = 1_000_000, 100_000
    coords = _np.random.default_rng(0).random((nverts, 2)).astype(_np.float32)
    offsets = _np.arange(0, nverts + 1, nverts // nfeat, dtype=_np.int64)
    vals = _np.arange(len(offsets) - 1, dtype=_np.float32)
    cf = ColumnarFeatures.from_arrays(coords, offsets, vals, "pct", "ring")
    assert len(cf) == nfeat
    assert cf.nbytes < 60 * 1024 * 1024, f"{cf.nbytes} bytes"
