"""Render the steering gearbox to PNG 'print screens' (headless)."""

from __future__ import annotations

import os

from build123d import Pos, export_stl

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

    # Per-part previews.
    for name, obj in parts.items():
        stl = os.path.join(OUT, f"{name}.stl")
        render(stl, os.path.join(IMG, f"part_{name}.png"),
               color=COLORS[name], title=name.replace("_", " "))
        print("rendered", name)

    # Assembled, colored. Export each part at its assembled pose, then render
    # all together so the layers read clearly.
    poses = {
        "housing_bottom": Pos(0, 0, 0),
        "housing_top": Pos(0, 0, p.cavity_h + p.floor),
        "turret_holder": Pos(0, 0, p.floor),
        "ring_gear": Pos(0, 0, p.floor + p.bearing_w + 1),
        "pinion": Pos(p.center_dist, 0, p.floor + p.bearing_w + 1),
    }
    layered = []
    for name, obj in parts.items():
        pp = os.path.join(OUT, f"_pose_{name}.stl")
        export_stl(poses[name] * obj, pp)
        layered.append((pp, COLORS[name]))
    render(layered, os.path.join(IMG, "assembly.png"),
           views=[(24, -60), (24, 60), (88, -90)],
           title="Steering gearbox — assembled")
    print("rendered assembly")

    # An exploded view (lift the upper parts) for clarity.
    exploded = {
        "housing_bottom": Pos(0, 0, 0),
        "turret_holder": Pos(0, 0, p.floor + 30),
        "ring_gear": Pos(0, 0, p.floor + p.bearing_w + 1 + 70),
        "pinion": Pos(p.center_dist, 0, p.floor + p.bearing_w + 1 + 70),
        "housing_top": Pos(0, 0, p.cavity_h + p.floor + 120),
    }
    layered = []
    for name in ["housing_bottom", "turret_holder", "ring_gear", "pinion", "housing_top"]:
        pp = os.path.join(OUT, f"_exp_{name}.stl")
        export_stl(exploded[name] * parts[name], pp)
        layered.append((pp, COLORS[name]))
    render(layered, os.path.join(IMG, "exploded.png"),
           views=[(18, -62)], title="Steering gearbox — exploded")
    print("rendered exploded")


if __name__ == "__main__":
    main()
