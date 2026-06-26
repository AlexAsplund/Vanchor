# Steering gearbox — 10-iteration design review

Each iteration is a 10-item "ensure" checklist, the critique of the **v1** design
against it, what **v2** (`steering.py`) changed, and what still remains. Honest
status: v2 fixes the clearly-wrong, clearly-correctable issues; items marked
*remain* need real-part validation (fits, torque, FEA, seal testing).

---

## 1 — Gear train & kinematics
Ensure: ①center distance = pinion+ring pitch radii (no bind) ②module sized for
root strength at stall torque ③reduction enough to swing the head vs water drag +
waves ④pinion teeth ≥ ~17 (no undercut at 20°) ⑤face width adequate, gears
coplanar in Z ⑥printed-tooth backlash clearance ⑦self-locking kept (no back-drive
from waves) ⑧ring gear concentric to the turret axis ⑨gear material plan
⑩production = true involute.
- **v1 problem:** module 1.5 trapezoidal teeth = weak + noisy; 4:1 maybe low for drag.
- **v2:** module 1.5→**2.0**, face 10→**12**, ring seated on a true body diameter + 6-bolt circle.
- **Remain:** involute profile, torque/FEA check, maybe 2-stage for more reduction.

## 2 — Bearings, shafts, axial location & thrust
Ensure: ①two *spaced* bearings to react the hanging-motor moment ②real shoulders
to locate each bearing ③defined thrust path for the head's weight → housing
④press-fit bores with shrink compensation ⑤sealed (2RS) bearings as a water
barrier ⑥turret axially trapped ⑦journal radial fit ⑧chamfer lead-in (no race on
a sharp printed ledge) ⑨bearing span vs lever arm ⑩marine corrosion (SS/sealed).
- **v1 problem:** turret OD 35.8 vs bearing ID 35 → **0.4 mm "shoulder"** (none); no
  thrust path; turret could slide out the top.
- **v2:** body **OD 44** → real shoulders both ends (**axial trap + thrust onto the
  bearing inner races**); seat shoulder + **chamfer** in base and lid.
- **Remain:** publish exact fits/shrink, thrust washer for heavy heads, SS bearings.

## 3 — Assembly & serviceability
Ensure: ①a feasible build/teardown order ②bearings press in before the turret
③ring gear bolts on after seating ④lid removable without wrecking the seal
⑤driver access ⑥tapped bosses / heat-set inserts for re-assembly ⑦motor swappable
without opening the wet box ⑧pinion fits the motor shaft ⑨encoder alignable
⑩nothing trapped.
- **v1 problem:** **no lid fastening at all** (gasket couldn't be compressed); motor
  lived *inside* the sealed cavity (no service without breaking the seal).
- **v2:** 4 **tapped lid bosses** + lid clearance holes; **external** motor mount.
- **Remain:** slot the motor mount for mesh/backlash; captive nut for the clamp bolt.

## 4 — Sealing & water management
Ensure: ①housing split sealed (gasket + even bolt load) ②turret rotary seal that
suits the surface finish ③sealed wire entry ④drainage/weep ⑤breather (no seal
pumping) ⑥socket water can't reach the gears ⑦seal surface smooth enough ⑧bolt
holes don't breach the cavity ⑨orientation sheds water ⑩honest IP rating.
- **v1 problem:** O-ring on a rough printed journal (leak/wear); **no drain, no
  breather**; lid unclamped.
- **v2:** gasket+bolts, O-ring groove kept, **weep hole** drains the socket,
  **breather** port; socket isolated from the gear cavity.
- **Remain:** a real **TC/lip oil seal on a smooth SS sleeve** for true ingress
  protection — this is **splash/rain rated, not submersible**.

## 5 — Sensor (AS5600) integration
Ensure: ①diametric magnet on-axis ②magnet→IC gap in spec (~0.5–3 mm) ③encoder
rigidly located ④no ferrous parts in the field ⑤I²C routed/strain-relieved
⑥absolute angle→steering, wrap in firmware ⑦magnet retained + diametric ⑧encoder
serviceable ⑨resolution ok (12-bit≈0.09°) ⑩board protected.
- **v1 problem:** standoff height put the IC **~5 mm above** the magnet → collision /
  out-of-spec gap.
- **v2:** floor **recess** sized so the IC sits **enc_gap = 1.5 mm** below the magnet;
  2 screw holes; magnet pocket on-axis.
- **Remain:** budget board thickness into the gap; keep fasteners non-ferrous near
  the magnet.

## 6 — Motor integration & packaging
Ensure: ①the real motor fits ②shaft reaches the pinion ③motor replaceable ④mount
matches the real hole pattern/pilot ⑤right-angle worm body clearance ⑥motor out of
standing water ⑦shaft entry sealed ⑧stiff mount ⑨wiring exit sealed ⑩self-locking
kept.
- **v1 problem:** the worm-gearmotor **body (46 mm) was longer than the 36 mm
  cavity** — it didn't physically fit, and sat inside the wet box.
- **v2:** motor moved **external, under the floor**; shaft pokes up through a sealed
  bore to the pinion; body hangs in air (serviceable).
- **Remain:** model the exact JGY-370 face; add a shaft bushing/seal at the bore; a
  drip shield over the motor.

## 7 — Printability & manufacturing
Ensure: ①walls ≥3 perimeters ②overhangs ≤45° or chamfered ③print orientation keeps
layer lines off the load path (teeth, clamp) ④no support in functional bores
⑤holes sized for shrink ⑥bridges within capability ⑦split parts to bed/support
⑧fits dialed via coupons ⑨marine UV-stable material ⑩gears printed solid/strong.
- **v1 problem:** bearing seats were sharp ledges (need support); module-1.5 teeth weak.
- **v2:** **seat chamfers**, bigger module, PETG/ASA recommended, nylon for the ring.
- **Remain:** coupon fit-tuning; print teeth on a helix for layer strength.

## 8 — Structural & load path
Ensure: ①motor moment reacted by the bearing span, not the gears ②housing stiff
(gears stay meshed) ③clamp grips without splitting ④boat-mounting feet sized for
steering+wave loads ⑤fillets at stress risers ⑥tooth root stress < limit at stall
⑦insert pull-out ok ⑧fatigue ⑨grounding-strike weak link/clutch ⑩no load through a
single layer-split feature.
- **v1 problem:** thin walls + no shoulders sent load into the 0.4 mm step / the
  gear teeth; **no boat-mount provision**.
- **v2:** thicker floor (4 mm), wall 3.2, **shoulders carry thrust**, spaced bearings
  carry the moment.
- **Remain:** add a **boat-mount flange/feet**, internal fillets, a shear/slip pin for
  grounding strikes, FEA.

## 9 — Tolerances, fits & adjustment
Ensure: ①bearing-seat press allowance (shrink) ②socket slip-fit (+0.3–0.5) ③gear
backlash ④mesh adjustability ⑤O-ring squeeze % ⑥gasket groove = cord size ⑦lid lip
clearance (bolts set compression) ⑧thread/insert hole sizes ⑨clamp slit width ⑩magnet
light press.
- **v1 problem:** nominal everywhere; no adjust slots; seal sizes not tied to
  standard parts.
- **v2:** parameterized clearances (`bore_clear`, `seat_chamfer`, O-ring/gasket
  params, lip clearance).
- **Remain:** motor-mount **slots** for backlash, tie O-ring/gasket to standard
  sizes, publish a tolerance table + test print.

## 10 — Electronics, thermal, safety & docs
Ensure: ①H-bridge rated for stall + fused ②cable-wrap limit in firmware (±185°) +
hard-stop fallback ③loss-of-feedback failsafe ④thermal/duty cycle (no PLA creep)
⑤sealed connector (IP) ⑥reverse-polarity/ESD ⑦manual override (declutch) ⑧stated IP
+ seal service interval ⑨integrates with the controller steering/telemetry ⑩full
BOM/drawings/firmware.
- **v1 problem:** no limits/failsafes/override documented; PLA would creep in the
  sun.
- **v2:** documented closed-loop + cable-wrap + self-locking hold; ASA/PETG; BOM +
  this review.
- **Remain:** hard end-stop feature, **manual declutch knob**, finalize fuse/driver
  spec, IP-rating test.

---

### Net v2 changes
Module 2 + wider gears · real bearing shoulders & thrust path · **external** motor
mount (the v1 motor literally didn't fit) · 4 tapped lid bolts to actually
compress the gasket · coordinated **1.5 mm** encoder air-gap · weep + breather ·
print chamfers · thicker floor. Trade-off: the box grew (module 2 → ~202×142 mm) —
drop to module 1.5 / fewer ring teeth if you want it smaller.

### Top remaining (need a real prototype)
Boat-mount flange · proper lip seal on an SS sleeve for true IP · involute gears in
nylon · motor-mount backlash slots · manual declutch · torque/FEA validation ·
coupon-tuned fits.
