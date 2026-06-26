"""Depth-map recorder: accumulate (position, depth) soundings as the boat moves.

This is the data behind the toggleable depth-map overlay -- a breadcrumb of
soundings that builds up automatically. (Interpolating a continuous contour
surface from these points is a future enhancement.)
"""

from __future__ import annotations

import json
import logging
import math
import os

from ..core.geo import haversine_m
from ..core.models import GeoPoint

# Metres per degree of latitude (≈ constant); longitude is scaled by cos(lat).
_M_PER_DEG_LAT = 111_320.0

logger = logging.getLogger("vanchor.depth")


class DepthMap:
    def __init__(self, min_distance_m: float = 3.0, max_points: int = 1500) -> None:
        self.min_distance_m = min_distance_m
        self.max_points = max_points
        # Each point is (lat, lon, depth_m).
        self.points: list[tuple[float, float, float]] = []
        self._last: GeoPoint | None = None

    # -- persistence (soundings survive restarts) ------------------------- #
    def save(self, path: str) -> None:
        if not self.points:
            return
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"points": self.points}, fh)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("could not save depth map: %s", exc)

    def load(self, path: str) -> None:
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                pts = json.load(fh).get("points", [])
            self.points = [tuple(p) for p in pts if len(p) == 3][-self.max_points :]
            if self.points:
                la, lo, _ = self.points[-1]
                self._last = GeoPoint(la, lo)
            logger.info("loaded %d depth soundings from %s", len(self.points), path)
        except (OSError, ValueError, TypeError) as exc:  # pragma: no cover
            logger.warning("could not load depth map: %s", exc)

    def record(self, point: GeoPoint | None, depth_m: float) -> None:
        if point is None or depth_m <= 0.0:
            return
        if self._last is None or haversine_m(self._last, point) >= self.min_distance_m:
            self.points.append((point.lat, point.lon, depth_m))
            self._last = point
            if len(self.points) > self.max_points:
                self.points = self.points[-self.max_points :]

    def as_list(self, limit: int = 600) -> list[list[float]]:
        """Most recent soundings as [[lat, lon, depth], ...] for the UI."""
        return [[la, lo, d] for la, lo, d in self.points[-limit:]]

    def as_grid(
        self,
        cell_m: float = 15.0,
        max_cells: int = 3000,
        interpolate: bool = True,
        interp_radius: int = 5,
        interp_min_dirs: int = 6,
        interp_power: float = 2.0,
        radiate: bool = True,
        radiate_radius_m: float = 30.0,
    ) -> dict:
        """Bin every sounding into a square grid (~``cell_m`` metres) and average
        depth per cell, returning a compact structure for the UI to colour-scale.

        Soundings span a small area, so we bin in a local metric frame using a
        flat metres-per-degree conversion at the data's mean latitude (cheap and
        accurate over the breadcrumb's extent -- no pyproj needed). Binning is a
        single O(n) pass over the points.

        The returned cell count is capped at ``max_cells`` by growing the
        effective cell size (doubling until the bins fit), and the cell size
        actually used is reported back so the client can label its colour scale.

        Two fill passes spread the measured data into the empty cells around it,
        in order of confidence:

        * **Radiate (nearest-neighbour / Voronoi).** When ``radiate`` is on
          (default), each empty cell within ``radiate_radius_m`` metres of a
          measured cell is assigned the depth of the *nearest* measured cell --
          the bottom is assumed roughly constant out to the Voronoi boundary
          where a different reading becomes nearer. The radius is bounded (a
          few cells) so one ping can't paint a whole lake and can't bleed into a
          neighbouring waterbody across an empty gap wider than the radius.
          Radiated cells are confident assumptions: ``"kind": "radiated"`` and
          ``"est": false``.

        * **Interpolate (enclosed-gap IDW).** When ``interpolate`` is on
          (default), *enclosed* empty cells -- holes surrounded by measured data
          in at least ``interp_min_dirs`` of the 8 compass directions within
          ``interp_radius`` cells -- get an inverse-distance-weighted estimate
          blended from the *differing* readings around them. These are genuine
          guesses between differing soundings: ``"kind": "interp"`` and
          ``"est": true``. Interp takes priority over radiate on any cell that
          qualifies, since a blend between differing readings is more honest
          there than picking the single nearest one.

        Measured cells carry ``"kind": "measured"`` and ``"est": false``. The
        deep middle of a sparsely-edged lake and any separate, far-away cluster
        stay untouched by both passes.

        Returns ``{cell_m, min_depth, max_depth, count, cells}`` where each cell
        is ``{"lat", "lon", "depth", "n", "est", "kind"}`` at the cell centre.
        """
        cell_m = max(1.0, float(cell_m))
        if not self.points:
            return {
                "cell_m": cell_m,
                "min_depth": 0.0,
                "max_depth": 0.0,
                "count": 0,
                "cells": [],
            }

        lats = [la for la, _, _ in self.points]
        lons = [lo for _, lo, _ in self.points]
        mean_lat = sum(lats) / len(lats)
        m_per_deg_lat = _M_PER_DEG_LAT
        m_per_deg_lon = _M_PER_DEG_LAT * max(0.01, math.cos(math.radians(mean_lat)))

        # Reference corner so cell indices are small non-negative integers.
        lat0, lon0 = min(lats), min(lons)

        # Grow the cell size until the binned cell count fits under the cap. The
        # number of distinct cells only ever shrinks as the cell grows, so a few
        # doublings converge quickly.
        while True:
            dlat = cell_m / m_per_deg_lat
            dlon = cell_m / m_per_deg_lon
            # accumulator: (i, j) -> [sum_depth, count]
            acc: dict[tuple[int, int], list[float]] = {}
            for la, lo, d in self.points:
                i = int((la - lat0) / dlat)
                j = int((lo - lon0) / dlon)
                bucket = acc.get((i, j))
                if bucket is None:
                    acc[(i, j)] = [d, 1.0]
                else:
                    bucket[0] += d
                    bucket[1] += 1.0
            if len(acc) <= max_cells:
                break
            cell_m *= 2.0

        # Measured cells: (i, j) -> averaged depth.
        measured: dict[tuple[int, int], float] = {
            ij: sum_d / n for ij, (sum_d, n) in acc.items()
        }

        # -- interpolation pass: fill enclosed holes (uncertain guesses) ---- #
        # Estimated cells: (i, j) -> idw depth. Built only for empty cells that
        # are genuinely surrounded by *differing* measured data within
        # ``interp_radius``. These are the honest "between two readings" guesses.
        estimated: dict[tuple[int, int], float] = {}
        if interpolate and len(measured) >= 8 and len(measured) < max_cells:
            estimated = self._interpolate_holes(
                measured,
                radius=max(1, int(interp_radius)),
                min_dirs=max(1, min(8, int(interp_min_dirs))),
                power=float(interp_power),
                budget=max_cells - len(measured),
            )

        # -- radiate pass: nearest-neighbour (Voronoi) footprint ------------ #
        # Each empty cell within ``radiate_radius_m`` of a measured cell takes
        # the depth of the NEAREST measured cell. Bounded radius => one ping
        # paints only a few cells and can't cross an empty gap to a neighbouring
        # waterbody. Interp cells already claimed above take priority and are
        # skipped here. Remaining cell budget is shared after interp.
        radiated: dict[tuple[int, int], float] = {}
        radiate_cells = int(radiate_radius_m / cell_m)  # radius in cells
        budget = max_cells - len(measured) - len(estimated)
        if radiate and radiate_cells >= 1 and measured and budget > 0:
            radiated = self._radiate(
                measured,
                claimed=estimated,
                radius=radiate_cells,
                budget=budget,
            )

        cells: list[dict] = []
        min_depth = math.inf
        max_depth = -math.inf
        for (i, j), avg in measured.items():
            min_depth = min(min_depth, avg)
            max_depth = max(max_depth, avg)
            cells.append(
                {
                    "lat": lat0 + (i + 0.5) * dlat,
                    "lon": lon0 + (j + 0.5) * dlon,
                    "depth": avg,
                    "n": int(acc[(i, j)][1]),
                    "est": False,
                    "kind": "measured",
                }
            )
        for (i, j), avg in radiated.items():
            min_depth = min(min_depth, avg)
            max_depth = max(max_depth, avg)
            cells.append(
                {
                    "lat": lat0 + (i + 0.5) * dlat,
                    "lon": lon0 + (j + 0.5) * dlon,
                    "depth": avg,
                    "n": 0,
                    "est": False,
                    "kind": "radiated",
                }
            )
        for (i, j), avg in estimated.items():
            min_depth = min(min_depth, avg)
            max_depth = max(max_depth, avg)
            cells.append(
                {
                    "lat": lat0 + (i + 0.5) * dlat,
                    "lon": lon0 + (j + 0.5) * dlon,
                    "depth": avg,
                    "n": 0,
                    "est": True,
                    "kind": "interp",
                }
            )

        return {
            "cell_m": cell_m,
            "min_depth": min_depth,
            "max_depth": max_depth,
            "count": len(self.points),
            "cells": cells,
        }

    @staticmethod
    def _interpolate_holes(
        measured: dict[tuple[int, int], float],
        radius: int,
        min_dirs: int,
        power: float,
        budget: int,
    ) -> dict[tuple[int, int], float]:
        """Fill empty cells that are enclosed by measured cells via IDW.

        A cell counts as *enclosed* only when, scanning outward along the 8
        compass directions up to ``radius`` cells, a measured cell is met in at
        least ``min_dirs`` of those 8 directions. This is the guard that keeps
        us inside the data: the deep middle of a lake whose only soundings hug
        the shore fails the test (no measured cell within ``radius`` straight
        out), and an isolated cluster never reaches another cluster's cells.

        ``budget`` caps how many estimates we add so the total cell count stays
        under ``max_cells``; once exhausted we stop filling.
        """
        if budget <= 0:
            return {}

        # 8 compass directions as (di, dj).
        dirs = (
            (1, 0), (-1, 0), (0, 1), (0, -1),
            (1, 1), (1, -1), (-1, 1), (-1, -1),
        )

        # Candidate empty cells: the bounding box of measured cells, minus the
        # measured cells themselves. Bounded by the data extent so we never
        # consider cells out past the soundings.
        i_min = min(i for i, _ in measured)
        i_max = max(i for i, _ in measured)
        j_min = min(j for _, j in measured)
        j_max = max(j for _, j in measured)

        estimated: dict[tuple[int, int], float] = {}
        for i in range(i_min, i_max + 1):
            for j in range(j_min, j_max + 1):
                if (i, j) in measured:
                    continue
                # Enclosure test: count directions with a measured hit in range.
                hits = 0
                for di, dj in dirs:
                    for r in range(1, radius + 1):
                        if (i + di * r, j + dj * r) in measured:
                            hits += 1
                            break
                    if hits >= min_dirs:
                        break  # already enclosed; no need to scan the rest
                if hits < min_dirs:
                    continue

                # IDW over measured cells within the (square) radius window.
                wsum = 0.0
                dsum = 0.0
                for di in range(-radius, radius + 1):
                    for dj in range(-radius, radius + 1):
                        if di == 0 and dj == 0:
                            continue
                        d = measured.get((i + di, j + dj))
                        if d is None:
                            continue
                        dist2 = di * di + dj * dj
                        w = 1.0 / (dist2 ** (power / 2.0))
                        wsum += w
                        dsum += w * d
                if wsum > 0.0:
                    estimated[(i, j)] = dsum / wsum
                    if len(estimated) >= budget:
                        return estimated
        return estimated

    @staticmethod
    def _radiate(
        measured: dict[tuple[int, int], float],
        claimed: dict[tuple[int, int], float],
        radius: int,
        budget: int,
    ) -> dict[tuple[int, int], float]:
        """Fill empty cells with the depth of the NEAREST measured cell.

        This is a bounded nearest-neighbour (Voronoi) extension: every empty
        cell within ``radius`` cells of at least one measured cell is assigned
        the depth of the closest measured cell (Chebyshev-windowed, Euclidean
        tie-break). The bottom is assumed roughly the same as the nearest
        sounding out to the Voronoi boundary -- the locus where a *different*
        reading becomes nearer.

        The radius is the critical guard: a cell more than ``radius`` cells from
        every sounding is never filled, so a single ping paints only its own
        small neighbourhood (it can't flood a whole lake) and the fill can't
        jump an empty gap wider than ``radius`` into a neighbouring waterbody.

        ``claimed`` cells (already taken by the enclosed-gap interpolation) are
        skipped so interp's honest blend wins where readings differ. ``budget``
        caps how many cells we add to respect ``max_cells``.
        """
        if budget <= 0:
            return {}

        # Candidate window: the measured bounding box grown by ``radius`` so a
        # cell just outside the data can still be reached by a nearby sounding,
        # bounded so we never scan unboundedly.
        i_min = min(i for i, _ in measured) - radius
        i_max = max(i for i, _ in measured) + radius
        j_min = min(j for _, j in measured) - radius
        j_max = max(j for _, j in measured) + radius

        radiated: dict[tuple[int, int], float] = {}
        for i in range(i_min, i_max + 1):
            for j in range(j_min, j_max + 1):
                if (i, j) in measured or (i, j) in claimed:
                    continue
                # Nearest measured cell within the square ``radius`` window.
                best_d2 = math.inf
                best_depth = None
                for di in range(-radius, radius + 1):
                    for dj in range(-radius, radius + 1):
                        if di == 0 and dj == 0:
                            continue
                        depth = measured.get((i + di, j + dj))
                        if depth is None:
                            continue
                        d2 = di * di + dj * dj
                        if d2 < best_d2:
                            best_d2 = d2
                            best_depth = depth
                if best_depth is not None:
                    radiated[(i, j)] = best_depth
                    if len(radiated) >= budget:
                        return radiated
        return radiated
