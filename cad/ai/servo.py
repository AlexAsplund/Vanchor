"""
Watertight steering-servo gearbox for the trolling-motor autopilot.

Improved, fully parametric variant of cad/models/*.stl (the Fusion 360
"TrollingMotorServo" export). Same concept — a self-locking worm gearmotor
drives a 1:1 spur pair whose hollow output hub grips the trolling-motor
shaft, with an AS5600 absolute encoder reading the free pinion axis — but
redesigned around a sealed enclosure:

  * The output gear is one hollow hub with two smooth seal lands riding in
    standard TC 35x47x7 rotary lip seals (one in the floor boss, one in the
    lid boss). The wet 25.4 mm shaft passes through the hub bore without
    ever entering the sealed volume.
  * Flat printed-TPU gasket under the lid, screws into BLIND heat-set
    bosses so no fastener channel reaches the interior.
  * The motor is clamped by a printed nest + strap - zero screws through
    housing walls (the original bolted the motor and encoder through the
    lid, both leak paths).
  * AS5600 hangs from bosses under the lid over a magnet in the pinion.
  * Single PG7 cable gland for all wiring, blind vent pilot for an
    optional Gore-type breather.
  * Proper involute gears (module 2, 24T/24T) replace the trapezoidal
    teeth. Still 1:1 so the encoder keeps reading true output angle.

Motor: 5840-31ZY-class worm gearmotor (dimensions from the user's drawing:
gearbox 58.2 x 40 x 34, motor can D31 x 57, 8 mm D-shaft x 15 protrusion
with 13 mm flat, output axis 20 mm from the gearbox far end, centred in
the 40 mm width). Self-locking drive is preserved.

Toolchain: build123d (aarch64; CadQuery does not install here).
Run:  .venv/bin/python cad/ai/servo.py   -> STL + STEP in cad/ai/out/

Print orientation: every exported STL is already oriented for printing
(flat face on the bed, no supports needed).
"""

from dataclasses import dataclass, field
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
    shaft_d: float = 25.4          # 1" motor shaft (measured off original STL)
    shaft_clear: float = 0.3       # coupler bore clearance (clamped closed)
    hub_bore_clear: float = 1.0    # hub tube runs free around the shaft

    # --- gears (1:1 so AS5600 on pinion axis reads output angle) ---
    module: float = 2.0
    teeth: int = 24                # both gears; PD 48, OD 52
    pressure_angle: float = 20.0
    gear_t: float = 14.0           # face width
    backlash_cd: float = 0.25      # extra centre distance for printed teeth

    # --- rotary lip seals: TC 35x47x7 (two, top + bottom) ---
    seal_land_d: float = 35.0      # hub seal-land OD (post-process smooth!)
    seal_od: float = 47.0
    seal_h: float = 7.0
    seal_pocket_clear: float = 0.3   # pocket dia = seal_od + this (press fit)
    seal_pocket_extra_h: float = 0.4

    # --- motor: 5840-31ZY worm gearmotor (user's drawing) ---
    mot_box_l: float = 58.2        # gearbox length
    mot_box_w: float = 40.0        # gearbox width
    mot_box_h: float = 34.0        # face to deepest point of casting
    mot_axis_from_end: float = 20.0  # output axis to gearbox far end
    mot_shaft_d: float = 8.0       # D-shaft
    mot_shaft_flat: float = 7.0    # across the flat (matches original STL bore)
    mot_shaft_len: float = 15.0    # protrusion above face
    mot_shaft_flat_len: float = 13.0
    mot_can_d: float = 31.0        # motor can
    mot_can_l: float = 57.0
    mot_can_axis_below_face: float = 16.0
    mot_fit: float = 0.3           # nest clearance around gearbox

    # --- AS5600 encoder ---
    magnet_d: float = 6.0          # diametric magnet 6x2.5 in pinion boss
    magnet_h: float = 2.5
    magnet_gap: float = 0.9        # chip face to magnet top
    enc_hole_pitch: float = 18.0   # board mount hole grid (measure your board!)
    enc_hole_pilot: float = 1.9    # M2 self-tap pilots
    enc_board_t: float = 1.6
    enc_chip_h: float = 0.9

    # --- housing ---
    wall: float = 2.4
    floor_t: float = 3.0
    lid_t: float = 5.0             # thick lid: stiff gasket clamp + seal pocket roof
    rim_flange: float = 1.2        # inward gasket land widening at the rim
    corner_r: float = 3.0
    gap_gear_wall: float = 2.0     # gear OD to inner wall
    wire_bay: float = 12.0         # space behind motor for wiring
    gland_hole_d: float = 12.2     # PG7 cable gland
    vent_pilot_d: float = 5.5      # blind pilot for optional M6 breather
    ear_hole_d: float = 5.2        # boat-mount ears

    # --- gasket (printed TPU, or template for 2 mm neoprene sheet) ---
    gasket_t: float = 2.0          # compresses to ~1.5
    gasket_squeeze: float = 0.5

    # --- fasteners ---
    insert_hole_d: float = 4.0     # M3 heat-set (D4.6 knurl) pocket
    insert_hole_h: float = 8.0
    m3_clear: float = 3.4
    m3_cb_d: float = 6.5
    boss_d: float = 9.0            # lid screw bosses
    m4_clear: float = 4.3          # coupler pinch bolts
    m4_head_d: float = 8.2
    m4_nut_af: float = 7.3         # nut pocket across flats (7.0 + fit)
    m4_nut_t: float = 3.6

    # --- coupler / drive hex ---
    hex_af: float = 30.3           # hub drive hex across flats (A/C 35 = land)
    hex_fit: float = 0.4           # socket A/F = hex_af + this
    coupler_od: float = 41.0
    coupler_clamp_h: float = 30.0
    slit_w: float = 2.4

    # ---------------- derived (read-only) ----------------
    @property
    def gear_od(self):        return self.module * (self.teeth + 2)
    @property
    def gear_pd(self):        return self.module * self.teeth
    @property
    def cd(self):             return self.gear_pd + self.backlash_cd  # centre distance
    @property
    def face_z(self):         return self.mot_box_h + 2.0     # motor face height (2 mm rails)
    @property
    def gear_z0(self):        return self.face_z + 1.0        # gear plane bottom
    @property
    def gear_z1(self):        return self.gear_z0 + self.gear_t
    @property
    def land_r(self):         return self.seal_land_d / 2
    @property
    def cone_z1(self):        # 45deg print cone gear OD -> land OD
        return self.gear_z1 + (self.gear_od - self.seal_land_d) / 2
    @property
    def hub_bore_d(self):     return self.shaft_d + self.hub_bore_clear
    @property
    def pocket_d(self):       return self.seal_od + self.seal_pocket_clear
    @property
    def pocket_h(self):       return self.seal_h + self.seal_pocket_extra_h
    # bottom seal stack
    @property
    def boss_rim_z(self):     return self.gear_z0 - 0.4       # thrust rim under gear
    @property
    def bot_pocket_z0(self):  return self.boss_rim_z - self.pocket_h
    @property
    def hub_z0(self):         return self.bot_pocket_z0 - 1.2  # hub bottom end
    # top seal stack
    @property
    def top_boss_z0(self):    return self.cone_z1 + 0.2       # lid boss bottom face
    @property
    def top_pocket_z1(self):  return self.top_boss_z0 + self.pocket_h
    @property
    def land_z1(self):        return self.top_pocket_z1 + 0.4  # land top / hex bottom
    @property
    def hex_h(self):          return 7.5
    @property
    def hub_z1(self):         return self.land_z1 + self.hex_h
    # interior envelope
    @property
    def inner_w(self):        return self.gear_od + 2 * self.gap_gear_wall
    @property
    def half_w(self):         return self.inner_w / 2
    @property
    def y_min(self):          return -(self.gear_od / 2 + 2.5)
    @property
    def mot_y0(self):         return self.cd - self.mot_axis_from_end  # gearbox near end
    @property
    def mot_y1(self):         return self.mot_y0 + self.mot_box_l
    @property
    def can_y1(self):         return self.mot_y1 + self.mot_can_l
    @property
    def y_max(self):          return self.can_y1 + self.wire_bay
    @property
    def h_int(self):          return 62.0                     # interior height
    @property
    def lid_z0(self):         return self.h_int + self.gasket_t - self.gasket_squeeze
    @property
    def lid_z1(self):         return self.lid_z0 + self.lid_t
    @property
    def can_axis_z(self):     return self.face_z - self.mot_can_axis_below_face
    @property
    def screw_ys(self):       return [self.y_min + 4.2, 22.0, 72.0, 102.0, self.y_max - 4.2]
    @property
    def screw_x(self):        return self.half_w - 4.2   # bosses embed 0.3 into wall


p = P()

# sanity checks -------------------------------------------------------------
assert p.top_boss_z0 > p.cone_z1, "lid boss hits hub print-cone"
assert p.top_pocket_z1 < p.lid_z1 - 1.0, "seal pocket roof too thin"
assert p.hex_af / math.cos(math.radians(30)) <= p.seal_land_d + 0.05, \
    "hex corners must not exceed seal land"
assert p.mot_y0 > p.gear_od / 2 + 0.1 + 2.0, "gearbox hits floor boss"
assert p.face_z + p.mot_shaft_len <= p.gear_z1 + 4.0 - 1.0, "shaft hits magnet pocket"

MM = 1  # readability


def hub_gear():
    """Output hub: bottom land / involute gear / 45deg print cone / top land /
    drive hex. One piece, prints hex-down without supports."""
    gear = Pos(0, 0, (p.gear_z0 + p.gear_z1) / 2) * SpurGear(
        p.module, p.teeth, p.pressure_angle, p.gear_t)
    bot_land = Pos(0, 0, (p.hub_z0 + p.gear_z0) / 2) * Cylinder(
        p.land_r, p.gear_z0 - p.hub_z0)
    cone = Pos(0, 0, p.gear_z1) * Cone(
        p.gear_od / 2, p.land_r, p.cone_z1 - p.gear_z1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    top_land = Pos(0, 0, (p.cone_z1 + p.land_z1) / 2) * Cylinder(
        p.land_r, p.land_z1 - p.cone_z1)
    hexp = Pos(0, 0, p.land_z1) * extrude(
        RegularPolygon(p.hex_af / math.cos(math.radians(30)) / 2, 6,
                       major_radius=True), p.hex_h)
    hub = gear + bot_land + cone + top_land + hexp
    # lead chamfers for the seal lips (bottom entry + hex top)
    hub = chamfer(hub.edges().filter_by(GeomType.CIRCLE)
                  .group_by(Axis.Z)[0], 1.2)
    bore = Pos(0, 0, p.hub_z0 - 1) * Cylinder(
        p.hub_bore_d / 2, p.hub_z1 - p.hub_z0 + 2,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    return hub - bore


def pinion():
    """Motor pinion: involute gear + magnet boss, blind D-bore, grub pilot."""
    gear = Pos(0, 0, (p.gear_z0 + p.gear_z1) / 2) * SpurGear(
        p.module, p.teeth, p.pressure_angle, p.gear_t)
    boss = Pos(0, 0, p.gear_z1) * Cylinder(
        7, 4, align=(Align.CENTER, Align.CENTER, Align.MIN))
    part = gear + boss
    # blind D-bore: round hole minus the flat tongue
    bore_r = p.mot_shaft_d / 2 + 0.15
    flat_y = p.mot_shaft_flat - p.mot_shaft_d / 2 + 0.15  # flat plane from axis
    bore_top = p.face_z + p.mot_shaft_len + 0.2
    void = Pos(0, 0, p.gear_z0 - 1) * Cylinder(
        bore_r, bore_top - p.gear_z0 + 1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    tongue = void & Pos(0, flat_y + 10, 0) * Box(20, 20, 200)
    tongue = tongue & Pos(0, 0, p.gear_z0 - 1) * Box(
        20, 20, p.face_z + p.mot_shaft_flat_len + 0.2 - (p.gear_z0 - 1),
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    part = part - (void - tongue)
    # magnet pocket
    part -= Pos(0, 0, p.gear_z1 + 4 - p.magnet_h - 0.1) * Cylinder(
        p.magnet_d / 2 + 0.1, p.magnet_h + 0.2,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    # radial M3 grub pilot onto the shaft flat (self-tap)
    part -= Pos(0, 20, p.gear_z0 + 7) * Rot(90, 0, 0) * Cylinder(1.3, 40)
    return part


def coupler():
    """Shaft coupler: hex socket onto the hub, split clamp with two M4
    pinch bolts grips the 25.4 mm trolling-motor shaft. Prints hex-down."""
    z0 = p.lid_z1 + 0.5                    # bottom face, just above the lid
    z1 = p.hub_z1 + p.coupler_clamp_h
    body = Pos(0, 0, z0) * Cylinder(
        p.coupler_od / 2, z1 - z0, align=(Align.CENTER, Align.CENTER, Align.MIN))
    body = chamfer(body.edges().filter_by(GeomType.CIRCLE).group_by(Axis.Z)[-1], 1.5)
    # clamp lugs flanking the slit (pipe-clamp style) - added BEFORE the
    # cuts so the hex socket stays clear
    lug_y0, lug_y1 = 14.0, 26.0
    lugs = Pos(0, (lug_y0 + lug_y1) / 2, z0) * Box(
        22, lug_y1 - lug_y0, z1 - z0, align=(Align.CENTER, Align.CENTER, Align.MIN))
    lugs = fillet(lugs.edges().filter_by(Axis.Z).group_by(Axis.Y)[-1], 3)
    body += lugs
    # hex socket (engages hub hex above the lid)
    sock_af = p.hex_af + p.hex_fit
    body -= Pos(0, 0, z0 - 0.1) * extrude(
        RegularPolygon(sock_af / math.cos(math.radians(30)) / 2, 6,
                       major_radius=True), p.hub_z1 - z0 + 0.3)
    # shaft bore above the socket
    body -= Pos(0, 0, p.hub_z1) * Cylinder(
        (p.shaft_d + p.shaft_clear) / 2, z1 - p.hub_z1 + 1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    # clamp slit (+Y side, between the lugs)
    slit_z0 = p.hub_z1 + 2
    body -= Pos(0, p.coupler_od / 4 + 6, slit_z0) * Box(
        p.slit_w, p.coupler_od / 2 + 16, z1 - slit_z0 + 1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    # two M4 pinch bolts across the slit: through hole, head c'bore, nut pocket
    bolt_y = 20.0
    for bz in (slit_z0 + 7, z1 - 7):
        body -= Pos(0, bolt_y, bz) * Rot(0, 90, 0) * Cylinder(p.m4_clear / 2, 60)
        body -= Pos(11 - 2.5, bolt_y, bz) * Rot(0, 90, 0) * Cylinder(
            p.m4_head_d / 2, 20, align=(Align.CENTER, Align.CENTER, Align.MIN))
        nut = extrude(RegularPolygon(p.m4_nut_af / math.sqrt(3), 6,
                                     major_radius=True), 20)
        body -= Pos(-11 + p.m4_nut_t, bolt_y, bz) * Rot(0, -90, 0) * nut
    return body


def housing():
    """Bottom housing: sealed floor with seal boss, motor nest, strap
    pillars, blind lid-screw bosses, gland + vent + mount ears."""
    yc = (p.y_min + p.y_max) / 2
    L = p.y_max - p.y_min
    outer = Pos(0, yc, (p.h_int - p.floor_t) / 2) * Box(
        p.inner_w + 2 * p.wall, L + 2 * p.wall, p.h_int + p.floor_t)
    outer = fillet(outer.edges().filter_by(Axis.Z), p.corner_r)
    cavity = Pos(0, yc, p.h_int / 2 + 1) * Box(p.inner_w, L, p.h_int + 2)
    cavity = fillet(cavity.edges().filter_by(Axis.Z), 1.8)
    part = outer - cavity

    # rim gasket-land flange (inward), leaves opening for the lid seal boss
    flange = Pos(0, yc, p.h_int - 2) * Box(p.inner_w, L, 4)
    flange = fillet(flange.edges().filter_by(Axis.Z), 1.8)
    fl_in = Pos(0, yc, p.h_int - 2) * Box(
        p.inner_w - 2 * p.rim_flange, L - 2 * p.rim_flange, 6)
    fl_in = fillet(fl_in.edges().filter_by(Axis.Z), 0.6)
    part += flange - fl_in

    # ---- floor seal boss (output axis) ----
    boss = Cylinder(p.pocket_d / 2 + p.wall, p.boss_rim_z,
                    align=(Align.CENTER, Align.CENTER, Align.MIN))
    part += boss
    part -= Pos(0, 0, p.bot_pocket_z0) * Cylinder(          # seal pocket
        p.pocket_d / 2, p.pocket_h + 1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    ch_z1 = p.bot_pocket_z0                                  # 45deg relief cone
    ch_z0 = ch_z1 - (p.pocket_d - 37.0) / 2
    part -= Pos(0, 0, ch_z0) * Cone(37.0 / 2, p.pocket_d / 2, ch_z1 - ch_z0,
                                    align=(Align.CENTER, Align.CENTER, Align.MIN))
    part -= Pos(0, 0, -0.5) * Cylinder(                      # labyrinth chamber
        37.0 / 2, ch_z0 + 0.5, align=(Align.CENTER, Align.CENTER, Align.MIN))
    part -= Pos(0, 0, -p.floor_t - 1) * Cylinder(            # floor pass-through
        (p.shaft_d + 3.1) / 2, p.floor_t + 2,
        align=(Align.CENTER, Align.CENTER, Align.MIN))

    # ---- motor nest ----
    hw = p.mot_box_w / 2 + p.mot_fit
    rail_y0, rail_y1 = p.mot_y0 + 0.5, p.mot_y1 - 0.5
    for s in (1, -1):
        part += Pos(s * (hw / 2 + 2), (rail_y0 + rail_y1) / 2, 1) * Box(
            hw - 4, rail_y1 - rail_y0, 2)                    # rails under gearbox
        part += Pos(s * (hw + 1.2), (p.mot_y0 + p.mot_y1) / 2, (p.face_z - 0.5) / 2) \
            * Box(2.4, p.mot_box_l, p.face_z - 0.5)          # side walls
    front = Pos(0, p.mot_y0 - 1.2 - p.mot_fit, (p.face_z - 0.5) / 2) * Box(
        2 * hw + 4.8, 2.4, p.face_z - 0.5)                   # front locator wall
    rear = Pos(0, p.mot_y1 + 1.2 + p.mot_fit, (p.face_z - 0.5) / 2) * Box(
        2 * hw + 4.8, 2.4, p.face_z - 0.5)                   # rear thrust posts
    can_relief = Pos(0, p.mot_y1 + 1.2, p.can_axis_z) * Rot(90, 0, 0) * Cylinder(
        p.mot_can_d / 2 + 0.5, 20)
    part += front + (rear - can_relief)

    # saddle + strap pillars for the motor can
    sad_y = (p.mot_y1 + p.can_y1) / 2
    saddle = Pos(0, sad_y, 3) * Box(28, 14, 6)
    saddle -= Pos(0, sad_y, p.can_axis_z) * Rot(90, 0, 0) * Cylinder(
        p.mot_can_d / 2 + 0.1, 20)
    part += saddle
    for s in (1, -1):
        part += Pos(s * (p.mot_can_d / 2 + 5), sad_y, 12) * Cylinder(4, 24)
        part -= Pos(s * (p.mot_can_d / 2 + 5), sad_y, 24 - p.insert_hole_h) * \
            Cylinder(p.insert_hole_d / 2, p.insert_hole_h + 0.1,
                     align=(Align.CENTER, Align.CENTER, Align.MIN))

    # ---- lid screw bosses (BLIND heat-set pockets - sealed) ----
    for ys in p.screw_ys:
        for s in (1, -1):
            part += Pos(s * p.screw_x, ys, p.h_int / 2) * Cylinder(
                p.boss_d / 2, p.h_int)
            part -= Pos(s * p.screw_x, ys, p.h_int - p.insert_hole_h) * Cylinder(
                p.insert_hole_d / 2, p.insert_hole_h + 0.1,
                align=(Align.CENTER, Align.CENTER, Align.MIN))

    # ---- PG7 cable gland through the motor-end wall ----
    part += Pos(0, p.y_max - 1.2, 38) * Rot(90, 0, 0) * Cylinder(10, 4.4)
    part -= Pos(0, p.y_max + p.wall + 1, 38) * Rot(90, 0, 0) * Cylinder(
        p.gland_hole_d / 2, 12, align=(Align.CENTER, Align.CENTER, Align.MIN))

    # ---- blind vent pilot (drill through + fit M6 breather if wanted) ----
    part -= Pos(10, p.y_max + p.wall + 0.01, 52) * Rot(90, 0, 0) * Cylinder(
        p.vent_pilot_d / 2, 1.2, align=(Align.CENTER, Align.CENTER, Align.MIN))

    # ---- boat-mount ears (outside the sealed volume) ----
    for ey in (-19, 129):
        for s in (1, -1):
            ear = Pos(s * (p.half_w + p.wall + 5), ey, 0) * Box(10, 14, 6)
            ear -= Pos(s * (p.half_w + p.wall + 5), ey, 0) * Cylinder(
                p.ear_hole_d / 2, 10)
            part += ear
    return part


def lid():
    """Lid: flat plate, gear-side seal boss (pocket opens down), encoder
    bosses, blind-boss screw pattern. Prints top-face-down, no supports."""
    yc = (p.y_min + p.y_max) / 2
    L = p.y_max - p.y_min
    plate = Pos(0, yc, p.lid_z0 + p.lid_t / 2) * Box(
        p.inner_w + 2 * p.wall, L + 2 * p.wall, p.lid_t)
    plate = fillet(plate.edges().filter_by(Axis.Z), p.corner_r)
    # seal boss over the output hub
    boss = Pos(0, 0, p.top_boss_z0) * Cylinder(
        p.pocket_d / 2 + p.wall, p.lid_z0 - p.top_boss_z0 + 0.1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    part = plate + boss
    part -= Pos(0, 0, p.top_boss_z0 - 0.1) * Cylinder(       # seal pocket (down)
        p.pocket_d / 2, p.pocket_h + 0.1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    part -= Pos(0, 0, p.top_pocket_z1) * Cylinder(           # labyrinth bore
        37.0 / 2, p.lid_z1 - p.top_pocket_z1 + 1,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    # encoder bosses (M2 pilots) around the pinion axis
    board_top = p.gear_z1 + 4 + p.magnet_gap + p.enc_chip_h + p.enc_board_t
    for sx in (1, -1):
        for sy in (1, -1):
            bp = Pos(sx * p.enc_hole_pitch / 2, p.cd + sy * p.enc_hole_pitch / 2,
                     board_top)
            part += bp * Cylinder(3, p.lid_z0 - board_top,
                                  align=(Align.CENTER, Align.CENTER, Align.MIN))
            part -= bp * Cylinder(p.enc_hole_pilot / 2, 5,
                                  align=(Align.CENTER, Align.CENTER, Align.MIN))
    # screw holes + counterbores
    for ys in p.screw_ys:
        for s in (1, -1):
            part -= Pos(s * p.screw_x, ys, p.lid_z0 - 1) * Cylinder(
                p.m3_clear / 2, p.lid_t + 2,
                align=(Align.CENTER, Align.CENTER, Align.MIN))
            part -= Pos(s * p.screw_x, ys, p.lid_z1 - 2) * Cylinder(
                p.m3_cb_d / 2, 2.1, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return part


def strap():
    """Motor-can hold-down strap; screws into the two pillars."""
    sad_y = (p.mot_y1 + p.can_y1) / 2
    top_z = p.can_axis_z + p.mot_can_d / 2 + 4.5
    block = Pos(0, sad_y, (24 + top_z) / 2) * Box(
        2 * (p.mot_can_d / 2 + 5 + 4), 14, top_z - 24)
    block -= Pos(0, sad_y, p.can_axis_z) * Rot(90, 0, 0) * Cylinder(
        p.mot_can_d / 2 + 0.3, 20)
    for s in (1, -1):
        block -= Pos(s * (p.mot_can_d / 2 + 5), sad_y, 20) * Cylinder(
            p.m3_clear / 2, 60)
        block -= Pos(s * (p.mot_can_d / 2 + 5), sad_y, 24 + 4) * Cylinder(
            p.m3_cb_d / 2, 60, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return block


def gasket():
    """Flat lid gasket - print in TPU, or use as a cutting template for
    2 mm neoprene sheet."""
    yc = (p.y_min + p.y_max) / 2
    L = p.y_max - p.y_min
    ring = Rectangle(p.inner_w + 2 * p.wall - 0.4, L + 2 * p.wall - 0.4)
    ring = fillet(ring.vertices(), p.corner_r)
    inner = Rectangle(p.inner_w - 2 * p.rim_flange, L - 2 * p.rim_flange)
    inner = fillet(inner.vertices(), 0.6)
    sk = Pos(0, yc) * (ring - inner)
    for ys in p.screw_ys:
        for s in (1, -1):
            sk += Pos(s * p.screw_x, ys) * Circle(p.boss_d / 2 + 0.3)
            sk -= Pos(s * p.screw_x, ys) * Circle(p.m3_clear / 2)
    return Pos(0, 0, p.h_int) * extrude(sk, p.gasket_t)


# ============================================================
# BUILD + EXPORT
# ============================================================
def zero(part, flip=False):
    """Re-orient for printing: optional 180deg flip, then bed at z=0."""
    if flip:
        part = Rot(180, 0, 0) * part
    bb = part.bounding_box()
    return Pos(-((bb.min.X + bb.max.X) / 2) * 0, 0, -bb.min.Z) * part


if __name__ == "__main__":
    parts = {}
    print(f"centre distance {p.cd}  gear OD {p.gear_od}  interior "
          f"{p.inner_w:.1f} x {p.y_max - p.y_min:.1f} x {p.h_int}")
    parts["HubGear"] = (hub_gear(), True)     # print hex-down
    parts["Pinion"] = (pinion(), False)
    parts["Coupler"] = (coupler(), True)      # print hex-socket-down
    parts["Housing"] = (housing(), False)
    parts["Lid"] = (lid(), True)              # print top-face-down
    parts["Strap"] = (strap(), True)          # print plate-down
    parts["GasketTPU"] = (gasket(), False)

    os.makedirs(OUT, exist_ok=True)
    asm = []
    for name, (part, flip) in parts.items():
        bb = part.bounding_box()
        print(f"{name}: assembly bbox "
              f"{bb.max.X - bb.min.X:.1f} x {bb.max.Y - bb.min.Y:.1f} x "
              f"{bb.max.Z - bb.min.Z:.1f}  z {bb.min.Z:.1f}..{bb.max.Z:.1f}")
        if name == "Pinion":  # true position: on the motor axis, meshed
            part = Pos(0, p.cd, 0) * Rot(0, 0, 180.0 / p.teeth) * part
        asm.append(part)
        pp = zero(part, flip)
        export_stl(pp, os.path.join(OUT, f"{name}.stl"), tolerance=0.01,
                   angular_tolerance=0.1)
        export_step(pp, os.path.join(OUT, f"{name}.step"))
    export_stl(Compound(children=asm), os.path.join(OUT, "Assembly.stl"),
               tolerance=0.02, angular_tolerance=0.2)
    print("exported ->", OUT)
