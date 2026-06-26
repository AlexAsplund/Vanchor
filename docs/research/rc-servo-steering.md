# RC Servo Steering vs. Custom Gearbox — Research Report

**Question:** Should Vanchor-NG steer the trolling-motor shaft with a powerful
off-the-shelf RC servo (+ a 3D-printed mount/bracket) instead of the custom
build123d worm-gear "steering gearbox" currently in `cad/`?

**Context:** Vanchor-NG is software-first. The steering actuator only has to
rotate the trolling-motor shaft left/right. An Arduino talks serial to the Python
backend and can emit standard hobby **PWM** (so an RC servo is trivial to drive)
or step/dir. Our HAL already models a closed-loop steering actuator with angle
feedback (`steering{commanded,target_deg,angle_deg,rate_dps,range_deg,wrap_pct}`),
slew limiting, and a `±185°` cable-wrap concept — so either actuator drops into
the same software.

**TL;DR:** For a DIY trolling-motor autopilot the off-the-shelf path is the
**proven, dominant** community solution: a ~**60 kg·cm waterproof RC servo**
(ANNIMOS/ZOSKAY/Wishiot **DS5160 SSG** class, ~$35–55) clamped to the shaft with a
printed bracket. It is cheaper, far faster to build, and has ample torque headroom
for a small-boat trolling motor. The custom worm gearbox wins only on
self-locking hold-with-no-power, true submersible sealing, and unlimited rotation.
**Recommended:** start with a 60 kg waterproof RC servo on PWM; keep the custom
gearbox as the "v2 / production" path. A **Feetech STS3215/ST3215 serial-bus
servo** is the interesting middle option if you want native position feedback over
serial, but its torque is marginal — see §6.

---

## 1. The "Outdoor Engineering" reference and the DIY scene

The phrase "Outdoor Engineering" did not resolve to a single findable
YouTube/store channel in searches (it may be a small channel, a renamed account,
or the user's paraphrase of the build's *style*). However, the build it describes
— **a 3D-printed bracket that clamps a high-torque RC servo to the trolling-motor
shaft for GPS autopilot steering** — is a very well-established DIY pattern, and
the searches converge hard on a specific lineage. The "Outdoor Engineering" mount
is almost certainly a remix/relative of the **Theo Frieling** design below (the
de-facto origin of the printed-servo-clamp approach), or one of the Amazon/Etsy
kits derived from it.

### 1a. Theo Frieling — "Minn Kota Steering" (the origin design)
- Thingiverse: <https://www.thingiverse.com/thing:4713499>
- A belly-boat angler designed (in Fusion 360) a printed clamp that grips the
  Minn Kota shaft and lets an RC servo swing the whole head.
- **First prototypes used an 18 kg servo; he found it too weak and upgraded to a
  60 kg AliExpress servo.** Used on a **Minn Kota Endura MAX 40 lb**, shaft
  diameter **29 mm**. **No removal of the motor head required** — the bracket
  clamps over the existing shaft.
- Printed in **PolyLite ASA / ABS / PET-G** (UV/heat-stable for outdoor use).
- This is the most-cited, most-remixed printed-servo trolling-motor steering
  design and the clear ancestor of the whole DIY scene.

### 1b. "Kayak Trolling Motor Steering Assembly — Newport Vessels 36 lb" (Dorikin350z)
- Thingiverse: <https://www.thingiverse.com/thing:6197159>
- A "heavily modified remix of Theo Frieling's design." Fits the **Newport Vessels
  Kayak Series 36 lb** (and similar motors with **26–29 mm shafts**). Designed
  around a **robust 60 kg servo**. Reported **~50 miles of real use** with no
  issues — good evidence a 60 kg servo survives the duty cycle.

### 1c. Commercial / kit versions of the same idea
- **Amazon "Trolling Motor Servo Steering Kit for Kayak or Boat"** (B0CQ1BCM5L):
  a printed bracket + steel-wire linkage that fits the **Minn-Kota C2** motor,
  **servo not included**, designed for a **60 kg servo** (180° rotation). Sellers
  will adapt to Traxxis / MotorGuide R3 shafts on request.
  <https://www.amazon.com/Trolling-Motor-Servo-Steering-Kayak/dp/B0CQ1BCM5L>
- Review write-up explicitly recommending the **ANNIMOS 60 kg** servo for that
  kit: <https://ferronusa.com/trolling-motor-steering-kit/>
- YouTube "Wireless Servo Steering for Trolling Motors":
  <https://www.youtube.com/watch?v=nT5gpVJkwss> (servo + printed mount, RC-style
  wireless steering — the visual archetype of the "Outdoor Engineering" build).

### 1d. Other well-known DIY trolling-motor-autopilot steering builds
- **Vanchor (the original, AlexAsplund)** — the project we're rewriting. Notably
  it used a **3D-printed gearbox driven by a stepper motor** over the shaft, *not*
  an RC servo. Raspberry Pi + e-compass + marine GPS, ~$300 total.
  Hackaday writeup: <https://hackaday.com/2021/09/24/open-source-autopilot-for-cheap-trolling-motors/>
  · Repo: <https://github.com/AlexAsplund/Vanchor>
- **ArduPilot / ArduRover "Trolling Motor" steering** — the Rover community drives
  trolling-motor steering from an autopilot; the steering output is a standard
  servo/PWM channel. <https://ardupilot.org/rover/docs/trolling-motor.html>
- **Linear-actuator outboard autopilots** (e.g. Firgelli / "DIY boat autopilot
  under $350", Jack Edwards' Arduino design) — different mechanism (push-pull a
  tiller) but the same Arduino-PWM control philosophy.
  <https://www.firgelliauto.com/blogs/news/making-a-boat-autopilot-using-a-linear-actuator-for-under-350>
- Forum threads (Arduino, RCGroups, fishing forums) overwhelmingly land on either
  a **high-torque RC servo** or a **12 V worm gearmotor + feedback** for steering.

**Takeaway:** the off-the-shelf path the user is pointing at is real, mature, and
field-proven. The community-standard part is a **~60 kg·cm waterproof RC servo
clamped to the shaft with a printed bracket.**

---

## 2. Torque requirement — how much do we actually need?

We need to rotate the *whole lower unit + shaft* about the vertical steering axis.
The resisting torque has three parts:

1. **Static/seal/clamp friction** in the motor's own steering bushing. On a cheap
   trolling motor this is modest — a person steers it one-handed with a tiller.
   Call it on the order of **0.5–2 N·m**.
2. **Hydrodynamic (turning) load** at speed: as you yaw the lower unit, the prop
   wash + the foil-shaped lower unit generate a side force whose line of action is
   offset from the steering axis, producing a self-aligning / resisting moment.
3. **Dynamic/transient loads:** waves slapping the unit, weeds, a grounding strike.
   These can momentarily spike far above the steady value (this is why commercial
   units add a **torque clutch** — see the patents below).

### Back-of-envelope for the hydrodynamic term
Small trolling motors (30–55 lb thrust) at trolling speed:
- Thrust ≈ 30–55 lbf ≈ **130–250 N**.
- Through-water speed is low (~1–2 m/s); the lower unit is small (lower-unit chord
  ~0.04–0.06 m, submerged span ~0.1–0.15 m → reference area ~0.005–0.01 m²).
- Side force when yawed a few degrees is a *fraction* of thrust; the moment arm
  (distance from the steering axis to the side-force center of pressure) is small
  (a few cm) because the lower unit is roughly *on* the steering axis.
- Estimated steady steering moment from hydrodynamics: **order 1–3 N·m** at
  trolling speed for a 30–55 lb motor, rising with speed² and with thrust.

### Sensible target torque
- **Steady requirement: ~2–4 N·m (≈ 20–40 kg·cm).**
- **With margin for waves/friction/transients: design for ~5–8 N·m
  (≈ 50–80 kg·cm)** continuous-ish, and tolerate brief stalls above that.
- The community's empirical answer matches this perfectly: **18 kg·cm was too
  weak; 60 kg·cm works well** (Theo Frieling, §1a). 60 kg·cm ≈ **5.9 N·m** of
  *stall* torque — and you steer slowly, so you're using stall-region torque.

### Important caveats (from the patent literature)
Trolling-motor steering must survive **rocks, weeds, groundings** — load can spike
far past the steady number. Commercial designs handle this with a **torque clutch
that decouples the steering motor above a threshold** and/or **current-feedback
stall management** (US 11,008,085; US 10,717,509; US 4,515,567 et al., found via
patent search). **Whatever actuator we pick, the steering linkage/clamp should
have a deliberate weak link or slip clutch for grounding strikes** — an RC servo's
plastic horn or a printed shear feature can serve this role; the custom gearbox
review (`cad/steering_REVIEW.md` item 8/10) already flags adding a shear/slip pin.

**Conclusion:** a **60 kg·cm-class servo has comfortable headroom** (≈1.5–3× the
steady requirement) for a small trolling motor, which is exactly why the DIY scene
settled on it. A 20–35 kg servo is marginal-to-adequate for the smallest motors in
calm water but leaves little margin. A serial-bus 30 kg servo (Feetech) is at the
low edge.

---

## 3. Candidate actuators (real specs + rough prices)

### A. High-torque **waterproof RC servos** (PWM) — the mainstream choice
| Model | Stall torque | Voltage | Waterproof | Gears | Rotation | Interface | ~Price |
|---|---|---|---|---|---|---|---|
| **ANNIMOS / ZOSKAY / Wishiot / RCmall DS5160 (SSG)** | 58 kg·cm @6V, 65 @7.4V, **70 @8.4V** | 6–8.4 V (HV) | **IP67** | stainless steel | 180° or **270°** variants | PWM | **~$35–55** |
| **Savöx SW-0241MGP** ("1/5 scale") | 30 kg·cm @6V, **40 @7.4V** | 6–7.4 V HV | waterproof | metal | ~180° | PWM | ~$70–120 |
| **Savöx SW-2290SG** (brushless "Monster") | 45 @6V, 55 @7.4V, **70 @8.4V** | 6–8.4 V HV | **IP67**, alu case, brushless | steel | ~180° | PWM | ~$180–230 |
| **Feetech FT5330M** | **35 kg·cm** @7.4V | 6–7.4 V | IP54 only | metal, alu case | 180° | PWM | **~$20–25** |
| **ANNIMOS 35 kg / 20 kg DS3218** | 20–35 kg·cm | 6.8–7.4 V | "waterproof" (splash) | metal | 180°/270° | PWM | ~$15–30 |

Notes:
- The **DS5160 (and its rebadges ANNIMOS / ZOSKAY / Wishiot / RCmall / Miuezuth)**
  is *the* community part: cheap, **IP67**, 60–70 kg·cm, stainless gears, dual ball
  bearings, **270°** option (useful for a wide steering sweep). Best
  price/torque/sealing combination here.
- **Savöx** = premium reliability/precision and genuine marine-grade sealing, at
  3–5× the price. The brushless **SW-2290SG** is overkill on torque but excellent
  if you want best-in-class durability and don't mind ~$200.
- **Feetech FT5330M** is only **IP54** (splash) — fine inside a dry enclosure, not
  for direct exposure.

### B. **Sail/winch servos** (multi-turn) — only if you need lots of rotation
| Model | Torque | Turns | Notes | ~Price |
|---|---|---|---|---|
| **Hitec HS-785HB** | ~13 kg·cm (183 oz·in @6V) | **3.5 turns** | classic multi-turn drum winch; torque is **low** | ~$45–60 |
| **SPARKHOBBY SW22HV** | 14–24.6 kg·cm | 1.5–7 turns programmable | **waterproof**, HV, metal gear | ~$30–45 |
| **Turnigy TGY-6114MD** | 14.5 kg·cm | drum | budget sail winch | ~$20 |

Verdict: multi-turn is nice for cable-wrap-free unlimited steering, but **winch
servos are torque-starved (~13–25 kg·cm)** — below our comfortable target. Only
worth it if you specifically want >360° rotation and can gear it down (which kills
the torque further). Generally **not recommended** here; ±180–270° from a standard
high-torque servo covers normal steering.

### C. **Smart serial-bus servos** (native position feedback) — best fit for our stack
| Model | Stall torque | Voltage | Interface | Feedback | Gears | ~Price |
|---|---|---|---|---|---|---|
| **Feetech STS3215 / ST3215 (30 kg variant)** | **30 kg·cm @12V** (19.5 @7.4V) | 6–12.6 V | **TTL serial bus, 1 Mbps**, daisy-chain | **12-bit absolute magnetic encoder; pos/speed/voltage/current/temp/load** | metal | **~$15–30** |
| Feetech ST3215-C044 (heavy-duty) | 27.4 kg·cm | 7.4 V | TTL serial bus | abs encoder | metal | ~$15–25 |
| **Dynamixel XM430-W350** | **4.1 N·m ≈ 42 kg·cm** | 12 V (max 14.8) | TTL/RS-485, Protocol 2.0 | 12-bit abs encoder, current-based torque | metal | ~$280 |
| **Dynamixel MX-64 (T/AT)** | ~6 N·m ≈ 60 kg·cm | 12 V | TTL/RS-485 | 12-bit abs, 360° | metal | **~$305–425** |

Notes:
- **Feetech ST/SC-bus servos are the standout for us:** they give **closed-loop
  absolute position + current/load/temp telemetry over serial** for ~$20 — exactly
  the kind of feedback our HAL already exposes (`steering.angle_deg`). Caveat:
  **30 kg·cm @12V is at the *low* end of our target** (~3 N·m), so headroom is thin
  for a bigger motor or rough water. They are **not waterproof** (need a dry box).
  Spec/price: <https://www.waveshare.com/st3215-servo.htm> ·
  RobotShop 12V 30 kg: <https://www.robotshop.com/products/feetech-12v-30kgcm-magnetic-encoding-servo-sts3215>
- **Dynamixel** gives the best feedback/control quality and 42–60 kg·cm, but at
  **$280–425 each** it blows the "cheap DIY autopilot" budget. Not waterproof.

### D. Brushless / industrial / gearmotor options (for completeness)
- **12 V worm gearmotor (JGY-370 class) + AS5600 encoder + H-bridge** — *this is
  exactly the current custom-gearbox plan* (`cad/steering_BOM.md`): quiet,
  **self-locking** (holds with zero current), high torque after reduction, ~$10
  motor + $3 encoder. Highest torque/$, but you build the housing/gears.
- True industrial BLDC servos (e.g. iFOC/ODrive + planetary, or a NEMA-class
  closed-loop stepper) — far more torque and capability than needed; cost,
  complexity, and waterproofing make them inappropriate for a hobby build.

---

## 4. Integration with the Vanchor-NG stack

- **PWM (RC servo): trivial.** The Arduino already can emit standard 50 Hz RC PWM
  (1000–2000 µs). Map the controller's normalized steering command →
  `pulse = 1500 + steering * span`. A high-torque HV servo draws real current
  (DS5160 stall ≈ 6–9 A) — **power it from a dedicated 2S LiPo / 7.4 V BEC, NOT
  the Arduino 5 V rail**, common ground to the Arduino signal. One signal wire +
  power; done.
  - **Open-loop caveat:** a plain PWM servo gives *no position feedback*. Our HAL
    models `steering.angle_deg` as feedback; with a dumb servo we'd either run it
    open-loop (trust the commanded angle) or add an external **AS5600 on the
    shaft** to close the loop (recommended for autopilot trust + cable-wrap
    enforcement). The custom gearbox already includes this AS5600.
- **Serial-bus servo (Feetech/Dynamixel): best feedback fit.** These return
  absolute angle + load + temp over a serial bus, which maps directly onto our
  existing `SerialMotorController` / steering-feedback telemetry. We already speak
  serial to the Arduino; either let the Arduino bridge the servo bus, or drive the
  Feetech bus directly from the Pi/host via a TTL adapter. This gives **real
  closed-loop steering with native feedback and no extra encoder.** Main downside:
  Feetech torque is marginal; Dynamixel is expensive.
- **Coupling to the shaft.** All DIY builds use a **printed split-clamp around the
  shaft** (1" / 25.4 mm or 29 mm typical) with the servo body fixed to a bracket
  that reacts against the boat/transom mount. The servo arm drives the clamp via a
  short link or directly. Our `BoatConfig.shaft_dia_mm` + `steer_reduction`
  already parameterize this. Add a 1.5–4:1 reduction (printed gear/arm geometry)
  if you want to trade servo speed for torque and finer resolution.
- **Cable-wrap / rotation limit.** RC servos are limited-angle (180/270°), which
  is *fine* — it inherently bounds rotation, no cable-wrap logic strictly needed
  if range ≤ ~300°. Our `steer_range_deg` / `wrap_pct` already model this. A
  multi-turn winch servo or the custom gearbox needs explicit `±185°` wrap
  enforcement in firmware (already designed).
- **Waterproofing (marine).** Use an **IP67 servo (DS5160 / Savöx)** if exposed.
  Mount the servo **above the waterline**, drive the clamp through a short link so
  the servo body stays dry-ish. Seal connectors (heat-shrink / dielectric grease),
  use **stainless/SS hardware**, and print the bracket in **ASA or PETG** (UV/heat
  stable — PLA creeps and sags in sun). IP54 servos (Feetech FT5330M) and all
  serial-bus servos (Feetech/Dynamixel) must live in a **dry enclosure**.

---

## 5. Trade-offs: off-the-shelf RC servo (+printed mount) vs. the custom gearbox

| Dimension | **Off-the-shelf RC servo + printed bracket** | **Custom build123d worm gearbox (`cad/`)** |
|---|---|---|
| **Up-front cost** | **~$35–55** (DS5160) + a little filament | ~$25–40 in parts (worm gearmotor $10, AS5600 $3, bearings, H-bridge, seals) — cheaper *parts* but… |
| **Build time / effort** | **Hours.** Print a bracket, clamp it on, wire PWM. Lowest-risk path to "it steers." | **Days–weeks.** Print/iterate housing+gears, press bearings, heat-set inserts, tune mesh/backlash, seals, firmware PID. Not yet prototyped. |
| **Torque headroom** | **Good** (60–70 kg·cm stall ≈ 1.5–3× steady need) | **Excellent** (worm reduction multiplies a cheap motor; easily highest torque) |
| **Backlash / precision** | Servo internal gear backlash is small; clamp/link adds some. 12-bit servos exist (DS5160 ~ good). | Printed-gear backlash is the weak point until ordered in nylon/involute; otherwise fine for slow steering. |
| **Hold with no power** | **No** — servo must hold position actively (draws current at stall against load). | **Yes** — worm gear is **self-locking**, holds steering with zero current (safer, lower power, no thermal stress). Key advantage. |
| **Position feedback** | None on plain PWM (add AS5600) / **native on Feetech/Dynamixel** | **Native AS5600** absolute encoder built in |
| **Waterproofing** | **IP67 servo** off the shelf (DS5160/Savöx) = easiest real marine sealing | Designed but **splash/rain only today**; true IP needs a lip seal on an SS sleeve (open item in review) |
| **Reliability / serviceability** | Servo is a sealed COTS unit — **swap in 5 min** if it dies; no custom spares | More moving custom parts; serviceable by design (external motor mount) but you maintain it |
| **Rotation range** | 180–270° (fine); multi-turn only with winch servos (low torque) | ±185° w/ cable-wrap logic; could be extended |
| **Grounding-strike protection** | Servo horn / printed link can be a **deliberate weak link** (cheap to replace) | Needs an explicit shear/slip-pin clutch (open item) |
| **Fit for a hobby/DIY autopilot** | **Excellent** — proven by the whole community, lowest barrier | Great *engineering*, but heavier lift; better as the "v2 / refined" path |

---

## 6. Recommendation

**For Vanchor-NG, adopt the off-the-shelf RC servo as the primary/default steering
actuator, and keep the custom gearbox as the optional "v2 / production-grade"
path.** Rationale: it's the field-proven community standard, it has ample torque
for a small trolling motor, it gets you steering in hours instead of weeks, and it
matches the project's software-first ethos (the hard part is the autopilot, not
the gearbox). Our HAL already abstracts the actuator, so this is a device-swap in
`app.py`, not a rewrite.

### Concrete shortlist
1. **Primary (best value): ANNIMOS / ZOSKAY / Wishiot / RCmall *DS5160 SSG*,
   270° version, ~$35–55.** IP67, ~70 kg·cm @8.4V, stainless gears, dual ball
   bearings. Drive with standard PWM from the Arduino; power from a 2S LiPo/BEC.
   This is the same class of servo Theo Frieling and the Newport-Vessels remix
   proved over real miles. **Add an external AS5600 on the shaft** to close the
   loop (matches our feedback telemetry) and to enforce a software steering-angle
   limit.
2. **Feedback-first alternative: Feetech STS3215 / ST3215 (30 kg @12V serial
   bus), ~$15–30.** Native absolute-position + load/current/temp over serial — the
   cleanest fit for our serial stack and `steering.angle_deg` telemetry, no extra
   encoder. **Caveat: 30 kg·cm (~3 N·m) is marginal** for anything but small
   motors in calm water, and it's **not waterproof** (dry box required). Great for
   bench/prototype and small builds; gear it down or step up if torque is tight.
3. **Premium/marine: Savöx SW-2290SG (brushless, IP67, ~70 kg @8.4V) ~$200**, or
   **SW-0241MGP (~40 kg, ~$80)** — if you want best-in-class durability and sealing
   and the budget allows.
4. **Keep:** the **custom worm gearbox** for when you want **self-locking
   zero-power hold**, true submersible sealing, and unlimited rotation — i.e. the
   "do it properly for production" option.

### Caveats / wiring checklist
- **Limited-angle, not continuous-rotation.** Use a standard (180°/270°) servo,
  not a continuous-rotation/winch servo, unless you deliberately want multi-turn —
  winch servos are torque-starved (~13–25 kg·cm).
- **Holding torque under load:** a plain servo *actively* fights steering load and
  can draw stall current / heat up if it's continuously loaded off-center. The
  self-aligning hydrodynamic moment is small at trolling speed, so this is usually
  fine, but it's the one place the worm gearbox is genuinely better.
- **Stall current is large:** DS5160 stall ≈ 6–9 A. **Dedicated battery/BEC, fat
  wires, common ground**, and a fuse. Never feed it from the Arduino's regulator.
- **Add a mechanical weak link / slip clutch** in the linkage for grounding
  strikes (the servo horn or a printed shear feature) — see the steering patents
  and `cad/steering_REVIEW.md` §8/§10.
- **Print the bracket in ASA/PETG**, stainless hardware, seal connectors. Mount
  the servo as high/dry as practical even if it's IP67.
- **Software:** map normalized steering → PWM pulse; if open-loop, trust commanded
  angle but prefer adding the AS5600 to feed real `angle_deg` back into the
  existing closed-loop telemetry + cable-wrap/range logic.

---

## Sources

DIY designs / references:
- Theo Frieling, "Minn Kota Steering" (origin printed servo clamp; 18 kg → 60 kg; 29 mm shaft; ASA/PETG): <https://www.thingiverse.com/thing:4713499>
- "Kayak Trolling Motor Steering Assembly — Newport Vessels 36 lb" (remix, 60 kg servo, ~50 mi tested): <https://www.thingiverse.com/thing:6197159>
- Amazon "Trolling Motor Servo Steering Kit" (printed bracket, fits Minn-Kota C2, 60 kg servo): <https://www.amazon.com/Trolling-Motor-Servo-Steering-Kayak/dp/B0CQ1BCM5L>
- Steering-kit review recommending ANNIMOS 60 kg: <https://ferronusa.com/trolling-motor-steering-kit/>
- YouTube "Wireless Servo Steering for Trolling Motors": <https://www.youtube.com/watch?v=nT5gpVJkwss>
- Original Vanchor (stepper-driven printed gearbox): <https://github.com/AlexAsplund/Vanchor> · Hackaday: <https://hackaday.com/2021/09/24/open-source-autopilot-for-cheap-trolling-motors/>
- ArduPilot Rover "Trolling Motor": <https://ardupilot.org/rover/docs/trolling-motor.html>
- Linear-actuator boat autopilot under $350: <https://www.firgelliauto.com/blogs/news/making-a-boat-autopilot-using-a-linear-actuator-for-under-350>

Servos:
- ANNIMOS / DS5160 60 kg IP67 (180°): <https://www.amazon.com/ANNIMOS-Digital-Voltage-Stainless-Waterproof/dp/B07KTS4L94> · (270°): <https://www.amazon.com/ANNIMOS-Digital-Voltage-Stainless-Waterproof/dp/B07KTSCN4J>
- Wishiot DS5160SSG (IP67, 270°): <https://www.amazon.com/Wishiot-DS5160SSG-Digital-DC6-8-4V-Waterproof/dp/B08HYX5SX3>
- ANNIMOS datasheet PDF: <https://m.media-amazon.com/images/I/81EFGw8qkhL.pdf>
- Savöx SW-0241MGP (waterproof, 40 kg @7.4V): <https://www.savoxusa.com/products/savsw0241mgp-waterproof-1-5th-scale-digital>
- Savöx SW-2290SG (IP67 brushless, 70 kg @8.4V): <https://www.savoxusa.com/products/new-monster-performance-high-voltage-high-torque-standard-size-servo>
- Feetech FT5330M (35 kg, IP54, PWM): <https://www.robotshop.com/products/feetech-180-degrees-digital-servo-74v-35kg-cm-ft5330m>
- Feetech ST3215 serial bus servo (30 kg @12V, abs encoder): <https://www.waveshare.com/st3215-servo.htm> · 12V 30 kg: <https://www.robotshop.com/products/feetech-12v-30kgcm-magnetic-encoding-servo-sts3215>
- Dynamixel XM430-W350 (4.1 N·m, 12V, serial): <https://emanual.robotis.com/docs/en/dxl/x/xm430-w350/> · <https://www.robotis.us/dynamixel-xm430-w350-r/>
- Dynamixel MX-64 (~6 N·m, serial, ~$305): <https://emanual.robotis.com/docs/en/dxl/mx/mx-64-2/> · <https://robotis.us/dynamixel-mx-64r/>

Sail/winch servos:
- Hitec HS-785HB (3.5-turn winch): <https://www.superdroidrobots.com/shop/item.aspx/hs-785-sail-winch-servo/600>
- SPARKHOBBY SW22HV (waterproof multi-turn winch): <https://www.amazon.com/SPARKHOBBY-Waterproof-Steering-Programmable-Digital/dp/B0D7C5DKG2>

Torque / load (patents on trolling-motor steering, torque clutch, stall management):
- US 11,008,085 (steering w/ stall prevention): <https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/11008085>
- US 10,717,509 (damage-prevention feedback): <https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/10717509>
- US 4,515,567 (trolling motor foot control): <https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/4515567>

Internal:
- Current custom gearbox design + BOM + review: `cad/steering.py`, `cad/steering_BOM.md`, `cad/steering_REVIEW.md`
