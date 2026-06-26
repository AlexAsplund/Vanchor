# Steering / azimuth gearbox — BOM, build & integration

A closed-loop "servo" that swings the trolling motor for steering. Quiet,
self-locking, cheap, and 3D-printable for a prototype. Parametric source:
`cad/steering.py` (build123d) → STEP/STL in `cad/out/steering/`. Renders in
`cad/out/renders/`.

Reduction 4:1 (pinion 15T → ring 60T, module 1.5), turret OD ~35.8 mm for a
1" (25.4 mm) motor shaft, housing ~154 × 109 × 39 mm.

## How it works
- The motor's 1" shaft **slides down into the turret socket** (blind bore) and a
  **top split-clamp** (one M5 bolt) grips it.
- The turret rides on two **6807-2RS** bearings and carries the **ring gear**.
- A **12 V brushed worm gearmotor** drives the **pinion** into the ring gear to
  rotate the turret ≈ ±185° (cable-wrap limited).
- An **AS5600 magnetic absolute encoder** reads a diametric magnet in the
  turret's solid base → absolute steering angle for closed-loop control.
- **Sealed**: gasket groove between housing halves, an **O-ring rotary seal** on
  the turret's upper journal, and a **cable gland** for the wiring.

## Why these parts (cheap + reliable)
| Part | Choice | Why | ~€ |
|---|---|---|---|
| Drive motor | **12 V worm gearmotor**, JGY-370 class (e.g. 10–50 RPM) | **Quiet** (no stepper whine), **self-locking** (holds steering with no power → no holding current, safer), high torque, ~€10 | 10 |
| Position sensor | **AS5600** module + 6 mm **diametric magnet** | 12-bit (4096) **absolute** angle over I²C, non-contact (no wear), no homing, reads true steering angle after all gearing | 3 |
| Motor driver | **DRV8871** or **BTS7960** H-bridge | Cheap PWM brushed-DC driver; BTS7960 if you want >3 A | 3–6 |
| Main bearings | **6807-2RS** ×2 (35×47×7) | Sealed (doubles as a water barrier), smooth, cheap | 2×2 |
| Rotary seal | **O-ring** ~Ø35×2 (NBR) | Seals the spinning turret | <1 |
| Lid gasket | 2 mm cord / flat gasket in the groove (or RTV bead) | Seals the housing split | <1 |
| Cable gland | **PG7** | Sealed wire entry | 1 |
| Gears | Printed now (PETG/PLA); order in **nylon (SLS/MJF)** later | Strength for production | — |
| Fasteners | M3 (lid + motor + encoder), M5 (shaft clamp), heat-set inserts | — | few |
| Structure | 3D-printed (PETG recommended for outdoor/UV/temp) | Prototype | — |

> The included gears use a **simplified trapezoidal tooth** (robust to print,
> fine for low-speed/high-reduction steering). For production, switch to a true
> **involute** profile (e.g. order printed in nylon) — the rest of the design is
> unchanged.

## Print & assembly
1. Print `turret_holder`, `housing_bottom`, `housing_top` in **PETG** (walls ≥3
   perimeters); print `ring_gear` + `pinion` solid (or order in nylon).
2. Press the two 6807 bearings into the base seat and lid recess. Fit the O-ring
   in the turret groove.
3. Heat-set M3 inserts into the motor-mount bosses, encoder standoffs, and lid
   bolt holes.
4. Bolt the AS5600 board to the floor standoffs, centred under the turret; press
   the diametric magnet into the turret base pocket (≈1–2 mm air gap to the IC).
5. Bolt the gearmotor under its boss; fit the pinion (grub screw on the flat).
6. Drop the turret (with ring gear bolted on) onto the lower bearing; mesh the
   pinion; lay the gasket; fit the lid over the upper bearing/seal; bolt down.
7. The motor shaft slides into the turret socket from the top; tighten the M5
   clamp.

## Control / firmware (closes the loop)
The unit is driven exactly like the rest of Vanchor-NG's steering:
- Firmware (Arduino/Pi) reads the **AS5600 angle over I²C**, compares it to the
  **target steering angle** from the controller, and drives the gearmotor with a
  **PID → PWM** through the H-bridge. The worm **self-locks** when the PWM stops,
  so it holds position with no current.
- Track total rotation to enforce the **±185° cable-wrap limit** (stop / reverse
  before wrap).
- This is the real hardware behind `hardware/serial_devices.py:SerialMotorController`:
  it already emits a normalized steering command; map it to a target angle
  (`steering * boat.max_steer_angle_deg`) and let this closed loop track it. The
  AS5600 reading can also feed back true steer angle to the telemetry/HUD.
- The simulator already models this actuator (steering rate-limited to the
  head's rotation speed — set `boat.max_steer_rate_dps` to this gearmotor's
  output RPM × 6).

## Regenerate / customise
Edit the `P` dataclass in `cad/steering.py` (shaft Ø, gear teeth/module,
reduction, bearing, motor mount, wall thickness, seals…) and run:

    python cad/steering.py        # STEP + STL
    python cad/make_renders.py    # PNG print-screens
