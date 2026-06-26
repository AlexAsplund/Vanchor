"""Tests for the radiate (nearest-neighbour / Voronoi) fill in DepthMap.as_grid.

The radiate pass spreads each measured cell's depth into the empty cells within
a bounded radius, taking the depth of the NEAREST measured cell. These tests
pin down:

- a lone sounding radiates into its neighbourhood (a sounder footprint, not a dot),
- the Voronoi boundary: between two differing readings each empty cell takes the
  nearer one's depth,
- the radius guard stops one ping flooding a whole lake,
- the radius guard stops bleed across an empty gap into a neighbouring waterbody,
- cell taxonomy / classification flags (measured / radiated / interp, est),
- max_cells is respected with radiate on.
"""

import pytest

from vanchor.core.geo import destination_point
from vanchor.core.models import GeoPoint
from vanchor.nav.depth import DepthMap

ORIGIN = GeoPoint(59.66275, 13.32247)


def _dm(points):
    dm = DepthMap(min_distance_m=0.0)
    dm.points = list(points)
    return dm


def _kinds(grid):
    out = {"measured": [], "radiated": [], "interp": []}
    for c in grid["cells"]:
        out[c["kind"]].append(c)
    return out


def test_single_sounding_radiates_into_a_footprint():
    # One ping should paint more than its own cell: a bounded neighbourhood.
    grid = _dm([(ORIGIN.lat, ORIGIN.lon, 7.0)]).as_grid(
        15.0, radiate_radius_m=30.0
    )
    kinds = _kinds(grid)
    assert len(kinds["measured"]) == 1
    # radius = 30/15 = 2 cells -> a 5x5 block minus the centre = 24 radiated.
    assert len(kinds["radiated"]) == 24
    # All radiated cells inherit the single reading's depth, flagged confident.
    assert all(c["depth"] == pytest.approx(7.0) for c in kinds["radiated"])
    assert all(c["est"] is False and c["n"] == 0 for c in kinds["radiated"])


def test_radiate_radius_is_bounded_not_whole_lake():
    # A wider radius paints more, but always a bounded block -- never unbounded.
    grid_small = _dm([(ORIGIN.lat, ORIGIN.lon, 5.0)]).as_grid(
        10.0, radiate_radius_m=20.0
    )
    grid_big = _dm([(ORIGIN.lat, ORIGIN.lon, 5.0)]).as_grid(
        10.0, radiate_radius_m=40.0
    )
    # r=2 -> 5x5-1=24 ; r=4 -> 9x9-1=80. Bounded by the radius, both finite.
    assert len(_kinds(grid_small)["radiated"]) == 24
    assert len(_kinds(grid_big)["radiated"]) == 80


def test_voronoi_boundary_between_two_differing_readings():
    # Two readings 90 m apart (6 cells at 15 m). Empty cells between them take
    # the depth of the NEARER reading; the split lands at the midline.
    cell_m = 15.0
    a = (ORIGIN.lat, ORIGIN.lon, 3.0)
    far = destination_point(ORIGIN, 90.0, 90.0)  # 6 cells east
    b = (far.lat, far.lon, 9.0)
    # Big enough radius that the two footprints meet in the middle.
    grid = _dm([a, b]).as_grid(cell_m, radiate_radius_m=60.0)
    rad = _kinds(grid)["radiated"]
    # Every radiated cell carries exactly one of the two source depths
    # (nearest-neighbour, never a blend).
    assert {round(c["depth"], 3) for c in rad} <= {3.0, 9.0}
    # Cells nearer the west reading are shallow; nearer the east are deep.
    west = destination_point(ORIGIN, 15.0, 90.0)
    east = destination_point(ORIGIN, 75.0, 90.0)

    def nearest_cell(lat, lon):
        return min(rad, key=lambda c: (c["lat"] - lat) ** 2 + (c["lon"] - lon) ** 2)

    assert nearest_cell(west.lat, west.lon)["depth"] == pytest.approx(3.0)
    assert nearest_cell(east.lat, east.lon)["depth"] == pytest.approx(9.0)


def test_radius_guard_prevents_neighbour_waterbody_bleed():
    # Two readings separated by an empty gap wider than the radiate radius.
    # Neither footprint reaches the other -- no bleed across the gap.
    cell_m = 15.0
    a = (ORIGIN.lat, ORIGIN.lon, 4.0)
    # 200 m east -> ~13 cells; radius only 2 cells, so a clear gap remains.
    far = destination_point(ORIGIN, 200.0, 90.0)
    b = (far.lat, far.lon, 20.0)
    grid = _dm([a, b]).as_grid(cell_m, radiate_radius_m=30.0)
    rad = _kinds(grid)["radiated"]
    # Each footprint is a bounded 24-cell block; no cell carries a blend, and
    # the empty gulf between them stays unfilled.
    assert {round(c["depth"], 3) for c in rad} == {4.0, 20.0}
    assert len(rad) == 48  # 24 around each, no overlap


def test_radiate_does_not_overwrite_interp_holes():
    # An enclosed hole between differing readings is claimed by interp (est),
    # not by radiate -- the honest blend wins where readings differ.
    cell_m = 15.0
    pts = []
    for r in range(5):
        for c in range(5):
            if (r, c) == (2, 2):
                continue
            east = destination_point(ORIGIN, cell_m * c, 90.0)
            node = destination_point(GeoPoint(east.lat, east.lon), cell_m * r, 0.0)
            pts.append((node.lat, node.lon, 2.0 + 3.0 * c))  # west-east gradient
    grid = _dm(pts).as_grid(cell_m, radiate_radius_m=30.0)
    kinds = _kinds(grid)
    # The centre hole is interp (uncertain blend), flagged est=True.
    interp = kinds["interp"]
    assert len(interp) == 1
    assert interp[0]["est"] is True
    # It is a blend, strictly between the surrounding measured min and max.
    measured_depths = [c["depth"] for c in kinds["measured"]]
    assert min(measured_depths) < interp[0]["depth"] < max(measured_depths)


def test_classification_flags_are_consistent():
    grid = _dm([(ORIGIN.lat, ORIGIN.lon, 6.0)]).as_grid(15.0, radiate_radius_m=30.0)
    for c in grid["cells"]:
        assert c["kind"] in {"measured", "radiated", "interp"}
        if c["kind"] == "measured":
            assert c["n"] >= 1 and c["est"] is False
        elif c["kind"] == "radiated":
            assert c["n"] == 0 and c["est"] is False
        else:  # interp
            assert c["n"] == 0 and c["est"] is True


def test_max_cells_respected_with_radiate():
    # A single reading would radiate 24 cells, but a tight cap must stop it.
    cap = 1 + 5  # measured + only 5 radiated allowed
    grid = _dm([(ORIGIN.lat, ORIGIN.lon, 5.0)]).as_grid(
        15.0, max_cells=cap, radiate_radius_m=30.0
    )
    assert len(grid["cells"]) <= cap
    kinds = _kinds(grid)
    assert len(kinds["measured"]) == 1
    assert len(kinds["radiated"]) <= 5


def test_radiate_can_be_disabled():
    grid = _dm([(ORIGIN.lat, ORIGIN.lon, 5.0)]).as_grid(15.0, radiate=False)
    assert all(c["kind"] != "radiated" for c in grid["cells"])
    assert len(grid["cells"]) == 1
