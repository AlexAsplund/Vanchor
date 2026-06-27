# Physics review: environmental force modeling (wind, current, gusts)

**Scope.** Rigorous physics audit of how wind, current and gusts enter the boat
simulator. Files reviewed:

- `src/vanchor/core/models.py` — `Environment` + `Environment.drift_vector()`
- `src/vanchor/core/config.py` — `EnvironmentConfig`
- `src/vanchor/sim/fossen.py` — `FossenBoat.step()` (3-DOF surge/sway/yaw model)
- `src/vanchor/sim/boat.py` — simpler kinematic boat (same `drift_vector` path)
- `src/vanchor/sim/gust.py` — `GustModel` (Ornstein–Uhlenbeck gust)
- `src/vanchor/sim/weather.py` — `WeatherModel` (slow OU wander) + presets
- `src/vanchor/sim/simulator.py` — wiring (gust/weather → env → `boat.step`)

**Verdict up front.** The gust model is physically reasonable. The **current**
and **wind** models are both physically wrong in the same way: both are injected
as a *kinematic ground-velocity offset* added to SOG **after** the body→NED
rotation, and neither touches the body-frame dynamics (`τ`) or the hydrodynamic
relative velocity. This is the correct treatment for *neither* force. Current
should advect the hull via the **relative velocity** `ν_r` used inside the
damping/added-mass terms; wind should be an **aerodynamic force** added to `τ`.

---

## How the code currently works

`Environment.drift_vector()` returns a single (east, north) velocity that sums a
current term and a wind term:

```python
ce = current_speed * sin(current_dir);  cn = current_speed * cos(current_dir)
we = wind_speed * wind_leeway * sin(wind_dir)   # wind_leeway default 0.03
wn = wind_speed * wind_leeway * cos(wind_dir)
return (ce + we, cn + wn)
```

In `FossenBoat.step()` (and identically in `boat.py`):

```python
nu_dot = M_inv @ (tau - D(nu) @ nu)     # tau has NO environmental term
nu = nu + nu_dot*dt                      # body velocity, water-independent
... rotate (u,v) by heading -> (north, east)
de, dn = env.drift_vector()
s.ground_ve = east + de                  # drift added to SOG only
s.ground_vn = north + dn
s.speed_mps = hypot(u, v)                # "STW" = body speed, ignores current
```

So both wind and current are a fixed world-frame velocity bias on position
integration. `τ` (line `tau = self._tau(command)`) contains **only** thruster
force. The damping `D(ν)ν` uses the *absolute* body velocity `ν`, not a
water-relative velocity. The gust just modulates `wind_speed` for one step
(`simulator.py` line ~120), so it rides through the same wrong leeway path.

Note one internal consistency point the code does get right by accident:
`speed_mps = hypot(u,v)` is the hull's velocity through an assumed-still water
column, and SOG = that plus drift, so STW ≠ SOG numerically. But the *value* of
STW is wrong because the dynamics never saw the current (see below).

---

## 1. Current — should enter via relative velocity `ν_r`, not SOG

### The physics

A floating hull is advected by the water it sits in. Hydrodynamic forces (drag,
added-mass, lift) depend on the velocity of the hull **through the water**, the
relative velocity

```
ν_r = ν − ν_c
```

where `ν_c` is the current expressed in the body frame. Fossen's unified model
(Handbook of Marine Craft Hydrodynamics, 2nd ed., §3 and the *marineCraftModel*
note) makes the split explicit:

```
M_RB ν̇ + C_RB(ν) ν                      ← rigid-body terms use ABSOLUTE ν
+ M_A ν̇_r + C_A(ν_r) ν_r + D(ν_r) ν_r    ← hydrodynamic terms use RELATIVE ν_r
+ g(η) = τ + τ_wind + τ_wave
```

For a constant, irrotational current (`ν̇_c ≈ 0`) this simplifies to the common
3-DOF form used in DP/USV simulators:

```
M ν̇_r + C(ν_r) ν_r + D(ν_r) ν_r = τ + τ_wind
```

with the kinematics still integrating the **absolute** velocity:

```
η̇ = R(ψ) ν          (position moves with absolute ν, i.e. SOG)
ν_r = ν − ν_c        (only the hydro forces see the current)
```

The physical consequences this captures and the current code misses:

- **Drag against the current.** A boat held stationary over ground in a 1.2 m/s
  river (`river` preset) has `ν ≈ 0` but `ν_r ≈ −1.2 m/s`, so the hull feels a
  full 1.2 m/s of drag the thruster must fight. In the current code a
  station-keeping boat experiences **zero** current drag — the current only
  slides the position. This is the single biggest error for the river/coastal
  presets.
- **STW vs SOG.** STW is `|ν_r|` (speed through water), SOG is `|R(ψ)ν|` (speed
  over ground). The code reports `speed_mps = hypot(u,v) = |ν|`, which is neither
  when a current is present: it's the ground speed mislabeled as water speed.
  Correct: `STW = hypot(u−u_c, v−v_c)`, `SOG = hypot(ground_ve, ground_vn)`.
- **Weathervaning.** With current in the damping, an unpowered boat naturally
  swings to align with the flow (the high sway damping `y_v` resists beam-on
  drift). The kinematic-offset model can never produce this; it just translates
  the hull rigidly regardless of heading.

OCIMF current loads (as implemented in OrcaFlex vessel theory) likewise use the
**relative** velocity of the sea past the vessel — same principle.

### Is adding current to SOG ever acceptable?

Only in the degenerate case where you don't care about hull drag, heading
dynamics, or a correct STW — e.g. a pure kinematic waypoint demo. For an
autopilot test rig whose whole point is station-keeping against current, it is
the wrong model: it makes the controller's job artificially easy (no force to
reject, only a position bias).

### Recommended current implementation

Keep current **out** of `drift_vector()`; instead:

1. Add a body-frame current to `Environment`/the boat. Rotate the world current
   into the body frame each step:
   ```python
   # world current velocity (NED), m/s — direction is "toward which it flows"
   c_e = current_speed * sin(current_dir); c_n = current_speed * cos(current_dir)
   h = radians(heading_deg)
   u_c =  c_n*cos(h) + c_e*sin(h)      # body surge component
   v_c = -c_n*sin(h) + c_e*cos(h)      # body sway component
   nu_c = [u_c, v_c, 0.0]
   ```
2. Use `ν_r = ν − ν_c` in the damping (and, for full rigor, in `C_A(ν_r)ν_r`):
   ```python
   nu_r = nu - nu_c
   nu_dot = M_inv @ (tau - D(nu_r) @ nu_r)   # M_A ν̇_r ≈ M ν̇ for constant current
   ```
3. Integrate position with **absolute** `ν` (current advection comes out for
   free because `ν` relaxes toward `ν_c` under drag — the hull genuinely drifts
   with the water). Drop the current term from the SOG offset entirely.
4. Report `STW = hypot(*nu_r[:2])`, `SOG = hypot(ground_ve, ground_vn)`.

This is a small change to `FossenBoat.step()` and is exactly the Fossen otter /
DP-simulator pattern.

---

## 2. Wind — should be an aerodynamic force in `τ`, not a fixed drift

### The physics

Wind acts on the above-water hull/superstructure as a quadratic aerodynamic
**force** (and yaw moment), not a velocity. The standard OCIMF / Isherwood /
Blendermann form (Fossen Handbook §10; OCIMF 1994; reproduced in OrcaFlex vessel
theory) is:

```
X_wind = ½ ρ_air C_X(γ_w) A_F V_rw²       (surge force, N)
Y_wind = ½ ρ_air C_Y(γ_w) A_L V_rw²       (sway force, N)
N_wind = ½ ρ_air C_N(γ_w) A_L L  V_rw²    (yaw moment, N·m)
τ_wind = [X_wind, Y_wind, N_wind]
```

where, with the boat's own motion subtracted to get **relative** (apparent)
wind:

```
u_rw = u − V_w cos(β_w − ψ)        # apparent wind, body frame
v_rw = v − V_w sin(β_w − ψ)
V_rw = hypot(u_rw, v_rw)            # apparent wind speed
γ_w  = −atan2(v_rw, u_rw)          # angle of attack of apparent wind on hull
```

- `ρ_air ≈ 1.225 kg/m³`
- `A_F` = frontal (transverse) projected area above waterline; `A_L` = lateral
  (broadside) area; `L` = length.
- `C_X, C_Y, C_N` are dimensionless coefficients that depend on the relative
  wind angle `γ_w`. Typical magnitudes from wind-tunnel data (Isherwood,
  Blendermann): `C_X` peaks ~0.5–1.0 head/stern-on, `C_Y` peaks ~0.7–1.0 beam-on,
  `C_N` peaks ~0.05–0.2 around 30°–60° off the bow and crosses zero at 0/90/180°.
  A simple, adequate small-craft approximation is
  `C_X ≈ c_x·cos γ_w`, `C_Y ≈ c_y·sin γ_w`, `C_N ≈ c_n·sin 2γ_w` with
  `c_x≈0.6, c_y≈0.9, c_n≈0.1`.

### Why the current `wind_leeway` model is wrong

`drift_vector()` adds `wind_speed · 0.03` as a fixed ground velocity in the wind
direction. This is non-physical:

- **A stationary boat does not translate at a fixed 3% of wind speed regardless
  of heading.** Leeway is an *equilibrium* between the aerodynamic side force and
  the hull's hydrodynamic side resistance; it depends on heading-relative wind
  angle, hull shape, and the boat's own motion. A bow-on wind produces almost no
  sideways drift; a beam wind produces a lot. The constant-fraction model is
  heading-independent and ignores the hull entirely.
- **No yaw / weathervaning.** Real wind exerts a yaw moment (`N_wind`) that
  swings the bow — the dominant disturbance an anchor-hold/heading-hold
  autopilot must reject. The drift model produces **zero** yaw effect, so the
  controller is never tested against the most important wind disturbance.
- **No force to reject.** As with current, modeling wind as a position bias means
  the thruster feels no wind load; station-keeping is artificially easy and the
  thrust/battery budget is under-counted.
- **Quadratic, not linear.** Wind force ∝ V², so a gust from 7→9 m/s is a
  ~65 % force increase, not the linear bump the leeway model implies. The gust
  riding on a linear leeway term badly under-represents gust loads.

`wind_leeway` is essentially a hand-tuned fudge that happens to give *some*
downwind creep. It is not derivable from any wind-tunnel coefficient set and
should be replaced.

### Recommended wind implementation

Compute `τ_wind` and add it to `τ` inside `step()`:

```python
def _tau_wind(self, env, nu, heading_deg):
    Vw = env.wind_speed
    if Vw <= 0: return np.zeros(3)
    psi = radians(heading_deg)
    bw  = radians(env.wind_dir)          # "toward" convention; flip if "from"
    # wind velocity components in body frame (where the wind pushes)
    wu =  Vw*cos(bw - psi); wv = Vw*sin(bw - psi)
    u_rw = nu[0] - wu; v_rw = nu[1] - wv  # apparent wind incl. boat motion
    Vrw = hypot(u_rw, v_rw); gw = -atan2(v_rw, u_rw)
    q = 0.5 * RHO_AIR * Vrw*Vrw
    Cx = cx*cos(gw); Cy = cy*sin(gw); Cn = cn*sin(2*gw)
    X = q*Cx*A_F; Y = q*Cy*A_L; N = q*Cn*A_L*L
    return np.array([X, Y, N])
```

> **Implementation correction (verified empirically).** The pseudocode above has
> a sign error: with `u_rw = u − wu` (boat minus wind) and `gw = −atan2(v_rw, u_rw)`
> the force comes out *upwind*. The physically-correct, as-shipped form uses the
> **apparent wind = wind − boat** and its un-negated angle:
> `aw = (wu − u, wv − v); gw = atan2(aw_v, aw_u); X = q·cx·cosγ·A_F; …`
> This reproduces the 54 N beam-on sanity check, pushes an idle boat **downwind**,
> and gives zero yaw beam-on. See `tests/test_fossen.py::test_wind_is_a_force_*`.

with new params on `FossenParams`/`Environment`:
`A_F` (~0.6 m² for a 4.1 m skiff with a low occupant/console),
`A_L` (~1.5–2.5 m²), `L=4.1`, `RHO_AIR=1.225`, `cx≈0.6, cy≈0.9, cn≈0.1`.
Add `τ_wind` to `τ` in the `nu_dot` line. Leeway then **emerges** from the
balance of `Y_wind` against sway damping `y_v` — no `wind_leeway` knob needed.

Sanity check of magnitude: beam-on 7 m/s wind on `A_L≈2 m²`:
`Y ≈ 0.5·1.225·0.9·2·49 ≈ 54 N`. Against the default sway damping
(`y_v ≈ −260`, i.e. ~260 N per m/s), steady leeway sway ≈ 0.2 m/s — a realistic
few-percent-of-wind drift that now varies correctly with heading and gusts
quadratically. (This recovers the intended ~3 % at beam-on while being zero
head-on, which the constant model cannot do.)

---

## 3. Gusts (`gust.py`) — assessment

The gust is an **Ornstein–Uhlenbeck** (mean-reverting) process:

```
v̇ = −v/τ + σ·ξ(t),    σ = amplitude·√(2/τ),   τ = 5 s default
```

so the stationary std is `amplitude` and the autocorrelation is `e^{−|Δt|/τ}`.

**This is physically defensible and a good choice for a control test rig:**

- An OU / first-order Gauss–Markov process is exactly the **longitudinal Dryden
  gust spectrum** — a Lorentzian PSD `S(ω) ∝ σ²·(2τ)/(1+(ωτ)²)`. The Dryden
  model is the standard, computationally cheap rational approximation to the
  irrational **von Kármán** spectrum and is widely used in aircraft/UAV sims
  precisely because it's a simple white-noise-driven filter. So the OU gust is
  not an ad-hoc hack; it's the recognized first-order turbulence model.
- The `τ ≈ 5 s` correlation time is reasonable for boat-scale gusts (gust fronts
  on the order of seconds to tens of seconds). The slow `WeatherModel` OU layer
  (τ = 180–300 s) sensibly separates the slow mean-wind wander from fast gusts —
  a two-scale model that matches how real wind records look (slow mean drift +
  fast turbulence).

**Limitations / minor improvements (not bugs):**

1. *Magnitude semantics.* `amplitude_mps` should track turbulence intensity
   `I_u = σ_u/Ū` (typically 0.1–0.2 over open water, higher over rough terrain).
   Tying `amplitude` to a fraction of the live mean wind would be more physical
   than a fixed m/s.
2. *Gust only scales speed, not direction.* Real turbulence also veers the wind
   direction (lateral/`v` component). Once wind is a proper force (§2), a second
   OU process on the cross-wind component would give realistic gust-induced yaw.
   The von Kármán/Dryden model has independent `u`,`v`,`w` channels; the sim only
   has the longitudinal one.
3. *True von Kármán PSD* (the `1+(ωτ)^{5/3}`-style roll-off) is overkill here —
   the OU/Dryden first-order form is the right level of fidelity for an autopilot
   stress test. **Recommendation: keep the OU gust**, just feed it into `τ_wind`
   (quadratically) once the wind force model lands, and optionally add a lateral
   gust channel.

---

## Summary of issues and recommended fixes

| # | Issue | Current (wrong) | Correct physics | Fix |
|---|-------|-----------------|-----------------|-----|
| 1 | **Current** added to SOG only | hull feels no drag from current; STW mislabeled | current advects hull via `ν_r = ν − ν_c` inside damping/added-mass; integrate position with absolute `ν` | rotate current into body frame, use `ν_r` in `D(ν_r)ν_r`, drop current from `drift_vector`; report STW=`|ν_r|`, SOG=ground velocity |
| 2 | **Wind** as fixed `0.03·V` ground drift | heading-independent, no yaw, no force, linear in V | aerodynamic force `½ρ_air C(γ_w) A V_rw²` in surge/sway/yaw, apparent (relative) wind | add `τ_wind` to `τ`; leeway emerges from `Y_wind` vs sway damping; delete `wind_leeway` |
| 3 | **Gusts** OU process | (correct) | OU = Dryden longitudinal spectrum (1st-order von Kármán approx) | keep; route through `τ_wind` quadratically; optionally add lateral gust channel + intensity-based amplitude |

**Net effect on the autopilot test rig:** today the controller never has to
reject any *force* from wind or current — only a position bias — and never sees a
wind-induced yaw moment. That makes station-keeping/heading-hold tests far easier
than reality and under-counts thrust/battery demand. Fixes 1 and 2 put both
disturbances where the physics demands (relative velocity for current, `τ` for
wind), which is also where they most stress a real autopilot.

---

## Citations

- Fossen, T. I. *Handbook of Marine Craft Hydrodynamics and Motion Control*,
  2nd ed., Wiley, 2021 — ch. on environmental forces; relative-velocity current
  model and `τ_wind` wind-force model.
  <https://www.wiley.com/en-us/Handbook+of+Marine+Craft+Hydrodynamics+and+Motion+Control,+2nd+Edition-p-9781119575054>
- Fossen, *Marine Craft Model* note — explicit `ν_r = ν − ν_c` split (rigid-body
  terms use absolute `ν`, hydrodynamic terms use relative `ν_r`):
  <https://fossen.biz/html/marineCraftModel.html>
- Fossen & Smogeli / IFAC, *How to incorporate wind, waves and ocean currents in
  the marine craft equations of motion*:
  <https://www.sciencedirect.com/science/article/pii/S1474667016312162>
- OCIMF (1994) wind/current load coefficients; implementation form in OrcaFlex
  vessel theory (½ ρ C A V², relative velocity, coefficients vs heading):
  <https://www.orcina.com/webhelp/OrcaFlex/Content/html/Vesseltheory,Currentandwindloads.htm>
- Blendermann / Isherwood wind-coefficient methods (wind-tunnel CX, CY, CN vs
  relative wind angle): "A Simple Method to Estimate Wind Loads on Ships",
  <http://www.i-asem.org/publication_conf/acem12/W3A-4.pdf>;
  Blendermann statistical assessment, <https://d-nb.info/1155824792/34>
- Dryden / von Kármán gust spectra (OU = first-order Gauss–Markov = Dryden
  longitudinal channel): von Kármán model,
  <https://en.wikipedia.org/wiki/Von_K%C3%A1rm%C3%A1n_wind_turbulence_model>;
  Dryden model, <https://en.wikipedia.org/wiki/Dryden_Wind_Turbulence_Model>
