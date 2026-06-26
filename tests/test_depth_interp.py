"""Tests for the enclosed-hole interpolation pass in DepthMap.as_grid.

These exercise the guards that keep interpolation *inside* the data:
- an enclosed hole gets filled (est=True, depth between the surroundings),
- the deep middle of a sparsely-edged lake stays empty,
- a separate far-away cluster is never filled from another cluster,
- the total cell count never exceeds max_cells.
"""

import math

import pytest

from vanchor.core.geo import destination_point
from vanchor.core.models import GeoPoint
from vanchor.nav.depth import DepthMap

ORIGIN = GeoPoint(59.66275, 13.32247)


def _dm(points):
    dm = DepthMap(min_distance_m=0.0)
    dm.points = list(points)
    return dm


def _cell_at(grid, lat, lon, tol_deg=1e-4):
    """Return the cell whose centre is closest to (lat, lon), or None."""
    best, bestd = None, math.inf
    for c in grid["cells"]:
        d = (c["lat"] - lat) ** 2 + (c["lon"] - lon) ** 2
        if d < bestd:
            best, bestd = c, d
    return best if bestd < tol_deg ** 2 else None


def _grid_points(cell_m, ncols, nrows, depth_fn, skip=None):
    """Build soundings on a regular lattice spaced ~cell_m apart.

    depth_fn(row, col) -> depth; skip is a set of (row, col) holes to omit.
    Each lattice node lands roughly in its own cell at the given cell size.
    """
    skip = skip or set()
    pts = []
    for r in range(nrows):
        for c in range(ncols):
            if (r, c) in skip:
                continue
            # east by c cells, north by r cells from the origin.
            east = destination_point(ORIGIN, cell_m * c, 90.0)
            node = destination_point(GeoPoint(east.lat, east.lon), cell_m * r, 0.0)
            pts.append((node.lat, node.lon, float(depth_fn(r, c))))
    return pts


def test_enclosed_hole_is_filled_with_estimate_between_neighbours():
    # 5x5 lattice of measured cells with the centre (2,2) missing -> enclosed.
    cell_m = 15.0
    pts = _grid_points(cell_m, 5, 5, depth_fn=lambda r, c: 10.0, skip={(2, 2)})
    # Isolate the interp pass; radiate would also fill the hole + surroundings.
    grid = _dm(pts).as_grid(cell_m, radiate=False)

    est = [c for c in grid["cells"] if c["est"]]
    assert len(est) == 1, "exactly the one enclosed hole should be filled"
    hole = est[0]
    assert hole["n"] == 0
    # Surrounded by depth 10 on all sides -> estimate ~10.
    assert hole["depth"] == pytest.approx(10.0)

    # Measured cells are flagged est=False.
    measured = [c for c in grid["cells"] if not c["est"]]
    assert len(measured) == 24
    assert all(c["n"] >= 1 for c in measured)


def test_estimate_is_between_surrounding_values():
    # Gradient: shallow on the west edge, deep on the east edge. The filled
    # hole should land strictly between the min and max of its neighbours.
    cell_m = 15.0
    pts = _grid_points(cell_m, 5, 5, depth_fn=lambda r, c: 2.0 + 3.0 * c, skip={(2, 2)})
    grid = _dm(pts).as_grid(cell_m)
    hole = next(c for c in grid["cells"] if c["est"])
    surround = [c["depth"] for c in grid["cells"] if not c["est"]]
    assert min(surround) < hole["depth"] < max(surround)


def test_sparse_perimeter_leaves_deep_interior_unfilled():
    # A large empty lake with soundings only on its rectangular perimeter.
    # Interior cells more than `radius` from any edge fail the enclosure test
    # (no measured cell straight out within R), so the middle stays empty.
    cell_m = 15.0
    span = 16  # 16x16 perimeter -> interior is far wider than radius (5)
    pts = []
    for k in range(span):
        for (r, c) in ((0, k), (span - 1, k), (k, 0), (k, span - 1)):
            east = destination_point(ORIGIN, cell_m * c, 90.0)
            node = destination_point(GeoPoint(east.lat, east.lon), cell_m * r, 0.0)
            pts.append((node.lat, node.lon, 8.0))
    grid = _dm(pts).as_grid(cell_m, interp_radius=5)

    est = [c for c in grid["cells"] if c["est"]]
    # The geometric centre of the lake must NOT be filled.
    mid_east = destination_point(ORIGIN, cell_m * (span / 2), 90.0)
    mid = destination_point(GeoPoint(mid_east.lat, mid_east.lon), cell_m * (span / 2), 0.0)
    centre = _cell_at(grid, mid.lat, mid.lon, tol_deg=cell_m / 111320.0)
    assert centre is None, "deep middle of a sparsely-edged lake must stay empty"
    # Whatever does get filled hugs the perimeter (a thin band), not the centre.
    assert len(est) < (span - 2) ** 2 // 2


def test_separate_far_cluster_is_never_filled_from_another():
    # Two enclosed rings far apart. Each fills its own hole; neither leaks
    # across the empty gulf between them.
    cell_m = 15.0
    a = _grid_points(cell_m, 5, 5, depth_fn=lambda r, c: 5.0, skip={(2, 2)})
    # Cluster B offset 2 km east (hundreds of cells away).
    bshift = destination_point(ORIGIN, 2000.0, 90.0)
    b_origin = GeoPoint(bshift.lat, bshift.lon)
    b = []
    for r in range(5):
        for c in range(5):
            if (r, c) == (2, 2):
                continue
            east = destination_point(b_origin, cell_m * c, 90.0)
            node = destination_point(GeoPoint(east.lat, east.lon), cell_m * r, 0.0)
            b.append((node.lat, node.lon, 20.0))

    grid = _dm(a + b).as_grid(cell_m)
    est = [c for c in grid["cells"] if c["est"]]
    # Exactly two holes filled (one per cluster); the empty gulf is untouched.
    assert len(est) == 2
    depths = sorted(c["depth"] for c in est)
    # Each estimate matches its own cluster's depth, never blended across.
    assert depths[0] == pytest.approx(5.0) and depths[1] == pytest.approx(20.0)


def test_total_cells_never_exceed_max_cells():
    # A grid of holes that would all fill, but max_cells is tight: the total
    # (measured + estimated) must stay within the cap.
    cell_m = 15.0
    # 10x10 lattice with every other interior cell missing -> many fillable holes.
    skip = {(r, c) for r in range(1, 9) for c in range(1, 9) if (r + c) % 2 == 0}
    pts = _grid_points(cell_m, 10, 10, depth_fn=lambda r, c: 7.0, skip=skip)
    measured_count = len(pts)
    cap = measured_count + 5
    grid = _dm(pts).as_grid(cell_m, max_cells=cap)
    assert len(grid["cells"]) <= cap


def test_interpolation_can_be_disabled():
    cell_m = 15.0
    pts = _grid_points(cell_m, 5, 5, depth_fn=lambda r, c: 10.0, skip={(2, 2)})
    grid = _dm(pts).as_grid(cell_m, interpolate=False)
    assert all(c["est"] is False for c in grid["cells"])
    assert all(not c["est"] for c in grid["cells"])
