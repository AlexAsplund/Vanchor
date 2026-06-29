#!/usr/bin/env python3
"""Generate a mock whole-lake depth map for the sim's starting lake (Visten).

Samples the sim's ``Bathymetry.depth_at`` over a grid masked to the lake water
polygon (``tests/data/water_sim.geojson``) and writes the soundings as
``vanchor_data/depthmap.json`` -- a pre-filled depth chart covering the entire
lake, replacing whatever sparse tracked soundings were there before.

    python scripts/mock_lake_depthmap.py [spacing_m]   # default 35 m
"""
from __future__ import annotations

import json
import math
import os
import sys

from shapely.geometry import Point, shape
from shapely.ops import unary_union
from shapely.prepared import prep

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from vanchor.core.models import GeoPoint  # noqa: E402
from vanchor.sim.bathymetry import Bathymetry  # noqa: E402

SPACING_M = float(sys.argv[1]) if len(sys.argv) > 1 else 35.0
GEOJSON = os.path.join(ROOT, "tests", "data", "water_sim.geojson")
OUT = os.path.join(ROOT, "vanchor_data", "depthmap.json")


def main() -> None:
    data = json.load(open(GEOJSON))
    feats = data["features"] if data.get("type") == "FeatureCollection" else [data]
    lake = unary_union([shape(f.get("geometry", f)) for f in feats])
    inside = prep(lake)
    minx, miny, maxx, maxy = lake.bounds  # lon, lat
    bath = Bathymetry()

    mid_lat = (miny + maxy) / 2.0
    dlat = SPACING_M / 111_320.0
    dlon = SPACING_M / (111_320.0 * math.cos(math.radians(mid_lat)))

    points = []
    lat = miny
    while lat <= maxy:
        lon = minx
        while lon <= maxx:
            if inside.contains(Point(lon, lat)):
                depth = bath.depth_at(GeoPoint(lat, lon))
                points.append([round(lat, 6), round(lon, 6), round(depth, 1)])
            lon += dlon
        lat += dlat

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"points": points}, fh)
    print(f"wrote {len(points)} soundings to {OUT} (spacing {SPACING_M:.0f} m)")


if __name__ == "__main__":
    main()
