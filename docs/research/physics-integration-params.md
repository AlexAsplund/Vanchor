# Physics Verification: Numerical Integration & Parameter Realism

**Scope:** Rigorous review of the 3-DOF Fossen maneuvering model in
`src/vanchor/sim/fossen.py` (`step()`, `FossenParams`, `__post_init__`) and the
simulation time-step / loop rate in `src/vanchor/sim/simulator.py`.

**Target craft:** 4.1 m × 1.7 m, 300 kg skiff with a single bow-mounted
~55 lbf steerable trolling motor.

**Date:** 2026-06. **Verdict up front:** the integrator is correct and
comfortably stable at the chosen `dt`; the physical parameters are realistic and
internally consistent. No required fixes. One *optional* fidelity improvement
(Coriolis term) and a couple of minor notes are listed at the end.

---

## 0. The time-step

`Simulator.__init__` defaults `physics_hz = 20.0`, and `run()` steps with
`period = 1/physics_hz`, so:

```
dt = 1/20 = 0.05 s   (50 ms, 20 Hz)
```

`step(period * time_scale)` is the only call site; deterministic tests pass an
explicit `dt`. The analysis below uses the live `dt = 0.05 s`.

---

## A. Numerical integration

### A.1 The scheme as written

In `FossenBoat.step()` (fossen.py:232–266):

```
tau    = _tau(command)                       # body force/moment [X, Y, N]
D(nu)  = D_lin + D_quad(nu)                   # linear + |nu| quadratic damping
nu_dot = M^-1 (tau - D(nu) nu)               # solve accelerations at old nu
nu    <- nu + nu_dot * dt                     # advance velocities (Euler)
heading <- heading + r_new * dt               # uses UPDATED r  -> semi-implicit
NED vel  = R(heading_new) * [u_new, v_new] + drift
position <- position + NED_vel * dt
```

This is **semi-implicit (symplectic) Euler**: the dynamics (`nu`) are advanced
with an explicit Euler step, then the kinematics (heading, position) are advanced
using the **already-updated** velocity. This is exactly the structure used in
Fossen's own `PythonVehicleSimulator` (the "otter" model the docstring cites) and
is standard practice for marine-craft simulators.

**Order of accuracy:** first order globally (local truncation error O(dt²)). This
is true both for the explicit-Euler velocity update and for the overall scheme.

**Correctness of the M⁻¹ solve:** correct. `M = M_rb + M_a` is built once and
inverted once in `_build_matrices`; `M` is constant (added mass and rigid-body
inertia don't depend on state), so caching `M^-1` is valid and efficient. The
mass matrix is

```
M = [[330,   0,    0  ],
     [  0, 550,   40  ],
     [  0,  40,  672.5]]          (kg, kg, kg·m²)
```

symmetric and positive-definite (eigenvalues all > 0), so `M^-1` is well-behaved.

### A.2 Stability analysis (the core question)

Linearize about low speed (quadratic damping → 0). The velocity ODE is
`nu_dot = -A nu` with `A = M^-1 D_lin`. Explicit Euler gives the amplification
`nu_{k+1} = (I - A dt) nu_k`; it is stable iff every eigenvalue λ of `A`
satisfies `|1 - λ dt| ≤ 1`, i.e. `0 < λ dt < 2`.

Computed eigenvalues of `A = M^-1 D_lin` (1/s) and the corresponding decay time
constants τ = 1/λ:

| mode (approx) | λ (1/s) | τ = 1/λ (s) | λ·dt @ dt=0.05 | `|1 − λ·dt|` |
|---|---|---|---|---|
| surge       | 1.041 | 0.96 | 0.0520 | 0.948 |
| sway-ish    | 0.471 | 2.12 | 0.0235 | 0.976 |
| yaw-ish     | 0.377 | 2.66 | 0.0188 | 0.981 |

The **fastest** mode is surge, τ ≈ 0.96 s. The stability limit for explicit Euler
is `dt < 2/λ_max ≈ 1.92 s`. At `dt = 0.05 s` the largest `λ·dt ≈ 0.052`, which is
**~37× inside** the stability boundary (1.92 s vs 0.05 s ⇒ ~38× margin on dt).
All amplification factors `|1 − λ dt|` are ≈ 0.95–0.98 < 1: every mode decays
monotonically with no oscillation, no overshoot, no growth.

**Worst case (quadratic damping engaged).** At top speed the quadratic terms
*stiffen* the system, raising the effective rates. Evaluating the damping
Jacobian near `u≈1.6, v≈0.5, r≈0.4`:

- Surge effective rate (including the `2·x_uu·|u|` self-derivative of the
  quadratic term): `(124.25 + 2·20·1.6)/330 ≈ 0.57 /s` → `λ·dt ≈ 0.029`.
- Full-matrix eigenvalues rise to ≈ {1.16, 0.64, 0.47} /s → max `λ·dt ≈ 0.058`,
  fastest τ ≈ 0.86 s.

Even in the worst case `λ·dt < 0.06`, far from the 2.0 limit. Quadratic damping
is unconditionally dissipative here, so it can only *help* stability, never hurt.

**Conclusion (A.2):** explicit/semi-implicit Euler is **comfortably stable** at
`dt = 0.05 s` — roughly a 30–40× margin to the stability boundary. The model is
nowhere near stiff at this resolution.

### A.3 Update ordering / coupling error

- **Heading uses the updated `r`** (`r_new` after the `nu += nu_dot*dt` line) and
  **position uses the updated `u, v` and updated heading.** This is the correct
  semi-implicit ordering and is what gives the scheme its good (symplectic-like)
  energy behavior. There is **no stale-velocity bug**.
- A residual O(dt) coupling error remains intrinsic to first-order integration:
  within a step the heading is treated as piecewise-constant when rotating
  body→NED (position uses `heading_new` but not the *average* heading over the
  step). During a hard turn at the max ~0.32 rad/s yaw rate this is a heading
  change of only `0.32·0.05 ≈ 0.016 rad ≈ 0.9°` per step — a sub-percent
  cross-track error per step that does not accumulate pathologically. Negligible
  for an autopilot operating at walking-pace speeds.

### A.4 Would RK4 / exact discretization help?

- **RK4:** would cut truncation error from O(dt) to O(dt⁴) but at 4× the cost per
  step and **no stability benefit is needed** (the system is not stiff at this
  dt). At τ_fast ≈ 0.9 s and dt = 0.05 s you already resolve the fastest dynamics
  with ~18 points per time-constant; Euler's per-step error is already tiny. RK4
  is **not warranted**.
- **Exact (matrix-exponential) discretization** of the linear part
  (`Phi = exp(-A dt)`) would make the *linear* dynamics exact for any dt and is
  cheap (precompute one 3×3 matrix). But the damping is genuinely nonlinear
  (quadratic in `nu`) and `tau` varies with the command, so it would only be
  exact for the linear sub-problem. Marginal benefit at this dt.

**Recommendation (A):** **Keep semi-implicit Euler at 20 Hz.** It is correct,
stable with a large margin, and accurate to well within the model's own
parameter uncertainty. Do **not** add RK4 — it would add cost and complexity for
no observable accuracy gain at this dt. If you ever (a) raise damping/added-mass
stiffness by ~30×, or (b) drop to <5 Hz, re-check `λ·dt`; otherwise no change.

> Optional, non-blocking: the model **omits the Coriolis/centripetal matrix
> `C(nu)`** (`tau = M nu_dot + (C + D) nu` in full Fossen form). This is a
> deliberate, defensible simplification at trolling-motor speeds — the
> `C`-coupling (e.g. `m·u·r` cross terms) scales with speed×rate and is small
> here (order `300·1.6·0.32 ≈ 150 N` cross-coupling vs sway damping of similar
> magnitude — not negligible during a hard turn, but it mainly affects the
> *crab/sideslip* detail, not stability or the gross trajectory). Adding `C(nu)`
> is the single highest-fidelity improvement if more realistic turn dynamics are
> wanted later; it does **not** change the integration-stability story.

---

## B. Parameter realism

All values computed from the defaults in `FossenParams` with `hull_tracking=1.0`
and the default L/B, where `hull_k = 1.0` (so the directional coefficients are
applied at face value).

### B.1 Mass & yaw inertia

- `mass = 300 kg` for hull + 55 lbf motor + battery + 1 person on a 4.1 m skiff
  is reasonable (a bare 14 ft aluminum jon hull is ~70–90 kg; add a person ~80 kg,
  battery ~25 kg, gear, and motor → 300 kg is a sensible loaded figure).
- `Iz = m/12·(L²+B²) = 300/12·(4.1²+1.7²) = 492.5 kg·m²` (uniform rectangle).
  Radius of gyration `kz = sqrt(Iz/m) = 1.281 m`, so **kz/L = 0.3125**.

  The ITTC recommended default when yaw gyradius is unknown is **kz ≈ 0.25·Lpp**,
  and the commonly cited band for hulls/yachts is **0.25–0.30·L** (yacht
  added-resistance practice uses 0.20/0.25/0.30·LWL). 0.3125 is **just above** the
  top of that band — the uniform-rectangle assumption slightly *over*-distributes
  mass to the ends vs a real hull whose mass (engine, batteries, occupants) sits
  nearer amidships, which would push kz/L toward ~0.25–0.28. The discrepancy is
  small (~10–25% on Iz) and **conservative** (a bit more rotational inertia →
  slightly lazier, safer turn response). **Acceptable**; if you want textbook
  realism, replace the rectangle formula with `Iz = m·(0.27·L)² ≈ 368 kg·m²`.

### B.2 Added mass

Displacement check: a 300 kg boat displaces ≈ 0.29 m³ (seawater) / 0.30 m³
(fresh) of water — so the **displaced-water mass ≈ the boat mass ≈ 300 kg**.
That is the natural yardstick for sway added mass.

| coeff | value | as fraction | literature range | verdict |
|---|---|---|---|---|
| `x_udot` (surge added mass) | −30 | 10% of m | surge ≈ 5–10% of m (planing craft up to ~10%) | **OK** (top of range, fine for a displacement skiff) |
| `y_vdot` (sway added mass) | −250 | 83% of m | sway can approach the displaced-water mass; sway added-mass coeff ~0.4–1.3, "10–50% of displacement" for ships, higher for beamy shallow hulls | **OK / plausible** — a beamy flat hull shoving water sideways legitimately approaches ~1× displaced mass |
| `n_rdot` (yaw added inertia) | −180 | 37% of Iz | yaw added inertia typically a meaningful fraction of Iz (no single rule; commonly tens of %) | **OK** |
| `y_rdot` / `n_vdot` (coupling) | −40 each | symmetric off-diagonal | small relative to diagonals; equal off-diagonals → symmetric M_a (correct) | **OK** |

Notes:
- The sway added mass being the largest term (and ≈ the displaced-water mass) is
  **physically correct**: lateral motion of a hull entrains the most water. 83% is
  on the higher side but well within the realistic envelope for a wide, shallow
  flat-bottom skiff (high beam-to-draft ratio).
- The added-mass matrix is symmetric (`y_rdot = n_vdot`), as required by potential
  theory. Good.

### B.3 Damping → top speed and turn rate

Both targets fall out of the chosen coefficients and check out:

- **Top speed.** `x_u` is *derived* in `__post_init__` so that
  `max_thrust = (−x_u)·v_max + (−x_uu)·v_max²`. Solving the surge balance
  `20·u² + 124.25·u − 250 = 0` gives `u = 1.6 m/s` exactly — i.e. the construction
  is self-consistent and full thrust converges to the target top speed.
  `x_u = −124.25` (derived), `x_uu = −20`.
  **1.6 m/s ≈ 3.6 mph**, which matches real-world reports of a 55 lbf motor on a
  *loaded* skiff/jon boat (3–4 mph; lighter kayaks reach ~5 mph). **Realistic.**
- **Thrust.** `max_thrust_n = 250 N`. 55 lbf = 55·4.448 = **245 N**, so 250 N is
  the correct order (≈ +2%). **OK.** `reverse_efficiency = 0.6` (props bite less
  astern) is a sensible heuristic.
- **Full-thrust / full-steer turn rate.** At δ=35°, T=250 N: `Fy = 143 N`,
  `N = thruster_x_m·Fy = 1.7·143 = 244 N·m`. Steady yaw from
  `n_r·r + n_rr·r|r| = N` → `200·r² + 700·r − 244 = 0` → `r = 0.319 rad/s =`
  **18.3 deg/s**, squarely inside the **12–25 deg/s** target band. **Realistic.**

### B.4 Parameter-realism summary table

| Parameter | Model value | Derived/implied | Literature / target | Verdict |
|---|---|---|---|---|
| mass | 300 kg | — | loaded 4.1 m skiff | OK |
| Iz | 492.5 kg·m² | kz/L = 0.3125 | 0.25–0.30·L (ITTC 0.25) | OK, slightly high — optional `Iz≈0.27L²·m≈368` |
| x_udot | −30 | 10% m | surge 5–10% m | OK |
| y_vdot | −250 | 83% m ≈ displaced-water mass | sway → ~displaced mass | OK (high but plausible) |
| n_rdot | −180 | 37% Iz | tens of % of Iz | OK |
| y_rdot=n_vdot | −40 | symmetric | small coupling | OK |
| max_thrust_n | 250 N | — | 55 lbf = 245 N | OK |
| max_speed_mps | 1.6 (=3.6 mph) | top-speed balance exact | 55 lbf skiff 3–4 mph | OK |
| turn rate | — | 18.3 deg/s @ full | 12–25 deg/s | OK |
| Coriolis C(nu) | omitted | — | present in full Fossen | acceptable simplification |

---

## C. Issues & recommended fixes (prioritized)

**Required fixes: none.** The integrator and parameters are sound.

**Optional / nice-to-have, in priority order:**

1. **(Fidelity, low effort) Yaw inertia is ~10–25% high.** The uniform-rectangle
   `Iz = m/12·(L²+B²)` gives kz/L = 0.31, just above the 0.25–0.30 band. If you
   want textbook realism use `Iz = m·(0.27·L)² ≈ 368 kg·m²`. Current value is
   conservative (lazier turns) so this is purely cosmetic.
2. **(Fidelity, medium effort) No Coriolis/centripetal `C(nu)`.** Adding the
   Fossen `C(nu)·nu` term would improve hard-turn / crab realism (the `m·u·r`,
   `m·v·r` cross-coupling is order ~100–150 N during an aggressive turn). It does
   **not** affect stability and is unnecessary for autopilot-grade behavior at
   these speeds. Worth it only if turn-dynamics fidelity becomes a goal.
3. **(Integration) Keep semi-implicit Euler at 20 Hz.** Do **not** switch to RK4
   (no benefit at this dt; 4× cost). If the model is ever made ~30× stiffer or run
   below ~5 Hz, re-verify `λ·dt < 2`.

**Things explicitly verified correct (no action):** M⁻¹ solve and caching;
semi-implicit update ordering (heading & position use updated ν); symmetric,
positive-definite mass matrix; symmetric added-mass coupling; self-consistent
top-speed derivation of `x_u`; thrust/speed/turn-rate all in realistic ranges.

---

## Citations

- ITTC Recommended Procedures 7.5-02-07-04.4 — yaw/pitch radius of gyration
  default 0.25·Lpp: <https://ittc.info/media/4180/75-02-07-044.pdf>
- Fossen, *Handbook of Marine Craft Hydrodynamics and Motion Control* / Fossen's
  Marine Craft Model (M = M_rb + M_a, C(ν), D(ν) structure):
  <https://fossen.biz/html/marineCraftModel.html>
- Fossen "otter" USV reference implementation (semi-implicit Euler in a marine
  sim): cybergalactic/PythonVehicleSimulator (cited in `fossen.py` docstring).
- Added-mass coefficient overview (surge ≈ 5–10% m for planing craft; sway
  coefficient ~0.4–1.3; sway/heave ~10–50% of displacement):
  <https://www.sciencedirect.com/topics/engineering/added-mass-coefficient> ;
  surge added mass of planing hulls (≤10% m):
  <https://www.researchgate.net/publication/335019928>
- Yacht radius-of-gyration practice (kyy = 0.20/0.25/0.30·LWL):
  <https://navalapp.com/calculations/added-resistance-in-waves-knowing-yacht-radius-of-gyration-calculation/>
- 55 lbf trolling-motor real-world speeds (skiff/jon boat 3–4 mph, kayak ~5 mph):
  <https://www.vatrerpower.com/blogs/news/speed-of-a-55-lb-thrust-trolling-motor>
  ; Minn Kota thrust/speed guidance:
  <https://minnkota-help.johnsonoutdoors.com/hc/en-us/articles/4413536408343-Calculate-Speed-and-Determine-Required-Thrust>
</content>
