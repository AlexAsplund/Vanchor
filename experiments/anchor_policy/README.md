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

Per step: `-distance − 0.08·thrust² − 0.6·(outside the watch circle)` — i.e.
hold the anchor **tight** and **cheap**, with an extra pull back inside the
radius. Maximised over full episodes across the randomised scenario batch.

## Deploying to the boat

`best_policy.json` is the whole model. On the Pi, load it with `TinyPolicy.load`
and call `forward(obs)` each control tick (build `obs` from GPS/compass exactly
as `env._obs` does), then send the returned `[thrust, steering]` as a
`MotorCommand`. No torch, no GPU. Integration as a selectable `anchor_ml` control
mode is the planned next step once the policy matures.
