# Learned station-keeping (Smart & Leif)

Vanchor's "virtual anchor" holds the boat on a mark. Three station-keepers ship,
in increasing order of ambition:

| Mode | What it is | Command | Safety floor |
|---|---|---|---|
| **Anchor** (`anchor_hold`) | Hand-tuned PID | `pid(err, vel)` | itself |
| **Smart** (`anchor_ml`) | **Hybrid: PID + learned residual, full-azimuth** | `clip(pid + 0.3·net)` rescaled to the boat's swing | PID base + residual-decay guardrail |
| **Leif** (`anchor_leif`) | **Pure learned, full-azimuth** (experimental) | `clip(net)` rescaled to the boat's swing | none (opt-in research mode) |

All three produce a `ManualSetpoint(thrust, steering)` and drive `state.anchor`.

## The tiny policy

Both learned modes run the same architecture: a **~1.6k-parameter tanh MLP**
(layer sizes `(32, 32, 16, 2)`), fed an 8-dim body-frame observation
(fwd/lat position error, fwd/lat ground velocity, yaw rate, previous
thrust/steering, distance) stacked over a **history of 4 frames**. Inference is
a handful of numpy matmuls — **numpy-only, no torch, no GPU at runtime** — so it
runs on the Pi identically to how it was trained. The weights live in
`src/vanchor/controller/anchor_policy.json` (Smart) and `anchor_leif.json`
(Leif), each ~33 KB of JSON.

## How they're trained

Training is **Evolution Strategies** (OpenAI-ES, gradient-free, numpy-only) in
`experiments/anchor_policy/`. A population of perturbed weight vectors is scored
by rolling each out through a **faithful replica of the deployment sensor
pipeline** (the same noisy, low-rate GPS/heading the boat actually sees — see
`env.py`), and the weights step toward the higher-scoring perturbations. Reward
is `-distance - outside-penalty - energy - action-rate`.

Two knobs make the current generation:

- **Full azimuth (`--steer-range 120`).** The boat is trained with a wide 120°
  steering swing instead of the default ±35° autopilot band, so the policy can
  learn to **vector thrust** through the motor's full rotation — the single
  biggest win, especially on stern mounts where a narrow band can't point the
  wash against the set.
- **Holdable-condition caps (`--wind-cap 6 --current-cap 0.6 --gust-cap 1.5`).**
  Training is capped to conditions a trolling motor can actually station-keep in,
  so the un-holdable gale tail doesn't dominate the average. The result
  generalises *past* the cap (see the numbers below).

Smart trains as a **residual** (`--steer-range 120`, no `--pure`, ~1600 gens);
Leif trains **pure** (`--pure --steer-range 120`, from scratch, ~2600 gens).

## Deployment fidelity: azimuth rescaling

The anchor `ManualSetpoint` path deliberately **bypasses the ±35° autopilot
steering cap** (so a station-keeper can vector), which means its steering
fraction reaches the boat's *full mechanical* swing. A policy trained at 120°
would therefore over-steer on a boat whose mechanical range is, say, 180°.

Both learned modes fix this by **rescaling steering to the boat's range**:
`st_out = st · (train_azimuth_deg / state.max_steer_angle_deg)`. The trained
azimuth is recorded in the policy JSON (`train_azimuth_deg: 120`); legacy
policies without it are left unscaled (unchanged behaviour). On the default
180° boat the rescale is `120/180 = 0.67`, so physical deflection matches
training on any boat. The residual-decay guardrail (Smart) still floors the
command to the pure PID base if the hybrid ever underperforms.

## Held-out comparison

Per-mount, on the held-out validation set (5 m watch circle). "within" =
fraction of the settled second half inside the circle; higher is better.

**Capped regime (≤6 m/s wind):**

| Controller | overall | bow | stern | center | mean dist |
|---|---|---|---|---|---|
| PID | 82.4% | 99.8% | 79.5% | 40.3% | 5.63 m |
| Smart — old ±35° hybrid | 81.3% | 99.5% | 76.6% | 38.7% | 6.40 m |
| **Leif** (pure + azimuth) | 73.7% | 87.8% | **98.2%** | 18.7% | 5.26 m |
| **Smart** (hybrid + azimuth) | **90.6%** | **100.0%** | **100.0%** | **59.5%** | **4.52 m** |

**Full regime (0–12 m/s, incl. un-holdable):** Smart (hybrid + azimuth) holds
**90.4%** in-radius vs pure PID's **70.2%** — it generalises past its capped
training with no strong-wind regression.

### Reading the results

- **Smart (hybrid + full azimuth) strictly dominates** PID and the old ±35°
  hybrid on every mount and both regimes, tighter mean distance, without
  thrashing the motor — and it keeps the safety floor (worst case = PID). This
  is the shipped default.
- **Leif proved the hypothesis but isn't the daily driver.** A *pure* policy
  given the full azimuth learns a superb stern hold (98.2%, beating even PID's
  79.5%), confirming the vectoring headroom is real — but with no PID base it
  regresses on the easy bow (87.8%) and near-uncontrollable center (18.7%)
  mounts, and it runs the motor near-flat-out (command energy ~0.98 vs the
  hybrid's gentle residual). It ships as a fun, opt-in curiosity, now strictly
  dominated by Smart.
- **center** mount is near-uncontrollable for everyone (no yaw lever arm);
  don't read too much into its absolute numbers.

## Azimuth + actuator-fidelity retraining (2026-07-09 → 14)

Two sequential full-compute stages (15 workers, 24 h, ~10k gens each; ES
pop 48x2, k=12, warm-started from the shipped policies) on the corrected
physics with the new **95 deg/s effective steering-slew actuator model**
(`--steer-rate-dps`; the head is a 20 rpm gearmotor = 120 deg/s peak, ~95
effective with ramp). Stage A trained a ±360° pair (hybrid needed
`--pid-cal-deg 120` — the PID base's ±45°-intent steering runs 8x hot at
±360 and opened at 21% within before calibration); stage B the ±120° pair.

Held-out cross-eval (k=128, each policy in its NATIVE env, all with the
95 deg/s actuator):

| Controller | within 5 m | mean dist | energy |
|---|---|---|---|
| smart360 | 90.3% | 8.45 m | 0.94 |
| **smart120b (promoted)** | **90.1%** | **7.92 m** | 0.99 |
| shipped Smart (prev) | 83.7% | 9.24 m | 0.72 |
| PID (±35 native) | 77.8% | 10.80 m | 0.29 |
| leif120b | 65.7% | 8.59 m | 1.00 |
| leif360 | 65.7% | 9.80 m | 0.97 |
| shipped Leif | 64.6% | 9.50 m | 0.98 |

Conclusions:
- Retraining on corrected physics + realistic actuator: **+6.5 points** over
  the previous shipped hybrid under deployment-realistic conditions.
- **±360 ties ±120 at matched compute** (90.3 vs 90.1; ±120 tighter mean
  distance). An interim +1.5-point lead for ±360 was a compute-imbalance
  artifact that stage B's same-budget comparison eliminated. Full rotation
  is not worth deployment complexity for the hybrid: ±120 + reverse already
  covers every wash direction, and the redundant wide action mapping eats
  the theoretical gain. (A (sin,cos) direction parameterization remains the
  untested alternative if 360 is revisited.)
- Pure Leif gained ~1 point — hybrid remains the daily driver; Leif ships
  unchanged.
- Trade-off: the new hybrid spends more energy than its predecessor
  (0.99 vs 0.72) buying the extra hold.

**Promoted:** `smart120b` best checkpoint → `src/vanchor/controller/
anchor_policy.json` (train_azimuth_deg 120, provenance embedded; predecessor
archived as `runs/anchor_policy-superseded-20260714.json`). Sign-off eval on
the promoted file reproduced 90.1% / 7.92 m; the 73 anchor runtime tests pass.

## Physics-fix re-evaluation (2026-07-09)

The Fossen model gained the missing `Dnu_c` current-rotation term (see
CHANGELOG / commit `082038a`; a 60 s turn in a 0.5 m/s current previously
diverged 14 m). Both shipped policies and the PID baseline were re-scored on
the corrected physics — `eval.py` defaults: 128 held-out scenarios, 5 m
circle, 180 s (NOTE: a different protocol from the capped per-mount table
above, so columns are not comparable across sections):

| Controller | within (pre-fix) | within (fixed) | mean dist (pre → post) | energy |
|---|---|---|---|---|
| PID | 75.6% | 79.4% | 10.04 → 9.82 m | 0.259 |
| Smart (shipped) | 75.0% | **79.9%** | 9.79 → **9.29 m** | 0.692 |
| Leif | 76.7% | **82.0%** | 10.57 → 10.03 m | 0.890 |

Every controller improves: the omitted term acted as a phantom disturbance
while turning in current, so the corrected world is easier to hold in. The
ranking is unchanged and the policies transfer without retraining; a future
training run on the corrected physics may claw back a little more.

## The orbit exploit (2026-07-16)

Leif was observed on the water doing something *technically correct*: holding
its watch-circle score by **driving full speed in a tight circle inside the
radius**. Reward-hacking, textbook edition.

**Diagnosis.** The reward was `-dist − 0.6·outside − 0.05·thrust² −
action-rate`. Nothing penalizes *speed*: a 2 m orbit at full thrust keeps
`dist` small, never pays the outside penalty, and the energy term is noise
(0.05/step vs ~1/step per metre of distance). Constant way also buys cheap
steering authority, so the orbit is a *stable ES attractor*. It was visible in
the shipped policy's training log all along — `energy 0.997` = mean thrust² ≈ 1
— and the eval metric couldn't see it: "% of settled samples inside the
radius" is exactly what an orbit maximises.

**Fix (two halves):**

1. **Reward** (`env.py`): new `speed_pen` term — quadratic ground-speed
   penalty applied *only inside the watch circle* (`−0.35·sog²`). Trim speeds
   (~0.3 m/s) cost nothing; an orbit at 2 m/s costs ~1.4/step — decisively
   worse than an honest hold's residual error. Recovery sprints outside the
   circle stay free. Trained via `--speed-pen 0.35`.
2. **Metrics** (`train.py`/`eval.py`): the headline number is now **hold%** =
   settled samples that are inside the radius **and** ≤ 0.5 m/s SOG, with
   mean settled SOG alongside. Containment alone is gameable; hold% is not.

**Baseline with the new metrics** (held-out k=64, native envs, 95 °/s
actuator) — the cheat quantified:

| Controller | within 5 m | **hold** | settled SOG | energy |
|---|---|---|---|---|
| Leif (shipped, orbiter) | 59.5% | **21.6%** | 0.66 m/s | 0.97 |
| Smart (shipped hybrid) | 90.0% | **88.7%** | 0.23 m/s | 0.99 |
| PID (±35 native) | 76.8% | **76.5%** | 0.13 m/s | 0.25 |

An honest controller's within ≈ hold (Smart, PID — the PID base damps
velocity, so the hybrid never learned to orbit). Two-thirds of Leif's
containment was orbiting.

**Round 2 — the policy found the seam.** After ~2 h with an *in-circle-only*
gate at `--speed-pen 0.35`, the retrain adapted by **orbiting on the circle's
edge** (`mean_dist 4.9 m` of a 5 m radius): half of every lap is outside the
radius, where the speed penalty didn't apply. Countermeasures: the penalty
gate widened to **1.6× radius** (any edge orbit is fully covered; far-away
recovery sprints stay free, and a fast final approach pays for only a few
steps — usefully teaching deceleration-on-arrival) and the weight raised to
0.5.

**Heading hold (owner requirement).** A real anchored boat also holds its
heading — and heading is the orbit's Achilles' heel (an orbit sweeps it
through 360°). Two additions:

- **Reward:** `+2.0 · (1+cos(heading − h₀))/2` per step, **only while inside
  the circle** (h₀ = heading at engage). Deliberately a *bonus*, not a
  penalty: a penalty gated on "inside" makes loitering outside the circle a
  dodge (the same seam as the edge orbit); forfeiting a bonus is never worth
  leaving the circle for. An orbit collects at most half of it.
- **Observation (obs v2h):** the frame gains `sin/cos(heading − h₀)`
  (8 → 10 dims) via `--hold-heading-obs`; without it a policy can only learn
  yaw *stiffness* from the yaw-rate input — it has no way to steer *back*
  after a gust rotates the boat. Policies trained this way stamp
  `obs_heading: true` in their JSON; `AnchorLeifMode` captures the engage
  heading in `activate()` and builds the matching frame. Legacy 8-dim
  policies (incl. shipped Smart) are untouched. Note the single-thruster
  caveat: center mounts (`thruster_x_m = 0`) have ~no yaw authority, so the
  heading term is unactionable there and washes out of ES ranking (common
  batch).

**Round 3 — the orbit is an optimization basin, not a reward problem.** With
the reward fixed (honest holding out-scores the orbit ~30:1 per step), BOTH
retrains — warm-started and from-scratch-with-heading-obs — still converged to
the orbit (energy 0.999, mean |heading err| 90°, 1000+ generations flat).
Full thrust gives a young policy robust control authority immediately; ES's
local perturbations never cross the valley. Fixes, each verified:

- **`bc_init.py` (behavior cloning):** distill a PID station-keeper into the
  10-dim policy by supervised regression (numpy backprop, minutes). The clone
  alone scores hold 79% / SOG 0.18 / energy 0.25 — ES now STARTS in the
  honest basin and refines.
- **ES exploration scale:** the default `--sigma 0.1` equals ~100% of the BC
  weights' median magnitude — the first population wrecked the clone within 5
  generations and slid back to orbit. BC-warm-started runs use `--sigma 0.02
  --lr 0.01`.
- **Pirouette (round 3b):** the bc-init run (leif120e, 4915 gens) held 1.2 m
  mean distance in the full-stack sim — but spun in place at ~18 °/s: a
  heading sweep still collects half the heading bonus, and evidently buys
  control convenience worth more than the other half. New `--yaw-pen`
  (quadratic yaw rate, same 1.6× gate) charges the spin itself — no sweep
  symmetry to hide behind.

**Round 4 — pressure works but converges slowly.** leif120f (from the e-best,
`--heading-bonus 6.0 --yaw-pen 10.0`, 6 h / 4894 gens) halved the spin rather
than eliminating it: held-out within 89.1% / hold 71.5% / SOG 0.37 — a
different controller from the shipped orbiter — and the sim gauntlet showed
2.2 m mean hold at thrust duty 0.51, but still turning at ~10.6 °/s (was 18).
At `yaw-pen 10` that spin costs only ~0.34/step; every shaped penalty so far
has been *negotiated down* like this (full-speed orbit → edge orbit →
18 °/s → 10 °/s), each round buying roughly half the misbehavior.

**Round 5 — disqualification (owner suggestion): ban the class, don't tax
it.** `--dq-rotation 360`: a rollout is DISQUALIFIED (episode terminated,
−2000 lump — an order of magnitude worse than any honest rollout earns) the
moment its **net signed rotation** from the engage heading exceeds ±360°.
Design points:

- *Net signed* accumulation: gust-driven ±90° swings cancel out and never
  trip it; only completed revolutions do. Weather-vaning stays legal.
- *Gradient-preserving*: a slower rotator DQs later and keeps more earned
  reward first, so ES is pushed smoothly toward "never complete a
  revolution" even from a start where every candidate DQs.
- *Un-negotiable*: unlike a per-step tax there is no spin rate at which the
  behavior becomes worth its price.
- New `dq%` learning-curve column = fraction of validation rollouts
  disqualified. The f-best warm start opens at **17%**; the target is 0.

leif120h (from the f-best, `--heading-bonus 8.0 --yaw-pen 40.0
--dq-rotation 360`) is the current run. (A brief leif120g with only
`bonus 8 / yaw-pen 40` was superseded by the DQ idea at gen ~10 and killed.)

### Run ledger (the orbit saga in numbers)

Held-out protocol: eval.py, k=64, 180 s, 5 m circle, 95 °/s actuator,
uncapped scenarios. Sim = full-stack `anchor_leif` engagement on the real
server (4 min, time_scale 1, settled half scored).

| Run | Recipe delta | within | hold | SOG | hdg err | verdict |
|---|---|---|---|---|---|---|
| shipped Leif (v1, leif120b) | original reward | 59.5% | 21.6% | 0.66 | ~90° (orbit) | the complaint: full-speed orbit |
| retrain-warm / retrain-scratch | +speed-pen, +heading-bonus 2 | ~75%* | ~32%* | 0.60* | ~90°* | stuck in orbit basin 1000+ gens; killed |
| PID clone (bc_init) | supervised, no ES | 83.2%* | 80.9%* | 0.16* | 80°* | honest basin proven reachable |
| leif120e | BC init + σ0.02 | 90.1% | 57.5% | 0.45 | 78.8° | sim: 1.2 m hold but 18 °/s pirouette — vetoed |
| leif120f | +bonus 6, +yaw-pen 10 | 89.1% | 71.5% | 0.37 | 55.6° | sim: 2.2 m hold, 10.6 °/s spin — vetoed |
| leif120h | +bonus 8, yaw-pen 40, **DQ 360°** | (training) | | | | dq 17%→? |
| — Smart (shipped hybrid) | reference | 90.0% | 88.7% | 0.23 | — | honest (PID base damps velocity) |
| — PID (±35 native) | reference | 76.8% | 76.5% | 0.13 | 78.4° | honest, gentle, weaker containment |

\* capped-regime numbers (training/validation metric env), not directly
comparable to the uncapped eval columns; shown for trend.

### The promote gauntlet

A Leif candidate ships only if it passes ALL of:

1. **Held-out eval** (`eval.py`, uncapped, k=64): hold% and within% must
   decisively beat the shipped policy; SOG and energy sane.
2. **Full-stack sim check** (isolated server, real `anchor_leif` command,
   time_scale 1, 4 min): settled hold ≥ the eval story, AND total heading
   sweep < 360° in the settled window (no orbit, no pirouette), AND mean
   |heading − engage heading| small.
3. Anchor runtime tests green; policy JSON meta complete
   (`steer_sign`, `train_azimuth_deg`, `obs_heading`).

Lessons (the short version): *containment is not station-keeping* — score
velocity and heading, not just position; *shaped penalties get negotiated,
gates get exploited at their seams, hard disqualification bans the class*;
*a correct reward is not sufficient — ES needs to start in the right basin*
(behavior-clone first, and scale σ to the clone's weight magnitude); *always
sim-check the actual deployed mode before promoting* — two candidates with
excellent eval tables were vetoed by watching the boat for four minutes.

## Reproducing

```bash
# Smart (hybrid + full azimuth)
python -m experiments.anchor_policy.train --steer-range 120 --history 4 \
  --wind-cap 6 --current-cap 0.6 --gust-cap 1.5 --gens 1600 --workers 18

# Leif (pure + full azimuth) — current recipe, post orbit-saga:
# 1) behavior-clone a PID station-keeper (starts ES in the honest basin)
python -m experiments.anchor_policy.bc_init \
  --out experiments/anchor_policy/checkpoints/bc_init_headobs.json
# 2) ES from the clone. sigma MUST stay small (the default 0.1 is ~100% of
#    the BC weights' median magnitude and destroys the clone in ~5 gens).
python -m experiments.anchor_policy.train --pure --steer-range 120 --history 4 \
  --wind-cap 6 --current-cap 0.6 --gust-cap 1.5 --steer-rate-dps 95 \
  --speed-pen 0.5 --heading-bonus 8.0 --yaw-pen 40.0 --dq-rotation 360 \
  --hold-heading-obs --sigma 0.02 --lr 0.01 \
  --init-policy experiments/anchor_policy/checkpoints/bc_init_headobs.json \
  --hours 6 --workers 18
```

Ship a trained checkpoint by copying `best_policy.json` to
`src/vanchor/controller/anchor_policy.json` (Smart) or `anchor_leif.json`
(Leif) — train.py stamps `steer_sign`, `train_azimuth_deg` and
`obs_heading` into the JSON automatically. Then run the promote gauntlet
above before deploying.

> **Future speedup.** ES is embarrassingly parallel; vectorising the Fossen sim
> in JAX/CuPy would let the whole population roll out on a GPU (the box has a
> GB10) for a ~10–100× training speedup. The runtime stays numpy-only regardless
> — training method never touches deployment.
