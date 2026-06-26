"""Tests for server-side gridding of the depth map (DepthMap.as_grid +
the /api/depth/grid endpoint)."""

import math

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.core.geo import destination_point
from vanchor.core.models import GeoPoint
from vanchor.nav.depth import DepthMap
from vanchor.ui.server import create_app


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


# -- API endpoint ------------------------------------------------------------ #
@pytest.fixture()
def client():
    app = create_app(Runtime())
    with TestClient(app) as c:
        yield c


def test_depth_grid_endpoint_shape(client):
    r = client.get("/api/depth/grid?cell_m=20")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert set(data) >= {"ok", "cell_m", "min_depth", "max_depth", "count", "cells"}
    assert isinstance(data["cells"], list)


def test_depth_grid_endpoint_clamps_cell_m(client):
    # Below the 2 m floor and above the 200 m ceiling get clamped.
    assert client.get("/api/depth/grid?cell_m=0.5").json()["cell_m"] == 2.0
    assert client.get("/api/depth/grid?cell_m=9999").json()["cell_m"] == 200.0
