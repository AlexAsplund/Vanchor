"""Tests for server-side gridding of the depth map (DepthMap.as_grid +
the /api/depth/grid endpoint)."""

import math

import pytest

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.core.geo import destination_point
from vanchor.core.models import GeoPoint
from vanchor.nav.depth import DepthMap


def _dm(points):
    """Build a DepthMap with the given (lat, lon, depth) points, no thinning."""
    dm = DepthMap(min_distance_m=0.0)
    dm.points = list(points)
    return dm


def test_empty_map_returns_empty_grid_cleanly():
    grid = DepthMap().as_grid(15.0)
    assert grid["cells"] == []
    assert grid["count"] == 0
    assert grid["cell_m"] == 15.0
    assert grid["min_depth"] == 0.0 and grid["max_depth"] == 0.0


def test_same_cell_soundings_average_together():
    p = GeoPoint(59.66275, 13.32247)
    # Three soundings within ~3 m of each other: one cell, averaged depth.
    pts = [
        (p.lat, p.lon, 6.0),
        (destination_point(p, 2.0, 90.0).lat, destination_point(p, 2.0, 90.0).lon, 8.0),
        (destination_point(p, 1.0, 0.0).lat, destination_point(p, 1.0, 0.0).lon, 10.0),
    ]
    grid = _dm(pts).as_grid(20.0, radiate=False)
    assert len(grid["cells"]) == 1
    cell = grid["cells"][0]
    assert cell["n"] == 3
    assert cell["depth"] == pytest.approx((6.0 + 8.0 + 10.0) / 3.0)
    assert grid["count"] == 3


def test_cells_far_apart_stay_distinct():
    p = GeoPoint(59.66275, 13.32247)
    far = destination_point(p, 100.0, 90.0)  # 100 m east, well over cell size
    grid = _dm([(p.lat, p.lon, 5.0), (far.lat, far.lon, 9.0)]).as_grid(15.0, radiate=False)
    assert len(grid["cells"]) == 2
    depths = sorted(c["depth"] for c in grid["cells"])
    assert depths == pytest.approx([5.0, 9.0])


def test_min_max_count_correct():
    p = GeoPoint(59.66275, 13.32247)
    pts = []
    for k, depth in enumerate((3.0, 7.0, 11.0, 4.5)):
        q = destination_point(p, 100.0 * k, 90.0)  # spread far apart -> own cells
        pts.append((q.lat, q.lon, depth))
    grid = _dm(pts).as_grid(15.0, radiate=False)
    assert grid["count"] == 4
    assert len(grid["cells"]) == 4
    assert grid["min_depth"] == pytest.approx(3.0)
    assert grid["max_depth"] == pytest.approx(11.0)


def test_cell_cap_raises_effective_cell_size_on_huge_spread():
    p = GeoPoint(59.66275, 13.32247)
    # 400 soundings each ~50 m apart along a line -> ~400 distinct 15 m cells.
    pts = [
        (q.lat, q.lon, 5.0 + (k % 5))
        for k in range(400)
        for q in [destination_point(p, 50.0 * k, 90.0)]
    ]
    grid = _dm(pts).as_grid(15.0, max_cells=50)
    assert len(grid["cells"]) <= 50
    # The reported cell size must have grown above the requested 15 m.
    assert grid["cell_m"] > 15.0
    assert grid["count"] == 400


def test_grid_is_o_n_single_pass_consistency():
    # A point exactly on a cell boundary still lands in exactly one cell.
    p = GeoPoint(0.0, 0.0)  # equator: simple metres-per-degree
    pts = [(p.lat, p.lon, 4.0), (p.lat, p.lon, 4.0)]
    grid = _dm(pts).as_grid(10.0, radiate=False)
    assert len(grid["cells"]) == 1 and grid["cells"][0]["n"] == 2


def test_as_grid_bbox_windows_soundings():
    # Four soundings; a bbox that contains only the lower-left one.
    pts = [(59.0, 18.0, 5.0), (59.0, 18.01, 6.0), (59.02, 18.0, 7.0), (59.02, 18.01, 8.0)]
    dm = _dm(pts)
    full = dm.as_grid(20.0, radiate=False, interpolate=False)
    win = dm.as_grid(20.0, radiate=False, interpolate=False,
                     bbox=(17.99, 58.99, 18.005, 59.01))  # (west, south, east, north)
    assert len(win["cells"]) < len(full["cells"])
    assert all(c["lat"] <= 59.01 and c["lon"] <= 18.005 for c in win["cells"])
    # A window over empty water yields no cells.
    assert dm.as_grid(20.0, bbox=(10.0, 50.0, 10.1, 50.1))["cells"] == []


# -- runtime.depth_grid (the /api/depth/grid route is thin glue over this) ---- #
# We exercise runtime.depth_grid() directly rather than through TestClient: a
# Runtime carrying depth data spins under the TestClient lifespan portal (the
# real uvicorn server with the same data starts fine), and depth_grid() returns
# exactly what the endpoint serves -- clamping, windowing and all.
def _rt(tmp_path, points):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)  # isolate from the repo's vanchor_data/
    rt = Runtime(cfg)
    rt.depth_map.points = list(points)
    return rt


def test_depth_grid_shape_and_ok(tmp_path):
    rt = _rt(tmp_path, [(59.0, 18.0, 5.0), (59.00008, 18.0, 6.0), (59.0, 18.00008, 5.5)])
    g = rt.depth_grid(20.0)
    assert g["ok"] is True
    assert set(g) >= {"ok", "cell_m", "min_depth", "max_depth", "count", "cells"}
    assert isinstance(g["cells"], list)


def test_depth_grid_clamps_cell_m(tmp_path):
    rt = _rt(tmp_path, [(59.0, 18.0, 5.0)])
    assert rt.depth_grid(0.5)["cell_m"] == 2.0      # below the 2 m floor
    assert rt.depth_grid(9999)["cell_m"] == 200.0   # above the 200 m ceiling


def test_depth_grid_windows_to_bbox(tmp_path):
    rt = _rt(tmp_path, [(59.0, 18.0, 5.0), (59.001, 18.0, 6.0)])
    # Tier-1: a far-away viewport window yields an empty (but ok) grid.
    assert rt.depth_grid(10.0, bbox=(10.0, 50.0, 10.1, 50.1))["cells"] == []
    # A window over the seeded soundings (~59, 18) returns cells.
    near = rt.depth_grid(10.0, bbox=(17.9, 58.9, 18.1, 59.1))
    assert near["ok"] is True and len(near["cells"]) >= 1


def test_as_grid_sparse_wide_data_is_bounded():
    # Soundings spread sparsely across a wide area: the old bounding-box scan in
    # the interpolate/radiate fills made as_grid O(bbox_area * radius^2) and
    # stalled for tens of seconds. It must stay bounded by max_cells and return
    # promptly (a regression would hang this test).
    pts = [(59.0 + (k % 50) * 0.002, 18.0 + (k // 50) * 0.002, 5.0 + k % 7) for k in range(500)]
    g = _dm(pts).as_grid(5.0)
    assert len(g["cells"]) <= 3000


def test_as_grid_source_grids_an_alternate_layer():
    # source= grids a parallel (lat, lon, value) layer (e.g. hardness) with the
    # same binning -- cell values come from that layer, not depth.
    dm = _dm([(59.0, 18.0, 5.0)])  # depth layer (ignored when source is hardness)
    dm.hardness = [(59.0, 18.0, 108.0), (59.001, 18.0, 112.0)]
    g = dm.as_grid(20.0, source=dm.hardness, radiate=False, interpolate=False)
    assert sorted(c["depth"] for c in g["cells"]) == pytest.approx([108.0, 112.0])


def test_depth_grid_field_selects_hardness(tmp_path):
    rt = _rt(tmp_path, [(59.0, 18.0, 5.0)])
    rt.depth_map.hardness = [(59.0, 18.0, 108.0), (59.001, 18.0, 112.0)]
    g = rt.depth_grid(20.0, field="hardness")
    assert g["field"] == "hardness" and g["max_depth"] == pytest.approx(112.0)
    assert rt.depth_grid(20.0, field="depth")["field"] == "depth"  # default unaffected
