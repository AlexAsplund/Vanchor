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

## Reproducing

```bash
# Smart (hybrid + full azimuth)
python -m experiments.anchor_policy.train --steer-range 120 --history 4 \
  --wind-cap 6 --current-cap 0.6 --gust-cap 1.5 --gens 1600 --workers 18

# Leif (pure + full azimuth)
python -m experiments.anchor_policy.train --pure --steer-range 120 --history 4 \
  --wind-cap 6 --current-cap 0.6 --gust-cap 1.5 --gens 2600 --workers 18
```

Ship a trained checkpoint by copying `best_policy.json` to
`src/vanchor/controller/anchor_policy.json` (Smart) or `anchor_leif.json`
(Leif) and stamping `train_azimuth_deg` into it.

> **Future speedup.** ES is embarrassingly parallel; vectorising the Fossen sim
> in JAX/CuPy would let the whole population roll out on a GPU (the box has a
> GB10) for a ~10–100× training speedup. The runtime stays numpy-only regardless
> — training method never touches deployment.
