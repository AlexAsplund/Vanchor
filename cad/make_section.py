"""Cross-section ('cut-away') render of the steering gearbox.

Cuts every part with the y=0 plane (which passes through the turret axis AND the
pinion axis) and renders the kept half, revealing the socket bore, bearings,
gear mesh, motor-mount bore, encoder standoffs and seals.
"""

from __future__ import annotations

import os

from build123d import Box, Pos, export_stl

import steering as S
from render import render

OUT = os.path.join(os.path.dirname(__file__), "out", "steering")
IMG = os.path.join(os.path.dirname(__file__), "out", "renders")
os.makedirs(IMG, exist_ok=True)

COLORS = {
    "housing_bottom": "#8a8f98",
    "housing_top": "#b8bdc6",
    "turret_holder": "#e08a2b",
    "ring_gear": "#2f7fd0",
    "pinion": "#2faa6a",
}


def main() -> None:
    p = S.P()
    parts = {
        "pinion": S.build_pinion(p),
        "ring_gear": S.build_ring_gear(p),
        "turret_holder": S.build_turret(p),
        "housing_bottom": S.build_housing_bottom(p),
        "housing_top": S.build_housing_top(p),
    }
    poses = {
        "housing_bottom": Pos(0, 0, 0),
        "housing_top": Pos(0, 0, p.cavity_h + p.floor),
        "turret_holder": Pos(0, 0, p.floor),
        "ring_gear": Pos(0, 0, p.floor + p.bearing_w + 1),
        "pinion": Pos(p.center_dist, 0, p.floor + p.bearing_w + 1),
    }
    # Half-space that removes everything with y > 0.
    big = 500.0
    cutter = Pos(0, big / 2, 0) * Box(big, big, big)

    layered = []
    for name, obj in parts.items():
        half = (poses[name] * obj) - cutter
        pp = os.path.join(OUT, f"_sec_{name}.stl")
        export_stl(half, pp)
        layered.append((pp, COLORS[name]))

    render(
        layered,
        os.path.join(IMG, "section.png"),
        views=[(12, 88), (16, 60), (3, 90)],
        title="Steering gearbox — cross-section (cut at centre plane)",
    )
    for f in os.listdir(OUT):
        if f.startswith("_sec_"):
            os.remove(os.path.join(OUT, f))
    print("wrote out/renders/section.png")


if __name__ == "__main__":
    main()
