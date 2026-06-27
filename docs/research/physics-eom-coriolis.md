# Physics verification: the missing Coriolis–centripetal term C(ν) in `fossen.py`

**Scope.** Rigorous verification of the equations-of-motion *structure* of the
3-DOF (surge–sway–yaw) maneuvering model in
`src/vanchor/sim/fossen.py` (plus `src/vanchor/core/models.py` for the
state/command/Environment vocabulary). No code was edited.

**References used**

- Fossen, *Handbook of Marine Craft Hydrodynamics and Motion Control*, 2nd ed.,
  Wiley 2021 — Ch. 3 (rigid-body kinetics, Coriolis matrix; Theorem 3.2 on
  `M → C(ν)`), Ch. 6/7 (maneuvering models, the 3-DOF horizontal model). Online
  summary: <https://fossen.biz/html/marineCraftModel.html>.
- `cybergalactic/PythonVehicleSimulator` (MIT), the "otter" USV the docstring
  cites: `vehicles/otter.py` and the `m2c(M, nu)` helper in `lib/gnc.py`.
  - otter EOM: `nu_dot = Dnu_c + Minv @ (tau + tau_damp + tau_crossflow - C @ nu_r - G @ eta + g_0)`
    with `C = CRB + CA`, `CA = m2c(MA, nu_r)`.
  - `m2c` 3-DOF branch (verbatim):
    ```python
    else:   # 3-DOF model (surge, sway and yaw)
        C = np.zeros((3,3))
        C[0,2] = -M[1,1] * nu[1] - M[1,2] * nu[2]
        C[1,2] =  M[0,0] * nu[0]
        C[2,0] = -C[0,2]
        C[2,1] = -C[1,2]
    ```

---

## TL;DR verdict

**Dropping C(ν) is a real modelling error, not a benign low-speed
simplification, for this boat.** This vehicle's whole purpose is *turning*
(anchor-hold, heading-hold, orbit), and the Coriolis–centripetal term is a
*turning* effect (it scales with yaw rate `r`). In a hard turn the missing
term **dominates the sway force balance**: it is ~3.9× the size of the sway
damping force, and it carries the *opposite sign* to what the damping coupling
produces. As a result the simulated boat crabs the **wrong way** during turns.

- The code's `step()` solves `M·ν̇ + D(ν)·ν = τ` and omits `C(ν)·ν`.
- The otter it is modelled on does **not** omit it (`- C @ nu_r` is in the EOM).
- The "crab during a turn" the docstring attributes to *damping/added-mass
  coupling* is physically wrong: the dominant, correctly-signed crab comes from
  the **added-mass Coriolis term C_A(ν)**, which is exactly the term that was
  dropped.

**Recommendation: add C(ν) = C_RB(ν) + C_A(ν), computed by the same `m2c`
formula the otter uses, evaluated on the code's own M_RB and M_A.** Exact
matrices below.

---

## 1. The canonical model vs. what the code implements

Canonical 3-DOF maneuvering model (Fossen 2021, Ch. 6), ν = [u, v, r]ᵀ:

```
M·ν̇ + C(ν)·ν + D(ν)·ν = τ ,   M = M_RB + M_A,   C = C_RB + C_A
```

`fossen.py:238-241` implements:

```python
nu_dot = self._mass_inv @ (tau - damping @ nu)        # M·ν̇ + D·ν = τ
```

— the `C(ν)·ν` term is absent. (The module docstring at lines 12–16 even writes
the equation *without* C, while the otter EOM it claims to follow *includes*
it.) So this is a deliberate-looking omission, not a transcription slip, but it
is not justified at this boat's operating point.

---

## 2. Is dropping C(ν) legitimate here? — Quantified, and NO.

C(ν) is genuinely negligible only when **both** the speeds and (crucially) the
yaw rate are tiny, i.e. near straight-line creeping. This autopilot spends much
of its life in **sustained turns** (anchor-hold corrections, orbit, heading
captures) at u ≈ 1.35–1.6 m/s and r up to ~0.3–0.45 rad/s (≈17–26°/s). That is
precisely the regime where C(ν) is large.

**Magnitudes at a hard turn** (u=1.6, v=0.15, r=0.45 rad/s), using the code's own
M and D (defaults, `hull_k=1.0`):

| axis  | C(ν)·ν force | D(ν)·ν force | |C|/|D| |
|-------|-------------:|-------------:|------:|
| surge | −45.2 N      | 250.0 N      | 0.18  |
| **sway**  | **+237.6 N** | 61.0 N   | **3.9** |
| yaw   | +81.6 N·m    | 361.5 N·m    | 0.23  |

The **sway** axis is the killer: the centripetal coupling `−Y_v̇·v·r − X_u̇·u·r`
(here ≈ +M[0,0]·u·r in C[1,2]) is **~4× the sway damping**. Sway is the boat's
softest axis, so the missing term controls the lateral force balance in a turn.

**End-to-end consequence — steady full-thrust full-steer turn** (integrated to
steady state, default params):

| model           | u (m/s) | v (sway, m/s) | r (°/s) | crab angle |
|-----------------|--------:|--------------:|--------:|-----------:|
| code (no C)     | 1.353   | **+0.397**    | 17.2    | **−16.3°** |
| correct (with C)| 1.350   | **−0.026**    | 17.7    | **+1.1°**  |

Without C the boat develops a large **+0.40 m/s sway and crabs ~16° to the
*outside* of the turn** — an artefact of the unbalanced `Y_r/N_v` damping
coupling. Adding C(ν) nearly cancels that sway (the added-mass centripetal force
opposes it) and leaves a small, correctly-signed inboard crab. So the omission
doesn't just change a magnitude — **it inverts the qualitative behaviour the
docstring is selling** (lines 30–36, the "visibly crab during a turn"). The
crab is real, but its dominant, correct source is C_A, not damping.

(At true low speed/low yaw — e.g. station-keeping at r≈0.05 rad/s, v≈0 — C(ν)·ν
in sway is only ~25 N and the omission is minor. The error is turn-rate-driven.)

---

## 3. The CORRECT C(ν) for this M (explicit matrices)

CG is at the origin here (M_RB = diag(m, m, Iz), no `x_G` terms), which removes
all `x_G·r` clutter. Using Fossen's standard sign convention (added-mass
coefficients negative) and the code's symbols
`X_u̇=x_udot`, `Y_v̇=y_vdot`, `Y_ṙ=y_rdot`, `N_v̇=n_vdot`, `N_ṙ=n_rdot`:

**Rigid-body** (Fossen 2021, Eq. for C_RB with x_G = 0):

```
            ⎡  0    -m·r    0 ⎤
C_RB(ν)  =  ⎢ m·r     0     0 ⎥
            ⎣  0      0     0 ⎦
```

**Added mass** (Fossen 2021, 3-DOF C_A):

```
            ⎡        0                 0          Y_v̇·v + Y_ṙ·r ⎤
C_A(ν)  =   ⎢        0                 0             −X_u̇·u      ⎥
            ⎣ −(Y_v̇·v + Y_ṙ·r)    X_u̇·u              0          ⎦
```

Both are skew-symmetric (νᵀC ν ≡ 0), as required (Fossen 2021, Property 3.1).

### Equivalent: just call `m2c` on the full M (recommended — it's what otter does)

The otter computes `CA = m2c(MA, nu_r)` and `CRB` separately, but because `m2c`
is linear in M and both blocks are mass matrices, evaluating `m2c` on the
**combined** `M = M_RB + M_A` gives exactly `C_RB + C_A`. With the code's own
`self._mass_matrix` (symmetric: M[1,2]=M[2,1]=−y_rdot=−n_vdot=40), the 3-DOF
`m2c` yields:

```
C[0,2] = -M[1,1]·v - M[1,2]·r        # = (Y_v̇·v + Y_ṙ·r) - m·r   ⟵ C_RB + C_A, sway/yaw
C[1,2] =  M[0,0]·u                   # = (m - X_u̇)·u  = m·r-arm + (-X_u̇)·u
C[2,0] = -C[0,2]
C[2,1] = -C[1,2]
```

For the default M = `[[330,0,0],[0,550,40],[0,40,672.5]]`:

```
            ⎡   0          0      -(550·v + 40·r) ⎤
C(ν)  =     ⎢   0          0          330·u       ⎥
            ⎣ 550·v+40·r  -330·u        0         ⎦
```

This is the single, exact term to add.

---

## 4. M_RB and added-mass structure — verified

- **M_RB = diag(m, m, Iz)** is correct *given CG at origin*. Iz from a uniform
  rectangle `m/12·(L²+B²) = 492.5 kg·m²` is a reasonable closed-form proxy
  (`fossen.py:114`). The CG-at-origin assumption is internally consistent: the
  thruster lever arms in `_tau` are measured from the CG, and the off-diagonal
  surge/sway↔yaw coupling that CG-offset would introduce is intentionally absent.
  **OK.**
- **Added-mass matrix** (`fossen.py:169-176`):
  ```
  M_A = [[-X_u̇,    0,      0   ],
         [   0,  -Y_v̇,  -Y_ṙ  ],
         [   0,  -N_v̇,  -N_ṙ  ]]
  ```
  With defaults `y_rdot = n_vdot = −40`, so M_A (and hence M) is **symmetric**,
  matching Fossen's requirement that M_A = M_Aᵀ for a body with port/starboard
  symmetry. **Good** — but note this symmetry is *not enforced*; it holds only
  because the two params happen to be set equal. If a user sets `y_rdot ≠ n_vdot`
  the model becomes physically inconsistent (M_A must be symmetric), and `m2c`'s
  internal `M = 0.5(M+Mᵀ)` symmetrization (which the code would lose) would no
  longer match. Worth a comment or an assertion.
- **Invertibility / positive-definiteness**: M = M_RB + M_A =
  `[[330,0,0],[0,550,40],[0,40,672.5]]`, eigenvalues ≈ {330, 538, 684} — all
  positive, det ≈ 1.22e8, well-conditioned. `np.linalg.inv` at line 179 is safe
  for the default and any sane parameter set. **OK.**

---

## 5. Other structural-fidelity notes

1. **"Crab during turn" misattributed (Sec. 2 / docstring lines 30–36).** The
   docstring credits the damping/added-mass *matrix coupling* for the crab. As
   shown, the dominant and correctly-signed crab is the **added-mass Coriolis
   force C_A**, which is dropped. Without C the crab is the wrong sign and ~6×
   too big in sway. *This is the headline structural error.*

2. **Damping cross-coupling `Y_r`, `N_v` doing C's job.** The linear damping has
   off-diagonal `y_r=n_r... y_r=-40, n_v=-40` (`fossen.py:96-98`). Physically
   these *are* legitimate hydrodynamic damping derivatives, but here they appear
   to be standing in for the missing Coriolis coupling and were likely tuned to
   produce *some* crab. After adding C(ν), these damping couplings should be
   re-reviewed (they may now double-count or over-rotate). Recommend re-tuning
   `y_r`, `n_v` once C is in.

3. **Quadratic damping is diagonal only** (`_damping`, lines 196-202) — fine and
   standard for a simple cross-flow model; the otter adds a separate cross-flow
   drag integral, which is a refinement, not a correctness issue. Not required.

4. **Integration order.** `step()` is semi-implicit only in the kinematics
   (velocity updated before position); the dynamics use an explicit Euler step
   for ν̇. Adding C(ν) (evaluated at the current ν, like D) keeps the same
   explicit structure and the same stability characteristics — no integrator
   change needed at dt≈0.02 s. The added-mass-dominated M keeps ν̇ well-scaled.

---

## 6. Concrete recommendation

**Add the Coriolis–centripetal term, evaluated on the existing mass matrix via
the otter's own `m2c` 3-DOF formula.** Minimal, exact change (illustrative — not
applied, per instructions):

```python
def _coriolis(self, nu):
    """C(nu) = C_RB + C_A via Fossen/otter m2c on the full M (3-DOF)."""
    M = self._mass_matrix            # symmetric for default params
    u, v, r = nu
    c02 = -M[1, 1] * v - M[1, 2] * r
    c12 =  M[0, 0] * u
    return np.array([[0.0, 0.0, c02],
                     [0.0, 0.0, c12],
                     [-c02, -c12, 0.0]])
```

and in `step()`:

```python
cor = self._coriolis(nu)
nu_dot = self._mass_inv @ (tau - cor @ nu - damping @ nu)
```

Then **re-tune `n_r`, `y_v`, `y_r`, `n_v`** so the steady turn rate and crab
match the intended ~12–25°/s spec (adding C will slightly raise r and flip the
crab sign, as the table in Sec. 2 shows). Optionally `assert` or symmetrize
`y_rdot == n_vdot` to keep M_A physical.

---

## Citations

- Fossen, *Handbook of Marine Craft Hydrodynamics and Motion Control*, 2nd ed.
  (2021): Ch. 3 (Theorem 3.2, `M → C(ν)`; Property 3.1 skew-symmetry of C);
  Ch. 6 (3-DOF horizontal maneuvering model `M ν̇ + C(ν)ν + D(ν)ν = τ`).
- Fossen marine-craft-model summary: <https://fossen.biz/html/marineCraftModel.html>
  (explicit 3-DOF C_RB and C_A matrices used in Sec. 3).
- `cybergalactic/PythonVehicleSimulator` (MIT):
  - `src/python_vehicle_simulator/lib/gnc.py` — `m2c(M, nu)` 3-DOF branch (quoted above).
  - `src/python_vehicle_simulator/vehicles/otter.py` — `C = CRB + CA`,
    `CA = m2c(MA, nu_r)`, and `nu_dot = ... + Minv @ (... - C @ nu_r - ...)`.
