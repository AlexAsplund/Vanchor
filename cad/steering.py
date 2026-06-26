"""Parametric 3D-printable steering / azimuth gearbox for the trolling motor (v2).

The motor's 1" shaft slides DOWN into a rotating socket -- the **turret** -- and a
top split-clamp grips it. A quiet 12 V brushed **worm gearmotor** (self-locking,
mounted UNDER the housing) drives a pinion into the turret's ring gear to swing
the motor for steering. An **AS5600 magnetic absolute encoder** reads a magnet in
the turret's solid base for closed-loop position feedback (+ cable-wrap limit).
The housing is sealed -- gasket + perimeter bolts, an O-ring rotary seal on the
turret, a cable gland, a weep + breather -- for splash/rain.

v2 incorporates a 10-pass design review (see cad/steering_REVIEW.md): real bearing
shoulders + thrust path, external motor mount, coordinated encoder air-gap, lid
bolting, drainage/breather, and print chamfers.

Cheap, reliable, off-the-shelf parts. Built with build123d.

    python cad/steering.py   # -> cad/out/steering/*.step + *.stl
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

from build123d import Box, Cylinder, Polygon, Pos, Rot, export_step, export_stl, extrude

C, S = math.cos, math.sin
TAU = 2 * math.pi


@dataclass
class P:
    module: float = 2.0            # bumped from 1.5: stronger printed teeth
    pinion_teeth: int = 15
    ring_teeth: int = 60           # 4:1 onto the turret
    gear_w: float = 12.0
    shaft_dia: float = 25.4        # 1" motor shaft
    bore_clear: float = 0.4
    socket_depth: float = 60.0
    # 6807-2RS bearings (35 x 47 x 7), one in the base, one in the lid.
    bearing_od: float = 47.0
    bearing_id: float = 35.0
    bearing_w: float = 7.0
    seat_chamfer: float = 0.8      # lead-in so press-fit prints cleanly
    turret_body_od: float = 44.0   # > bearing_id -> a REAL shoulder both ends
    clamp_od: float = 42.0
    clamp_h: float = 28.0
    clamp_bolt: float = 5.2        # M5
    # 12 V worm gearmotor (JGY-370 class), mounted UNDER the floor.
    motor_shaft: float = 6.2
    motor_pilot: float = 13.0
    motor_mount_pitch: float = 18.0
    motor_screw: float = 3.2
    motor_body_dia: float = 37.0
    # housing
    wall: float = 3.2
    floor: float = 4.0
    cavity_h: float = 40.0
    gasket_w: float = 2.2
    gasket_d: float = 1.4
    oring_w: float = 2.4
    oring_d: float = 1.6
    cable_gland: float = 12.5
    lid_bolt: float = 3.4          # M3 clearance in lid
    lid_tap: float = 2.6           # M3 self-tap into the bosses
    lid_boss_r: float = 4.6
    weep: float = 3.0              # drains the socket
    breather: float = 3.0          # Gore-vent port
    # AS5600 encoder
    enc_pocket: float = 14.0       # board recess (square) in the floor
    enc_depth: float = 4.0
    enc_gap: float = 1.5           # magnet face -> IC air gap
    enc_screw: float = 2.2
    magnet_dia: float = 6.4
    magnet_h: float = 3.0

    @property
    def pinion_pr(self): return self.module * self.pinion_teeth / 2
    @property
    def ring_pr(self): return self.module * self.ring_teeth / 2
    @property
    def center_dist(self): return self.pinion_pr + self.ring_pr
    @property
    def ring_outer(self): return self.ring_pr + self.module
    @property
    def pinion_outer(self): return self.pinion_pr + self.module
    @property
    def turret_h(self): return self.cavity_h + self.clamp_h   # journals + body in cavity, clamp above
    @property
    def housing_l(self):
        reach = max(self.ring_outer, self.center_dist + self.pinion_outer)
        return 2 * (reach + self.wall + 6)
    @property
    def housing_w(self): return 2 * (self.ring_outer + self.wall + 6)
    def lid_bolts(self):
        x = self.housing_l / 2 - self.wall - self.lid_boss_r
        y = self.housing_w / 2 - self.wall - self.lid_boss_r
        return [(sx * x, sy * y) for sx in (-1, 1) for sy in (-1, 1)]


def cyl(r, h, z0):
    return Pos(0, 0, z0 + h / 2) * Cylinder(r, h)


def tube(ro, ri, h, z0):
    return cyl(ro, h, z0) - cyl(ri, h + 0.2, z0 - 0.1)


def boxz(l, w, h, z0):
    return Pos(0, 0, z0 + h / 2) * Box(l, w, h)


def gear(m, z, width, bore):
    """Trapezoidal-tooth spur gear -- robust to print; order true involute later."""
    pr = m * z / 2
    outer, root = pr + m, max(bore / 2 + 3.0, pr - 1.25 * m)
    ang = TAU / z
    tip, rh = ang * 0.19, ang * 0.33
    pts = []
    for i in range(z):
        a = i * ang
        pts += [
            (root * C(a - rh), root * S(a - rh)),
            (outer * C(a - tip), outer * S(a - tip)),
            (outer * C(a + tip), outer * S(a + tip)),
            (root * C(a + rh), root * S(a + rh)),
        ]
    g = extrude(Polygon(*pts, align=None), amount=width)
    if bore > 0:
        g -= cyl(bore / 2, width * 3, -width)
    return g


# ---- parts ---------------------------------------------------------------- #
def build_pinion(p: P):
    g = gear(p.module, p.pinion_teeth, p.gear_w, 0)
    g += cyl(p.motor_shaft / 2 + 3.2, 8, -8)
    g -= cyl(p.motor_shaft / 2, p.gear_w + 8 + 2, -9)
    g -= boxz(p.motor_shaft, 1.2, p.gear_w + 8, -9)     # flat for the D-shaft
    return g


def build_ring_gear(p: P):
    g = gear(p.module, p.ring_teeth, p.gear_w, p.turret_body_od + 0.4)
    for i in range(6):                                   # 6-bolt circle to the body
        a = i * TAU / 6
        r = p.turret_body_od / 2 + 5
        g -= Pos(r * C(a), r * S(a)) * cyl(1.7, p.gear_w * 3, -p.gear_w)
    return g


def build_turret(p: P):
    jr = p.bearing_id / 2
    br = p.turret_body_od / 2
    cr = p.clamp_od / 2
    bore = (p.shaft_dia + p.bore_clear) / 2
    bw = p.bearing_w
    # z layout: lower journal (0..bw) | body (bw..cavity-bw) | upper journal (..cavity) | clamp
    body_z0, body_z1 = bw, p.cavity_h - bw
    total_h = p.turret_h

    t = cyl(jr, bw + 0.2, 0)                              # lower journal (in base bearing)
    t += cyl(br, body_z1 - body_z0, body_z0)             # body: shoulders both ends -> axial trap
    t += cyl(jr, bw, body_z1)                            # upper journal (in lid bearing)
    t += cyl(cr, p.clamp_h, p.cavity_h)                  # clamp section

    # O-ring rotary-seal groove on the upper journal.
    gz = body_z1 + 2
    t -= tube(jr + 0.1, jr - p.oring_d, p.oring_w, gz)

    # Blind socket from the top; solid base kept for the magnet.
    t -= cyl(bore, p.socket_depth, total_h - p.socket_depth)
    # Weep hole: drains the socket sideways near its floor.
    wz = total_h - p.socket_depth + 2
    t -= Pos(0, 0, wz) * Rot(0, 90, 0) * Cylinder(p.weep / 2, cr * 2 + 4)
    # Magnet pocket in the solid base (opens downward to the encoder).
    t -= cyl(p.magnet_dia / 2, p.magnet_h, 0)

    # Split clamp: slit through to the bore + cross bolt with a nut trap.
    cz = total_h - p.clamp_h / 2
    t -= boxz(cr * 2 + 2, 2.2, p.clamp_h, p.cavity_h)
    t -= Pos(0, 0, cz) * Rot(0, 90, 0) * Cylinder(p.clamp_bolt / 2, cr * 2 + 4)
    return t


def build_housing_bottom(p: P):
    L, W, H = p.housing_l, p.housing_w, p.cavity_h + p.floor
    body = boxz(L, W, H, 0) - boxz(L - 2 * p.wall, W - 2 * p.wall, p.cavity_h, p.floor)

    # Lower-bearing seat with a shoulder (outer race rests on it) + print chamfer.
    body -= cyl(p.bearing_od / 2, p.bearing_w, p.floor)
    body -= cyl(p.bearing_od / 2 + p.seat_chamfer, p.seat_chamfer, p.floor + p.bearing_w - p.seat_chamfer)
    body -= cyl(p.bearing_id / 2 + 1.5, p.floor + 1, -0.5)     # bore through to the encoder

    # External gearmotor mount UNDER the floor (body hangs below; shaft pokes up).
    cx = p.center_dist
    body += Pos(cx, 0, -8) * cyl(p.motor_pilot / 2 + 5, 8, 0)       # mount pad below floor
    body -= Pos(cx, 0, 0) * cyl((p.motor_pilot + 1) / 2, p.floor + 12, -9)  # shaft bore up into cavity
    for s in (-1, 1):
        body -= Pos(cx + s * p.motor_mount_pitch / 2, 0, -9) * cyl(p.motor_screw / 2, 10, 0)

    # AS5600 recess in the floor, set so the IC sits enc_gap below the magnet.
    body -= boxz(p.enc_pocket, p.enc_pocket, p.enc_depth, p.floor - p.enc_depth)
    for sx in (-1, 1):
        body -= Pos(sx * (p.enc_pocket / 2 - 2), 0, p.floor - p.enc_depth) * cyl(p.enc_screw / 2, p.enc_depth, 0)

    # Lid-bolt bosses (tapped) at the corners.
    for (x, y) in p.lid_bolts():
        body += Pos(x, y, p.floor) * cyl(p.lid_boss_r, p.cavity_h - p.gasket_d, 0)
        body -= Pos(x, y, 0) * cyl(p.lid_tap / 2, H, 0)

    # Gasket groove around the rim.
    body -= (
        boxz(L - p.wall, W - p.wall, p.gasket_d, H - p.gasket_d)
        - boxz(L - p.wall - 2 * p.gasket_w, W - p.wall - 2 * p.gasket_w, p.gasket_d + 1, H - p.gasket_d)
    )
    # Cable gland + breather in opposite walls.
    body -= Pos(-L / 2 + p.wall / 2, 0, p.floor + 9) * Rot(0, 90, 0) * Cylinder(p.cable_gland / 2, p.wall + 4)
    body -= Pos(L / 2 - p.wall / 2, 0, p.floor + 9) * Rot(0, 90, 0) * Cylinder(p.breather / 2, p.wall + 4)
    return body


def build_housing_top(p: P):
    L, W = p.housing_l, p.housing_w
    lid = boxz(L, W, p.wall, 0)
    # Upper-bearing recess (race seats up against the lid) + chamfer.
    lid -= cyl(p.bearing_od / 2, p.wall - 1, 1)
    lid -= cyl(p.bearing_od / 2 + p.seat_chamfer, p.seat_chamfer, 1)
    lid -= cyl((p.bearing_id + 2.4) / 2, p.wall + 2, -1)        # turret journal + seal exits here
    # Nesting lip into the gasket groove.
    lid += (
        boxz(L - p.wall - p.gasket_w, W - p.wall - p.gasket_w, p.gasket_d, -p.gasket_d)
        - boxz(L - p.wall - 2 * p.gasket_w - 1, W - p.wall - 2 * p.gasket_w - 1, p.gasket_d + 1, -p.gasket_d)
    )
    for (x, y) in p.lid_bolts():                               # clearance holes
        lid -= Pos(x, y, 0) * cyl(p.lid_bolt / 2, p.wall * 3, -p.wall)
    return lid


def assembly(p: P, parts: dict):
    a = parts["housing_bottom"]
    a += Pos(0, 0, p.cavity_h + p.floor) * parts["housing_top"]
    a += Pos(0, 0, p.floor) * parts["turret_holder"]
    a += Pos(0, 0, p.floor + p.bearing_w + 1) * parts["ring_gear"]
    a += Pos(p.center_dist, 0, p.floor + p.bearing_w + 1) * parts["pinion"]
    return a


def main() -> None:
    p = P()
    out = os.path.join(os.path.dirname(__file__), "out", "steering")
    os.makedirs(out, exist_ok=True)
    parts = {
        "pinion": build_pinion(p),
        "ring_gear": build_ring_gear(p),
        "turret_holder": build_turret(p),
        "housing_bottom": build_housing_bottom(p),
        "housing_top": build_housing_top(p),
    }
    for name, obj in parts.items():
        export_step(obj, os.path.join(out, f"{name}.step"))
        export_stl(obj, os.path.join(out, f"{name}.stl"))
        print(f"  wrote {name}")
    export_stl(assembly(p, parts), os.path.join(out, "assembly.stl"))
    print(f"reduction {p.ring_teeth/p.pinion_teeth:.1f}:1  module {p.module}  centre {p.center_dist:.1f}mm  "
          f"housing {p.housing_l:.0f}x{p.housing_w:.0f}x{p.cavity_h+p.floor:.0f}mm")


if __name__ == "__main__":
    main()
