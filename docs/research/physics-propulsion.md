# Physics verification: propulsion / actuation model

Scope: the thruster (actuation) model only — how a `MotorCommand` becomes a body-frame
generalized force/moment `tau = [Fx, Fy, N]`. The hull dynamics (mass, added mass, damping,
integration) are out of scope here and reviewed separately.

Files reviewed:

- `src/vanchor/sim/fossen.py` — `FossenBoat._tau()` (the actuator map) and `FossenParams`.
- `src/vanchor/core/config.py` — `BoatConfig` (`thruster_x_m()`, `thrust_yaw_ff_angle()`, mount/offset, thrust/steer limits).
- `src/vanchor/core/models.py` — `MotorCommand.clamped()`.

## The model under test

A single steerable trolling motor at body-frame offset `(x, y)` from the CG.

```
eff    = 1.0 if thrust >= 0 else reverse_efficiency          # 0.6
T      = command.thrust * eff * max_thrust_n                  # N, signed
delta  = command.steering * radians(max_steer_angle_deg)     # rad
Fx     = T * cos(delta)
Fy     = T * sin(delta)
N      = x * Fy - y * Fx
tau    = [Fx, Fy, N]
```

`command.thrust` and `command.steering` are each clamped to `[-1, 1]` by
`MotorCommand.clamped()` before use, so `|T| <= max_thrust_n` and `|delta| <= max_steer_angle`.

---

## 1. Vectored thrust with steering authority that scales with thrust — CORRECT

**Verdict: correct physics for a steerable trolling motor / outboard / azimuth (Z-drive) unit.**

A trolling motor steers by *physically rotating the whole propulsor* so the prop's thrust
vector points off-axis. The side force that turns the boat **is** a component of the propeller
thrust itself (`Fy = T sin delta`). If the prop is not turning, `T = 0`, there is no side force
and no yaw moment — exactly the code's behaviour. This is the defining characteristic of a
*vectored-thrust* / azimuth actuator and matches real boat handling: a trolling motor (or
outboard, or pod drive) has **no steering authority at idle**; you must give it throttle to
turn. Ardupilot's Rover models its boats with skid/vectored thrust on exactly this basis, and
Fossen's azimuth-thruster actuator model produces the body force purely from the thrust
magnitude and azimuth angle.

This is *deliberately and correctly different from a rudder model*. A rudder is a lifting
surface in the flow; its side force scales with the **square of the local flow speed** at the
rudder (`F_rudder ∝ ½ρ V² A C_L(alpha)`), not with engine thrust. A rudder therefore retains some
authority while coasting with the engine off (as long as the boat is making way through the
water), and conversely a rudder in still water with the engine off does nothing. The code
models a steerable propulsor, not a rudder, so tying steering authority to thrust (and *not*
to hull speed) is the right call. The docstring in `models.py` ("a rudder boat would realize
it with a rudder") correctly flags that the abstraction would need a different `_tau` for a
true rudder craft.

One subtlety worth noting (not a bug): a real outboard/trolling motor gets a small *added*
steering contribution from the propeller **slipstream washing over the lower unit/skeg**, and
at speed the hull's own flow adds a weak weather-vaning/rudder-like term. The code omits both.
For a slow (1.6 m/s) trolling-motor boat these are second-order; see §4.

## 2. Yaw-moment lever-arm formula `N = x·Fy − y·Fx` — CORRECT, signs check out

This is the standard rigid-body moment of a planar force applied at offset `(x, y)` from the
CG: `N = r × F |_z = x·Fy − y·Fx`. It is exactly the per-thruster row of Fossen's thrust
configuration matrix `T(alpha)`, where a thruster at `(lx, ly)` producing force `f` at azimuth
`alpha` contributes `N = f·(lx·sin alpha − ly·cos alpha) = lx·Fy − ly·Fx`. The code matches the
literature term-for-term.

Sign checks (body frame: +x forward/bow, +y starboard, +N = bow-to-starboard / clockwise
turn, consistent with the integrator's `heading += degrees(r)·dt` and "+heading = clockwise"):

- **Bow mount, x > 0, forward thrust, steer to starboard** (`delta > 0` ⇒ `Fy > 0`):
  `N = x·Fy > 0` ⇒ bow swings starboard. A bow-pull motor *pulls* the bow toward the steer
  side — correct, and matches the `fossen.py` docstring.
- **Stern mount, x < 0, forward thrust, steer to starboard** (`Fy > 0`):
  `N = x·Fy < 0` ⇒ bow swings to *port*. A stern motor steered to starboard pushes the stern
  to starboard, which yaws the bow to port — correct, and the bow/stern authority flip is
  exactly why `config.py` notes the helm carries a separate `steer_sign`.
- **Lateral offset at zero steering** (`delta = 0` ⇒ `Fx = T`, `Fy = 0`):
  `N = −y·Fx = −y·T`. A motor mounted to starboard (`y > 0`) under forward thrust gives `N < 0`,
  yawing the bow to port — i.e. thrust on the starboard side pushes that side forward and the
  bow swings away from the motor. Sign is physically correct.

The `thrust_yaw_ff_angle()` feed-forward that cancels this bias is also derived correctly:
setting `N = T·(x·sin delta − y·cos delta) = 0` gives `delta = atan2(y, x)`. The code uses
`atan2(y, |x|)`; using `|x|` (not signed `x`) is **intentional and correct** — the *physical*
deflection that opposes a given lateral offset has the same sign for a bow or stern mount, and
the separate `steer_sign` in the helm handles the bow/stern command-direction flip. No error.

Minor note: the offset used by physics is `thruster_x_m()` = `frac · length_m` with
`frac = 0.42` for bow/stern. With `length_m = 4.1` that is `±1.72 m`, matching
`FossenParams.thruster_x_m = 1.7`. Consistent. (These two values live in two places — config
vs. `FossenParams` default — so they can drift; worth a glance if either is ever retuned, but
not a physics error.)

## 3. Reverse as a flat 0.6 efficiency factor — reasonable simplification, with caveats

Modeling reverse as `T_reverse = 0.6 · T_forward` captures the first-order truth: a fixed-pitch
propeller (and the lower-unit/skeg shape) is optimized for forward flow and is markedly less
efficient pushing the other way. A reverse efficiency in the **0.5–0.7** band is a defensible
number; many fixed-pitch props deliver roughly 50–70% of forward thrust in reverse, so 0.6 is
a sensible midpoint. Acceptable for a sim.

What it omits:

- **Prop walk (paddle-wheel / transverse thrust).** A single fixed-pitch prop produces a
  *lateral* force in reverse (a right-handed prop kicks the stern to port in reverse), a real
  and often dominant low-speed handling effect. The model has no reverse-only side force, so
  the simulated boat backs up dead straight, which a real single-screw boat does not. This is
  the most defensible "missing realism" item, though it is small for the *bow-mounted* trolling
  motor this sim targets (prop walk matters most for a stern-mounted single screw used for
  docking), and trolling-motor handling guides rarely treat it as significant.
- **Asymmetric reverse steering.** Real reverse steering response differs from forward (flow
  separation off the now-leading edge of the skeg, etc.). The model keeps steering geometry
  identical in reverse; only the magnitude is scaled. Fine at this fidelity.

Recommendation: leave 0.6 as is; optionally document that prop walk is intentionally omitted.
A cheap, realistic upgrade would be a small reverse-only sway/yaw bias
(`Fy += k · |T| · (T < 0)`), but it is not required for autopilot development and would add a
tuning knob.

## 4. Missing effects — mostly fine to omit at this scale

- **Thrust deduction / hull interaction (`t`).** A propulsor accelerates water past the hull,
  lowering pressure aft and producing an effective thrust loss of ~5–20% (`(1−t)`). It is a
  near-constant scale factor here, and is already *implicitly absorbed* into the calibration:
  `max_thrust_n` is the **effective** thrust, and surge drag `x_u` is back-solved so full
  thrust reaches `max_speed_mps`. So thrust deduction is folded into the tuning — fine to omit
  explicitly.
- **Slipstream over a rudder / added flow.** Not applicable — there is no rudder. (Would only
  matter if a rudder model were added.)
- **Prop walk.** See §3 — the one omission with visible qualitative consequences (boat backs
  up perfectly straight), but minor for a bow-mount trolling motor.
- **Thrust loss at speed (J-dependence).** Real propeller thrust falls as advance ratio
  `J = Va/(nD)` rises: thrust is maximal at the bollard (`J = 0`) condition and declines toward
  zero as the boat speeds up. The model treats `T` as a function of command only, independent
  of `u`. **At this scale this is acceptable and arguably the right modeling choice**, because
  the speed envelope is tiny (0–1.6 m/s) and, crucially, the hull-drag side is *calibrated to
  the same operating point*: `x_u` is derived so that the **constant** full thrust balances
  drag at exactly `max_speed_mps`. Top speed is therefore correct by construction. Modeling
  `T(J)` would force re-deriving the drag balance and buy almost nothing over this narrow
  range. Worth a one-line comment, not a code change.

Net: the only physically *visible* omission is prop walk; everything else is either
inapplicable (rudder slipstream) or correctly absorbed into the calibration (thrust deduction,
J-dependence).

## 5. Realism of the numbers

- **`max_thrust_n = 250 N` (~56 lbf).** Correct units. A "55 lb thrust" trolling motor is rated
  in *pound-force*: 55 lbf = 245 N, so 250 N ≈ 56 lbf — spot on for a 12 V / 55 lb class
  motor. Good.
- **`max_steer_angle_deg`.** Two distinct values, and the split is sound: the **physics/manual**
  swing is `180°` (the head can rotate cable-wrap limited to ~185°), while the **autopilot**
  uses `autopilot_steer_deg = 35°`. The 35° authority the controller actually commands is a
  realistic, sensible vectoring limit (azimuth autopilots typically work within a modest cone
  to keep surge thrust up while turning). Using the *full* 180° as the physics clamp is also
  correct: a manually-driven trolling-motor head genuinely can point sideways/backwards. Note
  that `FossenParams.max_steer_angle_deg` defaults to `35.0`, whereas `BoatConfig` exposes
  `180.0` (physics) + `35.0` (autopilot) — make sure the integrator passes the intended limit
  into `FossenParams`; if the bare `FossenParams` default is used, the *manual* full-swing
  range is silently capped at 35°. Worth verifying the wiring (config plumbing, not a formula
  error).
- **Thrust → top-speed coupling.** Done the right way: `x_u` is derived from
  `max_thrust_n = (−x_u)·v_max + (−x_uu)·v_max²` so full thrust converges to `max_speed_mps`,
  with a guard against a misconfigured quadratic term driving `x_u` non-physically positive.
  This is a clean, physically grounded calibration: top speed is an emergent force balance, not
  a hard clamp. 1.6 m/s (~3.1 kn) is realistic for a ~4 m / 300 kg skiff on a 55 lb motor.

---

## Summary of issues + recommended fixes

| # | Item | Severity | Recommendation |
|---|------|----------|----------------|
| 1 | Vectored-thrust model (steering authority ∝ thrust, none at idle) | — | **Correct.** Right physics for a steerable trolling motor; deliberately and correctly *not* a rudder model. No change. |
| 2 | `N = x·Fy − y·Fx` lever arm; bow/stern signs; `atan2(y,\|x\|)` FF | — | **Correct**, matches Fossen's thrust-config matrix term-for-term. All sign cases verified. No change. |
| 3 | Prop walk in reverse (boat backs up perfectly straight) | Low (cosmetic) | Optional: add a small reverse-only sway/yaw bias, or just document the omission. Minor for a bow mount. |
| 4 | Reverse efficiency = 0.6 flat | — | Realistic (0.5–0.7 typical). Keep. |
| 5 | J-dependence / thrust loss at speed | — | OK to omit — drag is calibrated to the same operating point, so top speed is correct by construction. Add a one-line comment. |
| 6 | Thrust deduction / hull interaction | — | Correctly absorbed into the effective `max_thrust_n` + derived drag. No change. |
| 7 | Two sources of truth for `thruster_x_m` and `max_steer_angle` (config vs. `FossenParams` defaults; `FossenParams` defaults to 35° steer) | Low (config hygiene) | Verify the integrator passes `BoatConfig` geometry/limits into `FossenParams`; otherwise the bare-default `FossenParams` caps the *manual* swing at 35° and uses 1.7 m offset regardless of config. Not a formula bug. |

**Bottom line:** the actuation model is physically sound. The force decomposition, the
yaw-moment lever arm (and all four sign cases), the feed-forward derivation, the reverse
factor, the thrust→top-speed calibration, and the force magnitudes/angles are all correct and
consistent with marine maneuvering / actuator-model literature. No sign or derivation errors
were found. The only genuinely *missing* physical effect is reverse prop walk, which is minor
for the bow-mounted trolling motor this sim targets. The remaining notes are config-plumbing
hygiene, not physics.

---

## Sources

- Fossen, *Handbook of Marine Craft Hydrodynamics and Motion Control* (model framework `Mν̇ + C(ν)ν + D(ν)ν + g(η) = τ`; thrust configuration matrix `T(α)`): <https://fossen.biz/html/marineCraftModel.html>, <https://www.semanticscholar.org/paper/Handbook-of-Marine-Craft-Hydrodynamics-and-Motion-Fossen/31f03339cc9a7d37caa37672c522d200d136ebd3>
- Thrust allocation / per-thruster moment `N = f(lx·sin α − ly·cos α) = lx·Fy − ly·Fx` (Johansen & Fossen; DP azimuth-thruster allocation): <https://dynamic-positioning.com/proceedings/dp2008/thrusters_leavitt_pp.pdf>, <https://arxiv.org/html/2510.08119>, <https://www.sciencedirect.com/science/article/abs/pii/S0029801815002644>
- Vectored thrust vs. rudder steering: <https://ardupilot.org/rover/docs/rover-vectored-thrust.html>, <https://oceannavigator.com/vectored-thrust-for-voyagers/>, <https://en.wikipedia.org/wiki/Thrust_vectoring>
- Rudder force ∝ flow speed² and propeller slipstream dependence: <https://www.bergermaritiem.nl/en/propeller-nozzle/technology-configuration/nozzle-propeller-rudder-interaction>, <https://www.sciencedirect.com/topics/engineering/balanced-rudder>
- Prop walk / paddle-wheel effect (reverse asymmetry, single screw): <https://en.wikipedia.org/wiki/Propeller_walk>, <https://passagemaker.com/technical/how-to-use-prop-walk/>, <https://www.nauticed.org/images/courseart/Maneuvering/PropellerSideThrustDGerr.pdf>
- Propeller thrust vs. advance ratio J (max at bollard J=0, falls toward zero): <https://en.wikipedia.org/wiki/Advance_ratio>, <https://www.thecontactpatch.com/fluids/f1518-propeller-thrust>, <https://www.sciencedirect.com/topics/engineering/advance-coefficient>
- 55 lb trolling-motor thrust rating (55 lbf ≈ 245 N): <https://www.trollingmotors.net/blogs/selection/86933703-trolling-motor-thrust-guide>, <https://www8.garmin.com/manuals/webhelp/forcetrollingmotor/EN-US/GUID-9BEB843E-2C2B-450E-B9B2-F4C9F29D0C2E.html>
