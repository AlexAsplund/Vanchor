"""
Steering servo v3 — robust watertight 1:1 worm-gear actuator with
hall-indexed absolute feedback.

Clean-sheet redesign (PETG-first) around the 5840-31ZY worm gearmotor
(20 rpm high-torque variant), AS5600 encoder and the 25.4 mm
trolling-motor shaft:

  * 1:1 drive (z24/z24, module 2, PA 22.5deg, 16 mm face). The 20 rpm
    5840 variant already delivers several times the needed torque, so a
    reduction would only halve steering speed AND concentrate more
    tooth force on a smaller pinion. 1:1 keeps 120 deg/s at the shaft
    and the larger pinion radius keeps stall tooth stress ~35 MPa -
    the printed gears remain the designed fuse against a hard jam.
  * Absolute full-360deg: AS5600 on the pinion reads output azimuth
    directly (1:1). A hall switch (TO-92 in a lid boss) over an index
    magnet in the pinion top face fires once per revolution: a physical
    zero reference independent of gear meshing at assembly, plus a
    drift/slip self-check every rev. Firmware:
    azimuth = as5600 + stored_offset; re-validate at each index pulse.
  * Sealing: hollow output hub running in two TC 35x47x7 rotary lip
    seals; lid sealed by a FORM-IN-PLACE neutral-cure silicone bead in
    a shallow rim channel (no printed TPU gasket); blind heat-set
    bosses (no fastener reaches the interior); motor clamped by
    nest+strap (no shell penetrations); PG7 gland; blind vent + grease
    pilots; press-on splash cap over the lid bore.
  * Plastic-optimised shell: 2 mm walls / 2.8 mm floor (waterproofing
    comes from an epoxy/paint coat, rigidity from the box shape + the
    motor itself), 6.5 mm lid only where the seal pocket needs it,
    O8 bosses. No hold-down flanges: the housing is retained by the
    user's transom-mount design.

Toolchain: build123d + bd_warehouse (aarch64; CadQuery unavailable).
Run:  .venv/bin/python cad/ai2/servo.py   -> STL + STEP in cad/ai2/out/
Exported STLs are print-oriented (flat base, no supports).
"""

from dataclasses import dataclass
from build123d import *  # noqa: F403
import math
import os

from bd_warehouse.gear import SpurGear

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


# ============================================================
# PARAMETERS - all mm
# ============================================================
@dataclass
class P:
    # --- trolling-motor shaft ---
    shaft_d: float = 25.4
    shaft_clear: float = 0.3       # coupler bore clearance (clamped closed)
    hub_bore_clear: float = 1.0    # hub tube runs free around the shaft

    # --- gears: 1:1, AS5600 reads output azimuth directly ---
    module: float = 2.0
    teeth_p: int = 24              # pinion (AS5600 axis)
    teeth_r: int = 24              # output gear on the hub
    pressure_angle: float = 22.5   # stubby strong teeth
    gear_t: float = 16.0           # face width
    backlash_cd: float = 0.25

    # --- rotary lip seals: TC 35x47x7 ---
    seal_land_d: float = 35.0
    seal_od: float = 47.0
    seal_h: float = 7.0
    seal_pocket_clear: float = 0.3
    seal_pocket_extra_h: float = 0.4

    # --- motor: 5840-31ZY (user's drawing) ---
    mot_box_l: float = 58.2
    mot_box_w: float = 40.0
    mot_box_h: float = 34.0        # face to deepest casting point
    mot_axis_from_end: float = 20.0
    mot_shaft_d: float = 8.0
    mot_shaft_flat: float = 7.0
    mot_shaft_len: float = 15.0
    mot_shaft_flat_len: float = 13.0
    mot_can_d: float = 31.0
    mot_can_l: float = 57.0
    mot_can_axis_below_face: float = 16.0
    mot_fit: float = 0.3

    # --- AS5600 (on pinion) ---
    magnet_d: float = 6.0          # diametric 6x2.5 in the pinion boss
    magnet_h: float = 2.5
    magnet_gap: float = 0.9
    enc_hole_pitch: float = 18.0   # board holes - measure your breakout!
    enc_hole_pilot: float = 1.9
    enc_board_t: float = 1.6
    enc_chip_h: float = 0.9

    # --- hall index (magnet in pinion top face, sensor in a lid boss) ---
    idx_magnet_d: float = 4.0      # axial O4x2
    idx_magnet_h: float = 2.0
    idx_r: float = 17.5            # orbit radius on the pinion (inside root)
    idx_gap: float = 1.2           # magnet face to sensor face
    idx_body: float = 5.2          # TO-92 pocket width (4.6 body + fit)
    idx_body_t: float = 3.4        # TO-92 pocket thickness

    # --- housing (original-gauge walls: epoxy/paint handles porosity,
    # the motor + box shape handle rigidity) ---
    wall: float = 2.0
    floor_t: float = 2.8
    lid_t: float = 6.5
    rim_flange: float = 2.2
    corner_r: float = 4.0
    gap_gear_wall: float = 2.5
    wire_bay: float = 13.0
    gland_hole_d: float = 12.2     # PG7
    pilot_d: float = 5.5           # blind vent + grease pilots

    # --- form-in-place silicone gasket (neutral-cure bead in a rim channel) ---
    groove_w: float = 2.4          # retention channel width in the rim
    groove_d: float = 0.8          # channel depth
    groove_inset: float = 0.7      # channel inner edge from the outer face

    # --- fasteners ---
    insert_hole_d: float = 4.0
    insert_hole_h: float = 8.0
    m3_clear: float = 3.4
    m3_cb_d: float = 6.5
    boss_d: float = 8.0
    m4_clear: float = 4.3
    m4_head_d: float = 8.2
    m4_nut_af: float = 7.3
    m4_nut_t: float = 3.6
    # boss_d 8: O4 insert pocket + 2 mm wall - enough for M3 insert torque

    # --- coupler / drive hex / splash cap ---
    hex_af: float = 30.3
    hex_fit: float = 0.4
    hex_h: float = 9.0
    coupler_od: float = 41.0
    coupler_clamp_h: float = 30.0
    slit_w: float = 2.4
    cap_skirt_od: float = 56.0     # splash-cap umbrella
    cap_t: float = 1.8

    # ---------------- derived ----------------
    @property
    def pd_p(self):           return self.module * self.teeth_p
    @property
    def pd_r(self):           return self.module * self.teeth_r
    @property
    def ring_od(self):        return self.module * (self.teeth_r + 2)
    @property
    def cd(self):             return (self.pd_p + self.pd_r) / 2 + self.backlash_cd
    @property
    def face_z(self):         return self.mot_box_h + 2.0
    @property
    def gear_z0(self):        return self.face_z + 1.0
    @property
    def gear_z1(self):        return self.gear_z0 + self.gear_t
    @property
    def cone_z1(self):        return self.gear_z1 + (self.ring_od - self.seal_land_d) / 2
    @property
    def land_r(self):         return self.seal_land_d / 2
    @property
    def hub_bore_d(self):     return self.shaft_d + self.hub_bore_clear
    @property
    def pocket_d(self):       return self.seal_od + self.seal_pocket_clear
    @property
    def pocket_h(self):       return self.seal_h + self.seal_pocket_extra_h
    @property
    def boss_rim_z(self):     return self.gear_z0 - 0.4
    @property
    def boss_or(self):        return self.pocket_d / 2 + self.wall  # floor boss outer r
    @property
    def bot_pocket_z0(self):  return self.boss_rim_z - self.pocket_h
    @property
    def hub_z0(self):         return self.bot_pocket_z0 - 1.2
    @property
    def top_boss_z0(self):    return self.cone_z1 + 0.2
    @property
    def top_pocket_z1(self):  return self.top_boss_z0 + self.pocket_h
    @property
    def land_z1(self):        return self.top_pocket_z1 + 0.4
    @property
    def hub_z1(self):         return self.land_z1 + self.hex_h
    @property
    def h_int(self):          return 64.6
    @property
    def inner_w(self):        return self.ring_od + 2 * self.gap_gear_wall
    @property
    def half_w(self):         return self.inner_w / 2
    @property
    def y_min(self):          return -(self.ring_od / 2 + self.gap_gear_wall)
    @property
    def mot_y0(self):         return self.cd - self.mot_axis_from_end
    @property
    def mot_y1(self):         return self.mot_y0 + self.mot_box_l
    @property
    def can_y1(self):         return self.mot_y1 + self.mot_can_l
    @property
    def y_max(self):          return self.can_y1 + self.wire_bay
    @property
    def lid_z0(self):         return self.h_int  # lid seats on the rim (FIPG)
    @property
    def lid_z1(self):         return self.lid_z0 + self.lid_t
    @property
    def can_axis_z(self):     return self.face_z - self.mot_can_axis_below_face
    @property
    def screw_x(self):        return self.half_w - (self.boss_d / 2 - 0.3)
    @property
    def screw_ys(self):       return [self.y_min + 4.7, 24.0, 70.0, 102.0, self.y_max - 4.7]
    @property
    def pin_boss_z1(self):    return self.gear_z1 + 4  # pinion magnet boss top
    @property
    def hall_face_z(self):    return self.gear_z1 + self.idx_gap  # sensor face


p = P()

# ---- design invariants -----------------------------------------------------
assert p.mot_y0 > p.boss_or + 1.0, "gearbox nose hits the floor seal boss"
assert p.top_pocket_z1 < p.lid_z1 - 1.5, "seal pocket roof too thin"
assert p.hex_af / math.cos(math.radians(30)) <= p.seal_land_d + 0.05
assert p.idx_r + p.idx_magnet_d / 2 < p.pd_p / 2 - p.module * 1.25 - 0.4, \
    "index magnet orbit must stay inside the pinion root circle"
for _ys in p.screw_ys:
    assert math.hypot(p.screw_x, _ys) > p.ring_od / 2 + p.boss_d / 2 + 0.4 and \
        math.hypot(p.screw_x, _ys - p.cd) > p.ring_od / 2 + p.boss_d / 2 + 0.4, \
        f"screw boss at y={_ys} inside a gear sweep"
assert p.face_z + p.mot_shaft_len <= p.pin_boss_z1 - p.magnet_h - 1.0
assert p.hall_face_z > p.gear_z1 + 0.8, "hall boss touches the pinion"


def _gear(teeth):
    return SpurGear(p.module, teeth, p.pressure_angle, p.gear_t)


def hub_gear():
    """Output hub: seal land / ring gear z32 / 45deg print cone / seal land /
    drive hex. Index magnet pocket in the underside web. Prints hex-down."""
    gear = Pos(0, 0, (p.gear_z0 + p.gear_z1) / 2) * _gear(p.teeth_r)
    bot_land = Pos(0, 0, (p.hub_z0 + p.gear_z0) / 2) * Cylinder(
        p.land_r, p.gear_z0 - p.hub_z0)
    cone = Pos(0, 0, p.gear_z1) * Cone(
        p.ring_od / 2, p.land_r, p.cone_z1 - p.gear_z1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    top_land = Pos(0, 0, (p.cone_z1 + p.land_z1) / 2) * Cylinder(
        p.land_r, p.land_z1 - p.cone_z1)
    hexp = Pos(0, 0, p.land_z1) * extrude(
        RegularPolygon(p.hex_af / math.cos(math.radians(30)) / 2, 6,
                       major_radius=True), p.hex_h)
    hub = gear + bot_land + cone + top_land + hexp
    hub = chamfer(hub.edges().filter_by(GeomType.CIRCLE)
                  .group_by(Axis.Z)[0], 1.2)          # seal lead-in
    hub -= Pos(0, 0, p.hub_z0 - 1) * Cylinder(
        p.hub_bore_d / 2, p.hub_z1 - p.hub_z0 + 2,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    return hub


def pinion():
    """Motor pinion z16: magnet boss for the AS5600, blind D-bore with the
    flat ending where the shaft flat ends, radial grub pilot."""
    gear = Pos(0, 0, (p.gear_z0 + p.gear_z1) / 2) * _gear(p.teeth_p)
    part = gear + Pos(0, 0, p.gear_z1) * Cylinder(
        7, p.pin_boss_z1 - p.gear_z1, align=(Align.CENTER, Align.CENTER, Align.MIN))
    bore_r = p.mot_shaft_d / 2 + 0.15
    flat_y = p.mot_shaft_flat - p.mot_shaft_d / 2 + 0.15
    bore_top = p.face_z + p.mot_shaft_len + 0.2
    void = Pos(0, 0, p.gear_z0 - 1) * Cylinder(
        bore_r, bore_top - p.gear_z0 + 1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    tongue = void & Pos(0, flat_y + 10, 0) * Box(22, 20, 300)
    tongue &= Pos(0, 0, p.gear_z0 - 1) * Box(
        22, 20, p.face_z + p.mot_shaft_flat_len + 0.2 - (p.gear_z0 - 1),
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    part -= void - tongue
    part -= Pos(0, 0, p.pin_boss_z1 - p.magnet_h - 0.1) * Cylinder(
        p.magnet_d / 2 + 0.1, p.magnet_h + 0.2,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    # index magnet pocket in the top face (flush), same phase as output at 1:1
    part -= Pos(p.idx_r, 0, p.gear_z1 - p.idx_magnet_h - 0.2) * Cylinder(
        p.idx_magnet_d / 2 + 0.1, p.idx_magnet_h + 0.3,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    part -= Pos(0, 14, p.gear_z0 + 7) * Rot(90, 0, 0) * Cylinder(1.3, 24)
    return part


def coupler():
    """Hex-socket split clamp; groove near the base takes the splash cap."""
    z0 = p.lid_z1 + 0.5
    z1 = p.hub_z1 + p.coupler_clamp_h
    body = Pos(0, 0, z0) * Cylinder(
        p.coupler_od / 2, z1 - z0, align=(Align.CENTER, Align.CENTER, Align.MIN))
    body = chamfer(body.edges().filter_by(GeomType.CIRCLE).group_by(Axis.Z)[-1], 1.5)
    lug_y0, lug_y1 = 14.0, 26.0
    lug_z0 = z0 + 3.5              # lugs start above the splash-cap groove
    lugs = Pos(0, (lug_y0 + lug_y1) / 2, lug_z0) * Box(
        22, lug_y1 - lug_y0, z1 - lug_z0, align=(Align.CENTER, Align.CENTER, Align.MIN))
    lugs = fillet(lugs.edges().filter_by(Axis.Z).group_by(Axis.Y)[-1], 3)
    body += lugs
    sock_af = p.hex_af + p.hex_fit
    body -= Pos(0, 0, z0 - 0.1) * extrude(
        RegularPolygon(sock_af / math.cos(math.radians(30)) / 2, 6,
                       major_radius=True), p.hub_z1 - z0 + 0.3)
    body -= Pos(0, 0, p.hub_z1) * Cylinder(
        (p.shaft_d + p.shaft_clear) / 2, z1 - p.hub_z1 + 1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    # splash-cap groove (1 mm deep, chamfered entry from below)
    body -= Pos(0, 0, z0 + 1.2) * Cylinder(
        p.coupler_od / 2 + 2, 2.0, align=(Align.CENTER, Align.CENTER, Align.MIN)) \
        - Pos(0, 0, z0 + 1.2) * Cylinder(
            p.coupler_od / 2 - 1, 2.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
    slit_z0 = p.hub_z1 + 2
    body -= Pos(0, p.coupler_od / 4 + 6, slit_z0) * Box(
        p.slit_w, p.coupler_od / 2 + 16, z1 - slit_z0 + 1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    for bz in (slit_z0 + 7, z1 - 7):
        body -= Pos(0, 20, bz) * Rot(0, 90, 0) * Cylinder(p.m4_clear / 2, 60)
        body -= Pos(11 - 2.5, 20, bz) * Rot(0, 90, 0) * Cylinder(
            p.m4_head_d / 2, 20, align=(Align.CENTER, Align.CENTER, Align.MIN))
        nut = extrude(RegularPolygon(p.m4_nut_af / math.sqrt(3), 6,
                                     major_radius=True), 20)
        body -= Pos(-11 + p.m4_nut_t, 20, bz) * Rot(0, -90, 0) * nut
    return body


def splash_cap():
    """Umbrella ring: collar snaps into the coupler groove, skirt shields
    the lid bore gap from spray. Prints skirt-rim down, support-free."""
    z0 = p.lid_z1 + 0.5
    collar = Pos(0, 0, z0 + 1.3) * Cylinder(
        p.coupler_od / 2 + p.cap_t, 1.8,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    collar -= Pos(0, 0, z0 + 1.2) * Cylinder(
        p.coupler_od / 2 - 0.8, 2.2, align=(Align.CENTER, Align.CENTER, Align.MIN))
    skirt = Pos(0, 0, p.lid_z1 + 0.8) * Cone(
        p.cap_skirt_od / 2, p.coupler_od / 2 + p.cap_t, 2.3,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    skirt -= Pos(0, 0, p.lid_z1 + 0.6) * Cone(
        p.cap_skirt_od / 2 - 2 * p.cap_t, p.coupler_od / 2 - 0.8, 2.3,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    cap = collar + skirt
    # clear the centre completely (grip lip only) so hub hex + coupler pass
    cap -= Pos(0, 0, z0 - 0.5) * Cylinder(
        p.coupler_od / 2 - 0.8, 6, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return cap


def housing():
    """Robust sealed body + hall-index tower + transom flanges."""
    yc = (p.y_min + p.y_max) / 2
    L = p.y_max - p.y_min
    outer = Pos(0, yc, (p.h_int - p.floor_t) / 2) * Box(
        p.inner_w + 2 * p.wall, L + 2 * p.wall, p.h_int + p.floor_t)
    outer = fillet(outer.edges().filter_by(Axis.Z), p.corner_r)
    cavity = Pos(0, yc, p.h_int / 2 + 1) * Box(p.inner_w, L, p.h_int + 2)
    cavity = fillet(cavity.edges().filter_by(Axis.Z), 2.0)
    part = outer - cavity

    # rim flange (gasket land)
    flange = Pos(0, yc, p.h_int - 2) * Box(p.inner_w, L, 4)
    flange = fillet(flange.edges().filter_by(Axis.Z), 2.0)
    fl_in = Pos(0, yc, p.h_int - 2) * Box(
        p.inner_w - 2 * p.rim_flange, L - 2 * p.rim_flange, 6)
    fl_in = fillet(fl_in.edges().filter_by(Axis.Z), 0.6)
    part += flange - fl_in

    # form-in-place gasket: shallow silicone retention channel in the rim,
    # passing outboard of every screw hole (bead stays continuous over the
    # boss tops)
    og = Pos(0, yc) * Rectangle(p.inner_w + 2 * p.wall - 2 * p.groove_inset,
                                L + 2 * p.wall - 2 * p.groove_inset)
    og = fillet(og.vertices(), p.corner_r - 0.5)
    ig = Pos(0, yc) * Rectangle(
        p.inner_w + 2 * p.wall - 2 * (p.groove_inset + p.groove_w),
        L + 2 * p.wall - 2 * (p.groove_inset + p.groove_w))
    ig = fillet(ig.vertices(), max(0.5, p.corner_r - 0.5 - p.groove_w))
    part -= Pos(0, 0, p.h_int - p.groove_d) * extrude(og - ig, p.groove_d + 1)

    # ---- floor seal boss ----
    part += Cylinder(p.boss_or, p.boss_rim_z,
                     align=(Align.CENTER, Align.CENTER, Align.MIN))
    part -= Pos(0, 0, p.bot_pocket_z0) * Cylinder(
        p.pocket_d / 2, p.pocket_h + 1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    ch_z1 = p.bot_pocket_z0
    ch_z0 = ch_z1 - (p.pocket_d - 37.0) / 2
    part -= Pos(0, 0, ch_z0) * Cone(37.0 / 2, p.pocket_d / 2, ch_z1 - ch_z0,
                                    align=(Align.CENTER, Align.CENTER, Align.MIN))
    part -= Pos(0, 0, -0.5) * Cylinder(
        37.0 / 2, ch_z0 + 0.5, align=(Align.CENTER, Align.CENTER, Align.MIN))
    part -= Pos(0, 0, -p.floor_t - 1) * Cylinder(
        (p.shaft_d + 3.1) / 2, p.floor_t + 2,
        align=(Align.CENTER, Align.CENTER, Align.MIN))

    # ---- motor nest ----
    hw = p.mot_box_w / 2 + p.mot_fit
    for s in (1, -1):
        part += Pos(s * (hw / 2 + 2), (p.mot_y0 + p.mot_y1) / 2, 1) * Box(
            hw - 4, p.mot_box_l - 1, 2)
        part += Pos(s * (hw + 1.2), (p.mot_y0 + p.mot_y1) / 2,
                    (p.face_z - 0.5) / 2) * Box(2.4, p.mot_box_l, p.face_z - 0.5)
    front = Pos(0, p.mot_y0 - 1.2 - p.mot_fit, (p.face_z - 0.5) / 2) * Box(
        2 * hw + 4.8, 2.4, p.face_z - 0.5)
    rear = Pos(0, p.mot_y1 + 1.2 + p.mot_fit, (p.face_z - 0.5) / 2) * Box(
        2 * hw + 4.8, 2.4, p.face_z - 0.5)
    can_relief = Pos(0, p.mot_y1 + 1.2, p.can_axis_z) * Rot(90, 0, 0) * Cylinder(
        p.mot_can_d / 2 + 0.5, 24)
    part += front + (rear - can_relief)

    # saddle + strap pillars for the motor can
    sad_y = (p.mot_y1 + p.can_y1) / 2
    saddle = Pos(0, sad_y, 3) * Box(28, 14, 6)
    saddle -= Pos(0, sad_y, p.can_axis_z) * Rot(90, 0, 0) * Cylinder(
        p.mot_can_d / 2 + 0.1, 20)
    part += saddle
    for s in (1, -1):
        part += Pos(s * (p.mot_can_d / 2 + 5), sad_y, 12) * Cylinder(4.5, 24)
        part -= Pos(s * (p.mot_can_d / 2 + 5), sad_y, 24 - p.insert_hole_h) * \
            Cylinder(p.insert_hole_d / 2, p.insert_hole_h + 0.1,
                     align=(Align.CENTER, Align.CENTER, Align.MIN))

    # ---- lid screw bosses: sides + end centres, all blind ----
    pts = [(s * p.screw_x, ys) for ys in p.screw_ys for s in (1, -1)]
    pts += [(0.0, p.y_max - 4.7)]  # no -Y end boss: ring gear sweeps there
    for bx, by in pts:
        part += Pos(bx, by, p.h_int / 2) * Cylinder(p.boss_d / 2, p.h_int)
        part -= Pos(bx, by, p.h_int - p.insert_hole_h) * Cylinder(
            p.insert_hole_d / 2, p.insert_hole_h + 0.1,
            align=(Align.CENTER, Align.CENTER, Align.MIN))

    # ---- PG7 gland + blind vent & grease pilots (motor-end wall) ----
    part += Pos(0, p.y_max - 1.5, 38) * Rot(90, 0, 0) * Cylinder(10, 5)
    part -= Pos(0, p.y_max + p.wall + 1, 38) * Rot(90, 0, 0) * Cylinder(
        p.gland_hole_d / 2, 14, align=(Align.CENTER, Align.CENTER, Align.MIN))
    for px in (12, -12):   # vent (right) / grease (left) - drill to activate
        part -= Pos(px, p.y_max + p.wall + 0.01, p.h_int - 8) * Rot(90, 0, 0) * \
            Cylinder(p.pilot_d / 2, 1.6,
                     align=(Align.CENTER, Align.CENTER, Align.MIN))

    # no hold-down flanges: the transom-mount design retains the housing
    return part


def lid():
    """Thick lid: seal boss, AS5600 bosses, blind-boss screw pattern."""
    yc = (p.y_min + p.y_max) / 2
    L = p.y_max - p.y_min
    plate = Pos(0, yc, p.lid_z0 + p.lid_t / 2) * Box(
        p.inner_w + 2 * p.wall, L + 2 * p.wall, p.lid_t)
    plate = fillet(plate.edges().filter_by(Axis.Z), p.corner_r)
    boss = Pos(0, 0, p.top_boss_z0) * Cylinder(
        p.pocket_d / 2 + p.wall, p.lid_z0 - p.top_boss_z0 + 0.1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    part = plate + boss
    part -= Pos(0, 0, p.top_boss_z0 - 0.1) * Cylinder(
        p.pocket_d / 2, p.pocket_h + 0.1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    part -= Pos(0, 0, p.top_pocket_z1) * Cylinder(
        37.0 / 2, p.lid_z1 - p.top_pocket_z1 + 1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    # hall-index boss over the pinion's magnet orbit (pocket opens down;
    # prints upward in the lid's top-face-down orientation)
    hall_y = p.cd + p.idx_r   # far side of the pinion, clear of the hub cone
    part += Pos(0, hall_y, p.hall_face_z) * Cylinder(
        6, p.lid_z0 - p.hall_face_z + 0.1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    part -= Pos(0, hall_y, p.hall_face_z - 0.1) * Box(
        p.idx_body, p.idx_body, p.idx_body_t + 0.1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))          # TO-92 pocket
    part -= Pos(0, hall_y + 4, p.hall_face_z - 0.1) * Box(
        3, 8, p.idx_body_t + 0.1,
        align=(Align.CENTER, Align.MIN, Align.MIN))             # lead notch
    board_top = p.pin_boss_z1 + p.magnet_gap + p.enc_chip_h + p.enc_board_t
    for sx in (1, -1):
        for sy in (1, -1):
            bp = Pos(sx * p.enc_hole_pitch / 2, p.cd + sy * p.enc_hole_pitch / 2,
                     board_top)
            part += bp * Cylinder(3.5, p.lid_z0 - board_top,
                                  align=(Align.CENTER, Align.CENTER, Align.MIN))
            part -= bp * Cylinder(p.enc_hole_pilot / 2, 5,
                                  align=(Align.CENTER, Align.CENTER, Align.MIN))
    pts = [(s * p.screw_x, ys) for ys in p.screw_ys for s in (1, -1)]
    pts += [(0.0, p.y_max - 4.7)]  # no -Y end boss: ring gear sweeps there
    for bx, by in pts:
        part -= Pos(bx, by, p.lid_z0 - 1) * Cylinder(
            p.m3_clear / 2, p.lid_t + 2,
            align=(Align.CENTER, Align.CENTER, Align.MIN))
        part -= Pos(bx, by, p.lid_z1 - 2.5) * Cylinder(
            p.m3_cb_d / 2, 2.6, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return part


def strap():
    """Motor-can hold-down strap."""
    sad_y = (p.mot_y1 + p.can_y1) / 2
    top_z = p.can_axis_z + p.mot_can_d / 2 + 5
    block = Pos(0, sad_y, (24 + top_z) / 2) * Box(
        2 * (p.mot_can_d / 2 + 5 + 5), 14, top_z - 24)
    block -= Pos(0, sad_y, p.can_axis_z) * Rot(90, 0, 0) * Cylinder(
        p.mot_can_d / 2 + 0.3, 20)
    for s in (1, -1):
        block -= Pos(s * (p.mot_can_d / 2 + 5), sad_y, 20) * Cylinder(
            p.m3_clear / 2, 60)
        block -= Pos(s * (p.mot_can_d / 2 + 5), sad_y, 28) * Cylinder(
            p.m3_cb_d / 2, 60, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return block


# ============================================================
# BUILD + EXPORT
# ============================================================
def zero(part, flip=False):
    if flip:
        part = Rot(180, 0, 0) * part
    bb = part.bounding_box()
    return Pos(0, 0, -bb.min.Z) * part


if __name__ == "__main__":
    print(f"{p.teeth_r}/{p.teeth_p} drive  CD {p.cd}  gear OD {p.ring_od}  "
          f"interior {p.inner_w:.0f} x {p.y_max - p.y_min:.0f} x {p.h_int:.0f}")
    parts = {
        "HubGear": (hub_gear(), True),
        "Pinion": (pinion(), False),
        "Coupler": (coupler(), True),
        "SplashCap": (splash_cap(), True),
        "Housing": (housing(), False),
        "Lid": (lid(), True),
        "Strap": (strap(), True),
    }
    os.makedirs(OUT, exist_ok=True)
    asm = []
    for name, (part, flip) in parts.items():
        bb = part.bounding_box()
        print(f"{name}: bbox {bb.max.X - bb.min.X:.1f} x "
              f"{bb.max.Y - bb.min.Y:.1f} x {bb.max.Z - bb.min.Z:.1f}  "
              f"z {bb.min.Z:.1f}..{bb.max.Z:.1f}")
        if name == "Pinion":
            part = Pos(0, p.cd, 0) * Rot(0, 0, 180.0 / p.teeth_p) * part
        asm.append(part)
        pp = zero(part, flip)
        export_stl(pp, os.path.join(OUT, f"{name}.stl"), tolerance=0.01,
                   angular_tolerance=0.1)
        export_step(pp, os.path.join(OUT, f"{name}.step"))
    export_stl(Compound(children=asm), os.path.join(OUT, "Assembly.stl"),
               tolerance=0.02, angular_tolerance=0.2)
    print("exported ->", OUT)
