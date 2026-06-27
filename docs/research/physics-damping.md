# Physics verification: hydrodynamic damping of the 3-DOF Fossen boat model

**Scope.** Rigorous review of the hydrodynamic damping `D(ν) = D_lin + D_quad(ν)` in
`src/vanchor/sim/fossen.py` against Fossen, *Handbook of Marine Craft Hydrodynamics
and Motion Control* (2nd ed., Wiley 2021) and standard marine-craft hydrodynamic-derivative
practice (SNAME 1950 prime system, Clarke 1983 / Norrbin regressions).

**Bottom line.** The *structure* of the linear damping is textbook-correct and the
positive-definiteness / sign convention is right. The model is, however, **not** a
faithful Fossen `D(ν)`: (1) the quadratic damping is a pure diagonal
`diag(X|u|u, Y|v|v, N|r|r)` and omits the *cross-flow-drag coupling* terms (`N_{|v|v}`,
`Y_{|r|r}`, etc.) that Fossen's nonlinear damping contains; (2) there is **no Coriolis /
centripetal matrix `C(ν)`** at all, which is a larger physical error than anything in the
damping itself; (3) several coefficients, when nondimensionalized, are realistic in *order
of magnitude* but the sway-set (`y_v`, `y_vv`) sits high and the surge linear drag comes out
implausibly large because of the thrust-balance derivation. Details and per-term verdicts
below.

---

## 0. Reference model (what Fossen actually prescribes)

For a 3-DOF surface craft (`ν = [u, v, r]`), Fossen writes

```
(M_RB + M_A) ν̇ + (C_RB(ν) + C_A(ν)) ν + (D_L + D_n(ν)) ν = τ
```

Linear damping (Fossen, Ch. 6; confirmed on fossen.biz/html/marineCraftModel.html):

```
            | -X_u    0      0   |
D_L = -     |  0     -Y_v   -Y_r |
            |  0     -N_v   -N_r |
```

— block-diagonal (surge decoupled), sway-yaw coupled, **not symmetric** in general
(`Y_r ≠ N_v` is allowed and usual).

Nonlinear (cross-flow-drag) damping, Fossen's standard quadratic form:

```
                | X_{|u|u}|u|             0                          0                       |
D_n(ν) = -      |  0        Y_{|v|v}|v| + Y_{|r|v}|r|   Y_{|v|r}|v| + Y_{|r|r}|r|            |
                |  0        N_{|v|v}|v| + N_{|r|v}|r|   N_{|v|r}|v| + N_{|r|r}|r|            |
```

i.e. the sway/yaw quadratic block is a **full 2×2** with off-diagonal `|·|` terms, derived
from the strip-wise cross-flow-drag integral
`Y_cf = -½ρ ∫ C_d(x)·T(x)·|v + x·r|(v + x·r) dx`, `N_cf = -½ρ ∫ C_d·T·x·|v+x r|(v+x r) dx`.

Added mass `M_A` is **symmetric** (`Y_{ṙ} = N_{v̇}`) for a body in an ideal fluid.

---

## 1. Linear damping `D_lin` — VERDICT: structurally CORRECT, magnitudes mixed

Code (`_build_matrices`):

```python
self._d_lin = -np.array([[x_u, 0, 0],
                         [0,  y_v, y_r],
                         [0,  n_v, n_r]])
```

with `x_u, y_v, n_r, y_r, n_v` all negative → `D_lin` has positive diagonal. Confirmed:

| Check | Result |
|---|---|
| Sign convention (negative derivs → resisting force) | **Correct.** Fossen convention exactly. |
| Block structure (surge decoupled, sway-yaw coupled) | **Correct.** Matches `D_L` above. |
| Placement of `y_r` (row sway, col yaw) and `n_v` (row yaw, col sway) | **Correct.** |
| Positive-definiteness | **Holds.** Symmetric-part eigenvalues = `[124.3, 256.4, 703.6]` > 0 with the derived `x_u`. The sway-yaw 2×2 `[[260,40],[40,700]]` is PD (eig `[256, 704]`). So damping always dissipates energy. |

**Symmetry question (asked explicitly).** `D_lin` is **not** required to be symmetric, and
Fossen's is generally asymmetric. Here `y_r = n_v = -40`, which makes it *accidentally*
symmetric. That is physically *acceptable but not specially motivated*: for a real hull
`Y_r` and `N_v` differ (e.g. Clarke gives `Y_r' ≈ +small or ≈0`, `N_v' < 0`, often opposite
signs — see §4). Setting them equal is a modeling simplification, not a law. It is not wrong,
but it is not "the physical value" either.

**Sign of the couplings.** Both are negative (`-40`). `N_v < 0` is physically right: positive
sway (drift to starboard) on a hull with the centre of lateral resistance abaft the CG produces
a *bow-up-into-the-drift* (negative) moment — this is the **weathercock / directional-stability**
term, and its sign here is correct and is what makes the boat tend to align with its velocity.
`Y_r < 0` (sway force from yaw rate) is plausible but its *real* sign is hull-dependent and
often near zero or positive for small craft; `-40` is a reasonable, conservative choice.

> Minor: the couplings being equal *and* equal to the added-mass couplings (`y_rdot = n_vdot
> = -40`) is a coincidence of round numbers, not a constraint. Fine for a game-grade sim.

---

## 2. Quadratic damping — VERDICT: FORM IS INCOMPLETE (missing cross-flow coupling)

Code (`_damping`):

```python
d_quad = -np.diag([x_uu*abs(u), y_vv*abs(v), n_rr*abs(r)])
return self._d_lin + d_quad
```

So `D_quad·ν = -[X_uu|u|u, Y_vv|v|v, N_rr|r|r]`. The diagonal `|ν|ν` terms themselves are a
correct Morison-type / cross-flow drag in each axis **in isolation**.

**What is missing (the question you asked).** Yes — per Fossen the nonlinear damping should
have **cross-axis quadratic terms**. The cross-flow-drag integral couples sway and yaw because
the local cross-flow velocity at station `x` is `v + x·r`:

- `N_{|v|v}` and `N_{|v|r}` — yaw moment that depends on `|v|` (a sideways-drifting hull with
  fore-aft asymmetry generates a quadratic restoring yaw moment). **This is the dominant
  nonlinear directional-stability term and it is absent.**
- `Y_{|r|r}`, `Y_{|r|v}` — sway force depending on `|r|`.

The present diagonal form is the common *engineering simplification* used in many USV sims
(including the Otter), so it is **defensible for a control-development sim**, but it is **not**
the full Fossen cross-flow-drag form and should be documented as a simplification. The most
physically important omission is the quadratic `N_{|v|v}|v|·v` weathercocking moment; with only
linear `n_v` the nonlinear directional restoring during hard sideslip is under-modeled.

> Recommendation (if higher fidelity is wanted): add at minimum a quadratic yaw-from-sway term
> `N_{|v|v}` (negative) so that strong leeway produces a growing align-to-flow moment. A full
> 2×2 quadratic block is the textbook answer but overkill for this application.

---

## 3. Surge linear-drag derivation — VERDICT: SOUND idea, but produces an over-large `x_u`

Code (`__post_init__`):

```python
x_u_mag = max_thrust_n / v_max - (-x_uu) * v_max     # = 250/1.6 - 20*1.6
self.x_u = -max(x_u_mag, 1.0)
```

Solving the steady balance `T_max = (-X_u)v_max + (-X_uu)v_max²` for `X_u` is **correct and
standard** — it guarantees the boat asymptotes to `v_max` at full thrust. Verified numerically:
at `v_max = 1.6`, linear drag = 198.8 N + quadratic 51.2 N = **250 N = max thrust**. 

**Failure modes:**

1. **The `≥ 1` guard is right and necessary.** If `(-X_uu)v_max ≥ T_max/v_max` the raw `x_u`
   goes negative (anti-damping → unstable). The clamp to `-1.0` prevents that. With the
   defaults it is not triggered (`x_u_mag = 124.25`). Good defensive code.
2. **Physical realism of the split.** The derivation forces *whatever linear drag is needed*
   to close the balance. With these defaults the drag at top speed is **80 % linear / 20 %
   quadratic**. For a planing/semi-displacement skiff at 1.6 m/s the *real* split is the other
   way around — viscous + wave drag is strongly super-linear, so a physically-tuned hull would
   be quadratic-dominated. The derived `x_u = -124` (prime `X_u' ≈ -0.0092`) is therefore on
   the **high side** and the model will feel "syrupy"/over-damped in surge at low speed and
   recover to `v_max` faster than a real hull. Not wrong (it still hits the right top speed),
   but the low-speed surge transient is too damped. A cleaner approach: pick `X_uu` from a
   target drag fraction and let `X_u` be small, or fit both to a drag curve.
3. Edge case: if a user sets `max_speed_mps` very small, `x_u_mag` blows up (250/v_max);
   if very large, the guard fires and top speed will then *exceed* `v_max`. Worth a comment.

---

## 4. Coefficient realism — nondimensional comparison

Nondimensionalized with the **SNAME / Fossen Prime-I (length) system** at `U = v_max = 1.6`,
`ρ = 1000`, `L = 4.1`. Forces ∝ `½ρL²U`, sway-yaw cross ∝ `½ρL³U`, yaw moment ∝ `½ρL⁴U`;
quadratic terms drop the `U`. Draft is not given; using displacement `∇ = m/ρ = 0.30 m³` and a
skiff `Cb ≈ 0.36` gives `T ≈ 0.12 m` (a shallow flat hull) — **note the Clarke comparison is
very sensitive to this assumed `T`.**

| Deriv (code) | Value (SI) | Prime (this model) | Typical / Clarke-predicted prime | Verdict |
|---|---|---|---|---|
| `X_u` (derived) | −124.3 | −0.0092 | surge linear small, ~−0.001…−0.005 | **high** (see §3) |
| `X_uu` | −20 | −0.0024 | O(−0.001…−0.01) | OK |
| `Y_v` | −260 | −0.0193 | Clarke `≈ −0.008` (at T=0.12); ships −0.005…−0.03 | **upper end / ~2× Clarke** but plausible |
| `Y_r` | −40 | −0.00073 | Clarke `≈ −0.002` (sign varies, often +) | order OK, sign defensible |
| `N_v` | −40 | −0.00073 | Clarke `≈ −0.0015` | **slightly low** but right sign |
| `N_r` | −700 | −0.0031 | Clarke `≈ −0.0015`; ships −0.002…−0.005 | **good** (factor ~2 of Clarke) |
| `Y_vv` | −180 | −0.0214 | cross-flow O(−0.01…−0.05) | OK, on high side |
| `N_rr` | −200 | −0.00142 | O(−0.001…−0.005) | **good** |
| `X_udot` (added) | −30 | −0.00087 | ~3–10 % of `m'` | OK (10 % of mass) |
| `Y_vdot` (added) | −250 | −0.0073 | sway added mass ≈ hull mass, O(−0.01) | OK (~83 % of mass) |
| `N_rdot` (added) | −180 | −0.00031 | O(−0.0005…−0.001) | reasonable |
| `Y_rdot=N_vdot` | −40 | — | symmetric ✓ | **correct** |

Notes / flags:

- **No coefficient is sign-wrong or wildly non-physical.** Everything is the right sign and
  within ~1–3× of regression values — good for a control-dev sim.
- The **biggest realism flag is the relative balance**, not individual values: `N_r' ≈ -0.0031`
  vs `Y_v' ≈ -0.0193` makes the hull *much* more resistant to sideways translation than to
  yawing, which combined with the missing `N_{|v|v}` is what the tuning leans on to "crab."
  That is a legitimate tuning choice, but it is tuning, not derived hydrodynamics.
- **Caveat on Clarke:** Clarke's regression is calibrated for *displacement ships* with
  normal `B/T` (≈2–4); here `B/T ≈ 14`, far outside its validity, so the Clarke column is an
  order-of-magnitude sanity check only, not a target. The agreement to within ~2× is actually
  better than one should expect.
- Added-mass `M_A` is **symmetric** (`Y_{ṙ} = N_{v̇} = -40`) — physically required and
  satisfied. Good.

---

## 5. `hull_tracking` scaling — VERDICT: directionally sensible, but inconsistent (couplings unscaled)

```python
k = ht * slender                 # ht ∈ [0.25,3], slender ∈ [0.7,1.6]
self.n_r  *= k;  self.n_rr *= k          # yaw damping
self.y_v  *= k;  self.y_vv *= k          # sway damping
```

- Scaling **`n_r`/`n_rr`** to set turn rate / directional stiffness is correct.
- Scaling **`y_v`/`y_vv`** to reduce leeway for a better-tracking hull is reasonable.
- **Problem 1 — couplings left unscaled.** Directional ("course") stability in linear theory is
  governed by the **stability discriminant**
  `C = N_r'(Y_v' − m') − N_v' Y_r'` (sign of the spiral root). It depends on **`N_v` and `Y_r`
  just as much as on `N_r`/`Y_v`.** Scaling `y_v` and `n_r` by `k` but holding `n_v`, `y_r`
  fixed **changes the coupling ratio** and so does *not* cleanly map to "directional stability";
  for large `k` it can even shift the stability margin in a non-obvious direction. To scale
  "tracking" coherently you should scale the **weathercock term `n_v`** too (a keelboat has a
  larger `|N_v|`), and arguably leave the bulk `y_v` partly alone (leeway is mostly about lateral
  area, which `slender` already proxies).
- **Problem 2 — sway and yaw scaled by the *same* `k`.** A deep-V/keel hull increases yaw
  damping *and* directional stiffness but does **not** necessarily increase pure sway drag by
  the same factor; coupling them means `hull_tracking` simultaneously stiffens turns and kills
  leeway in lock-step, which over-constrains the hull character.
- **Mutation caveat (code-quality, not physics):** `__post_init__` multiplies the dataclass
  fields **in place**, so constructing a second `FossenParams` from an already-scaled instance's
  values, or re-running post-init, would double-apply `k`. The default `k = 1.0` hides this, but
  any non-default `hull_tracking` makes the stored `n_r` etc. no longer the "raw" coefficients.

> Recommendation: to model "directional stability" coherently, scale `{n_r, n_rr, n_v}`
> (the yaw/weathercock set) by `k`, and scale the sway set `{y_v, y_vv}` by a *separate, milder*
> factor (or by `slender` only). Keep `y_r`/`n_v` consistent so the stability discriminant moves
> monotonically with `k`.

---

## 6. Most important issue overall: missing Coriolis/centripetal `C(ν)`

Although outside the literal "damping" target, it dominates the physics verdict and the prompt
asks about the crab/turn behavior the damping is tuned to produce. The code integrates

```
M ν̇ + D(ν) ν = τ
```

with **no `C(ν)ν` term.** Fossen's model has `(C_RB + C_A)ν`. In a turn this matters:

- Rigid-body centripetal sway force `≈ m·u·r`. At `u = 1.2 m/s`, `r = 0.32 rad/s` that is
  **≈ 115 N** — comparable to the sway *damping* and *larger* than the modeled coupling forces.
  This is the real physical cause of the outward "crab" in a turn; here it is faked entirely by
  the `y_r`/`y_v` damping coupling instead.
- The added-mass Munk moment `(Y_{v̇} − X_{u̇})·u·v` (destabilizing yaw moment from combined
  surge+sway) is also absent — this is what *reduces* directional stability at speed.

The model is energetically stable without `C(ν)` (which is why it "works"), but the
turn/sideslip coupling it produces is a *tuned imitation* of Coriolis via damping asymmetry, not
the real mechanism. For a control-development sim that may be acceptable; it should be documented,
and it explains why the damping couplings had to be hand-tuned.

---

## Summary of errors / recommended fixes (priority order)

1. **(Medium-High) No `C(ν)` Coriolis matrix.** The crab/turn coupling is faked through damping.
   Add `C_RB(ν) + C_A(ν)` per Fossen Ch. 6 if turn fidelity matters; then `y_r/n_v` can be
   returned to physical (smaller) values. — *Largest physical gap.*
2. **(Medium) Quadratic damping is diagonal-only.** Missing Fossen cross-flow-drag coupling,
   chiefly `N_{|v|v}` (quadratic weathercock) and `Y_{|r|r}`. Add at least `N_{|v|v} < 0`.
3. **(Medium) `hull_tracking` scales only `{n_r,n_rr,y_v,y_vv}`** and leaves the couplings
   `{n_v,y_r}` fixed → does not move "directional stability" (the `N_r(Y_v−m) − N_v Y_r`
   discriminant) coherently. Scale the yaw/weathercock set `{n_r,n_rr,n_v}` together; treat sway
   separately. Also: in-place field mutation risks double-applying `k`.
4. **(Low-Medium) Derived surge `x_u` is over-large** (80 % of top-speed drag is linear; real
   hull is quadratic-dominated). Top speed is correct but low-speed surge is over-damped.
   Prefer fitting `X_uu` to a target drag fraction and keeping `X_u` small.
5. **(Low) `Y_v'/Y_vv'` sit on the high side** and `N_v` slightly low vs regression; all signs
   correct, none non-physical. Acceptable as tuning given `B/T` is far outside Clarke validity.
6. **(None) Confirmed correct:** `D_lin` sign convention, structure, positive-definiteness;
   added-mass symmetry (`Y_{ṙ}=N_{v̇}`); the surge thrust-balance method and its `≥1` guard;
   the diagonal `|ν|ν` quadratic terms in isolation.

---

## Sources

- T. I. Fossen, *Handbook of Marine Craft Hydrodynamics and Motion Control*, 2nd ed., Wiley,
  2021 — Ch. 6 (Hydrodynamics, linear & nonlinear damping), App. D (nondimensionalization /
  prime system). [Wiley](https://www.wiley.com/en-us/Handbook+of+Marine+Craft+Hydrodynamics+and+Motion+Control,+2nd+Edition-p-9781119575054)
- Fossen, "Fossen's Marine Craft Model" — confirms `D = D_L + D_n(ν)` structure, asymmetric
  linear `D_L`, and the full 2×2 quadratic cross-flow block. [fossen.biz](https://fossen.biz/html/marineCraftModel.html)
- T. I. Fossen, R. Skjetne et al., "A Nonlinear Ship Manoeuvering Model: Identification and
  adaptive control…", *Modeling, Identification and Control* 25(1):3–27, 2004 — concrete 3-DOF
  damping/added-mass derivative set for a model ship. [mic-journal.no](https://www.mic-journal.no/PDF/2004/MIC-2004-1-1.pdf)
- D. Clarke, P. Gedling, G. Hine, "The Application of Manoeuvring Criteria in Hull Design Using
  Linear Theory," RINA Trans., 1983 — regression formulas for `Y_v', Y_r', N_v', N_r'`.
  [Semantic Scholar](https://www.semanticscholar.org/paper/The-Application-of-Manoeuvring-Criteria-in-Hull-Clarke-Gedling/4699a34f0362be5aa02fe26fe60babc5194ba441)
- "Refinement of Norrbin Model via Correlations between Dimensionless Cross-Flow Coefficient and
  Hydrodynamic Derivatives," *J. Mar. Sci. Eng.* 12(5):752, 2024 — cross-flow-drag /
  Norrbin coefficient ranges. [MDPI](https://www.mdpi.com/2077-1312/12/5/752)
- SNAME (1950) notation for marine vessels — prime-system normalization conventions. [SNAME](https://sname.org/marine-engineering)
