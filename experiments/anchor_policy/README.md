# Anchor policy — a tiny learned station-keeper

A long-running experiment that trains a **tiny neural-network controller for the
anchor (station-keeping) function**, small enough to run on a Raspberry Pi with
no ML runtime (a forward pass is a few small numpy matrix multiplies, ~µs).

## Why this design

- **Tiny policy** (`policy.py`): an 8→24→16→2 tanh MLP (~650 params). Input =
  what a real boat actually senses (anchor position error, own velocity, yaw
  rate — all in the **body frame**, so it's heading-invariant and deployable
  straight from GPS + compass). Output = `MotorCommand[thrust, steering]`.
- **Evolution Strategies** (`train.py`, OpenAI-ES): gradient-free, so no torch —
  just numpy + `multiprocessing`. It fits a *fast, deterministic* simulator and
  a *tiny* policy perfectly, and parallelises across every CPU core. "Speeding
  up time" is literal: the sim runs un-paced, flat out, on all cores, at a
  training `dt` of 0.1 s. The integrator sits ~30× inside its stability limit,
  so 0.1 s gives the **same physics** as the 0.05 s runtime step — `eval.py`
  re-checks the policy at 0.05 s to confirm the transfer.
- **Trains on the real physics** (`env.py`): the exact Fossen 3-DOF model +
  the real gust (Ornstein–Uhlenbeck) and slow-weather pipeline from the runtime.
- **Every scenario thinkable** (`scenarios.py`): randomised wind (0–12 m/s) +
  gusts, current (0–1.2 m/s), slow weather wander, **and** the boat itself
  (mass, hull character, bow/stern/centre mount, motor power) and the start
  (offset, heading, initial drift). All candidates in a generation are scored on
  the *same* batch (common random numbers) for a fair, low-variance ranking; a
  separate held-out validation set drives the learning curve and "best" pick.

## Run

```bash
python -m experiments.anchor_policy.train          # start / resume (checkpoints/)
python -m experiments.anchor_policy.train --gens 30 --pop 24 --k 4   # quick smoke
python -m experiments.anchor_policy.eval           # best policy @ runtime dt 0.05
```

Stop anytime — `checkpoints/state.npz` lets it resume; `checkpoints/log.jsonl`
is the learning curve; `checkpoints/best_policy.json` is the deployable policy.

## Reward

Per step (all optional terms 0 unless the flag is passed):

```
  -distance
  -0.6·(outside the watch circle)
  -0.05·thrust²
  -arate·(Δthrust² + Δsteer²)              --arate      action-rate (CAPS)
  -anticip·max(0, outward radial speed)    --anticip    arrest drift early
  -speed_pen·SOG²        while dist ≤ 1.6R --speed-pen  orbit exploit fix
  +bonus·(1+cos(hdg−h₀))/2 while dist ≤ R  --heading-bonus  hold engage heading
  -yaw_pen·r²            while dist ≤ 1.6R --yaw-pen    pirouette fix
  DQ (terminate, −2000) past ±360° net rotation  --dq-rotation
```

i.e. hold the anchor **tight**, **stationary**, **pointing where it was
engaged**, and **cheap** — and a completed revolution disqualifies the
rollout outright. The speed/yaw/heading/DQ terms exist because a pure policy
otherwise reward-hacks station-keeping by orbiting; the full story (five
rounds of exploit → countermeasure, with numbers) is in
`docs/anchor-ml.md` § "The orbit exploit". Maximised over full episodes
across the randomised scenario batch.

## Escaping the orbit basin (BC init)

A pure policy trained from scratch reliably converges to the orbit even
under a correct reward — full thrust buys instant control authority, and
ES's local perturbations can't cross the valley to hold-and-trim.
`bc_init.py` behavior-clones a PID station-keeper into the policy
(supervised regression, numpy backprop, minutes); start ES from that with
`--init-policy ... --sigma 0.02 --lr 0.01`. The small sigma is mandatory:
the default 0.1 is ~100% of the BC weights' median magnitude and wrecks the
clone within ~5 generations.

`--hold-heading-obs` appends sin/cos of (heading − engage heading) to each
obs frame (8 → 10 dims) so the policy can steer *back* to the engage
heading, not merely resist yaw; the trained JSON stamps `obs_heading: true`
and the runtime `AnchorLeifMode` builds the matching frame.

## Adaptive trait pressure (--adapt)

`--adapt --target-hold 80 --target-hdg 20 --target-dq 0`: every
`--adapt-every` (50) gens the trainer raises the weight of each trait
missing its target (×1.25, ≤16× base) and relaxes met-with-margin weights
back toward base — automated curriculum instead of hand-retuned restarts.
Under `--adapt` the best checkpoint is picked by a FIXED canonical score
(`hold + 0.25·within − 0.5·hdg_err − 2·dq%`), never by the moving
`val_return`. Weight trajectory is logged per record (`w_speed`, `w_head`,
`w_yaw`) and `adapt @gen …` lines mark every change.

## Deploying to the boat

`best_policy.json` is the whole model. On the Pi, load it with `TinyPolicy.load`
and call `forward(obs)` each control tick (build `obs` from GPS/compass exactly
as `env._obs` does), then send the returned `[thrust, steering]` as a
`MotorCommand`. No torch, no GPU. Integration as a selectable `anchor_ml` control
mode is the planned next step once the policy matures.
