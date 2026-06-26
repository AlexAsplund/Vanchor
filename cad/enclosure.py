"""Parametric electronics enclosure for the Vanchor-NG controller.

A splash-resistant box + lid to house the Raspberry Pi + Arduino + wiring on the
boat. Code-based (CadQuery / OpenCASCADE) so every dimension is a parameter --
change the constants and re-run to regenerate STEP (for editing in any CAD tool)
and STL (for printing).

    python cad/enclosure.py            # writes cad/out/*.step and *.stl

Defaults fit a Raspberry Pi 4 (85x56) and an Arduino Nano side by side.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import cadquery as cq


@dataclass
class Params:
    inner_l: float = 120.0   # internal length (mm)
    inner_w: float = 82.0    # internal width
    inner_h: float = 40.0    # internal height (base cavity)
    wall: float = 2.4        # wall thickness
    floor: float = 2.4       # floor thickness
    lid_t: float = 2.4       # lid plate thickness
    lip_h: float = 4.0       # lid lip that nests into the base
    lip_clear: float = 0.3   # clearance so the lid actually fits
    fillet: float = 3.0      # outer vertical edge fillet
    boss_r: float = 4.0      # lid screw boss radius
    screw_tap: float = 2.5   # hole dia for a self-tapping M3
    screw_free: float = 3.4  # clearance hole dia in the lid
    gasket_w: float = 2.0    # gasket groove width on the rim
    gasket_d: float = 1.2    # gasket groove depth
    # Raspberry Pi 4 mounting standoffs.
    pi_hole_dx: float = 58.0
    pi_hole_dy: float = 49.0
    standoff_h: float = 4.0
    standoff_r: float = 3.2
    standoff_hole: float = 2.7  # M2.5 self-tap
    # Cable slot in one end wall.
    cable_w: float = 22.0
    cable_h: float = 10.0


def _boss_xy(p: Params) -> list[tuple[float, float]]:
    inset = p.wall + p.boss_r + 0.5
    x = p.inner_l / 2 - (inset - p.wall)
    y = p.inner_w / 2 - (inset - p.wall)
    return [(x, y), (-x, y), (x, -y), (-x, -y)]


def build_base(p: Params) -> cq.Workplane:
    ol, ow = p.inner_l + 2 * p.wall, p.inner_w + 2 * p.wall
    oh = p.inner_h + p.floor
    base = (
        cq.Workplane("XY")
        .box(ol, ow, oh, centered=(True, True, False))
        .edges("|Z")
        .fillet(p.fillet)
        .faces(">Z")
        .shell(-p.wall)
    )
    floor_z = p.floor

    # Lid screw bosses rising from the floor to the rim.
    for (x, y) in _boss_xy(p):
        base = (
            base.faces("<Z").workplane(offset=oh, invert=True)
            .moveTo(x, y)
            .circle(p.boss_r)
            .extrude(-(oh - p.floor))
        )
    # Tap holes down the bosses.
    base = (
        base.faces(">Z").workplane()
        .pushPoints(_boss_xy(p))
        .hole(p.screw_tap, depth=p.inner_h - 1.0)
    )

    # Raspberry Pi standoffs on the floor.
    pts = [
        (p.pi_hole_dx / 2, p.pi_hole_dy / 2),
        (-p.pi_hole_dx / 2, p.pi_hole_dy / 2),
        (p.pi_hole_dx / 2, -p.pi_hole_dy / 2),
        (-p.pi_hole_dx / 2, -p.pi_hole_dy / 2),
    ]
    base = (
        base.faces("<Z[-1]").workplane(offset=-floor_z, invert=False)
        .pushPoints(pts).circle(p.standoff_r).extrude(p.standoff_h)
    )
    base = (
        base.faces(">Z").workplane(invert=True)
        .pushPoints(pts).hole(p.standoff_hole, depth=p.standoff_h)
    )

    # Gasket groove around the top rim.
    rim = (
        cq.Workplane("XY").workplane(offset=oh)
        .rect(ol - p.wall, ow - p.wall)
        .rect(ol - p.wall - 2 * p.gasket_w, ow - p.wall - 2 * p.gasket_w)
        .extrude(-p.gasket_d)
    )
    base = base.cut(rim)

    # Cable slot in the +X end wall.
    slot = (
        cq.Workplane("YZ").workplane(offset=ol / 2 - p.wall - 1)
        .center(0, p.floor + p.cable_h / 2)
        .rect(p.cable_w, p.cable_h)
        .extrude(p.wall + 2)
    )
    base = base.cut(slot)
    return base


def build_lid(p: Params) -> cq.Workplane:
    ol, ow = p.inner_l + 2 * p.wall, p.inner_w + 2 * p.wall
    lid = (
        cq.Workplane("XY")
        .box(ol, ow, p.lid_t, centered=(True, True, False))
        .edges("|Z")
        .fillet(p.fillet)
    )
    # Nesting lip on the underside.
    lip = (
        cq.Workplane("XY").workplane(offset=-p.lip_h)
        .rect(p.inner_l - p.lip_clear, p.inner_w - p.lip_clear)
        .rect(p.inner_l - p.lip_clear - 2 * p.wall, p.inner_w - p.lip_clear - 2 * p.wall)
        .extrude(p.lip_h)
    )
    lid = lid.union(lip)
    # Countersunk clearance holes for the bosses.
    lid = (
        lid.faces(">Z").workplane()
        .pushPoints(_boss_xy(p))
        .cskHole(p.screw_free, p.screw_free + 2.4, 82)
    )
    return lid


def main() -> None:
    p = Params()
    out = os.path.join(os.path.dirname(__file__), "out")
    os.makedirs(out, exist_ok=True)
    parts = {"enclosure_base": build_base(p), "enclosure_lid": build_lid(p)}
    for name, wp in parts.items():
        cq.exporters.export(wp, os.path.join(out, f"{name}.step"))
        cq.exporters.export(wp, os.path.join(out, f"{name}.stl"))
        print(f"wrote {name}.step + .stl")


if __name__ == "__main__":
    main()
