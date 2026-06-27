"""Train the tiny anchor policy with Evolution Strategies (OpenAI-ES).

ES fits a gradient-free, embarrassingly-parallel paradigm to a fast deterministic
simulator and a tiny policy: each generation perturbs the weights in many random
directions (antithetically), rolls the perturbed policies out across a shared
batch of randomised scenarios, rank-normalises the returns and takes an Adam step
up the estimated gradient. No autodiff, no torch -- just numpy + multiprocessing,
so "speeding up time" is literal: every CPU core runs the un-paced sim flat out.

    python -m experiments.anchor_policy.train            # start / resume
    python -m experiments.anchor_policy.train --gens 5 --pop 16   # quick smoke

Checkpoints + an append-only JSONL learning curve land in ./checkpoints/.
Stop anytime (Ctrl-C / kill) -- the latest generation is already saved; rerun to
resume from checkpoints/state.npz.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from multiprocessing import Pool

import numpy as np

# Allow running as a script from anywhere (adds the repo root for `experiments.`).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from experiments.anchor_policy.env import AnchorEnv
from experiments.anchor_policy.policy import TinyPolicy
from experiments.anchor_policy.scenarios import scenario_batch, validation_batch

CKPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkpoints")

# Defaults (overridable via CLI).
SIZES = (8, 24, 16, 2)
DT = 0.1            # training step (runtime is 0.05; physics is identical here)
DURATION = 120.0    # seconds per episode
RADIUS = 5.0        # watch-circle radius (m)
K_TRAIN = 6         # scenarios scored per candidate per generation
K_VALID = 64        # held-out validation scenarios


def _rollout(pol: TinyPolicy, env: AnchorEnv, scenario: dict):
    obs = env.reset(scenario)
    ret = 0.0
    dists = []
    energy = 0.0
    done = False
    while not done:
        a = pol.forward(obs)
        obs, rew, done, info = env.step(a)
        ret += rew
        dists.append(info["dist"])
        energy += a[0] * a[0]
    dists = np.asarray(dists)
    return ret, dists, energy / len(dists)


def _score(args):
    """Mean episode RETURN of `theta` over a batch (gen_seed<0 -> validation)."""
    theta, sizes, gen_seed, k, dt, dur, rad = args
    pol = TinyPolicy(sizes=sizes, params=theta)
    env = AnchorEnv(dt=dt, duration_s=dur, radius_m=rad)
    batch = validation_batch(k) if gen_seed < 0 else scenario_batch(gen_seed, k)
    return float(np.mean([_rollout(pol, env, sc)[0] for sc in batch]))


def _metrics(theta, sizes, dt, dur, rad):
    """Interpretable validation metrics for the learning curve (main process)."""
    pol = TinyPolicy(sizes=sizes, params=theta)
    env = AnchorEnv(dt=dt, duration_s=dur, radius_m=rad)
    win, md, en, rr = [], [], [], []
    for sc in validation_batch(K_VALID):
        ret, dists, energy = _rollout(pol, env, sc)
        settled = dists[len(dists) // 2:]          # second half = steady state
        win.append(float(np.mean(settled <= rad) * 100.0))
        md.append(float(np.mean(settled)))
        en.append(float(energy))
        rr.append(ret)
    return {
        "val_return": float(np.mean(rr)),
        "within_pct": float(np.mean(win)),
        "mean_dist_m": float(np.mean(md)),
        "energy": float(np.mean(en)),
    }


def _centered_ranks(x: np.ndarray) -> np.ndarray:
    ranks = np.empty(len(x), dtype=np.float64)
    ranks[np.argsort(x)] = np.arange(len(x))
    return ranks / (len(x) - 1) - 0.5     # [-0.5, 0.5], higher reward -> higher


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gens", type=int, default=100_000)
    ap.add_argument("--pop", type=int, default=48)         # antithetic -> 2x rollouts
    ap.add_argument("--sigma", type=float, default=0.1)
    ap.add_argument("--lr", type=float, default=0.02)
    ap.add_argument("--k", type=int, default=K_TRAIN)
    ap.add_argument("--dt", type=float, default=DT)
    ap.add_argument("--duration", type=float, default=DURATION)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--no-resume", action="store_true")
    args = ap.parse_args()

    os.makedirs(CKPT_DIR, exist_ok=True)
    rng = np.random.default_rng(0)
    proto = TinyPolicy(sizes=SIZES, rng=rng)
    n = proto.n_params
    theta = proto.get_params()
    m_adam = np.zeros(n); v_adam = np.zeros(n)
    start_gen, best_val, adam_t = 0, -1e18, 0
    b1, b2, eps_a = 0.9, 0.999, 1e-8

    state_path = os.path.join(CKPT_DIR, "state.npz")
    if os.path.exists(state_path) and not args.no_resume:
        st = np.load(state_path)
        theta, m_adam, v_adam = st["theta"], st["m"], st["v"]
        start_gen, best_val = int(st["gen"]), float(st["best_val"])
        adam_t = int(st["adam_t"]) if "adam_t" in st.files else start_gen
        print(f"resumed at gen {start_gen} (best_val={best_val:.1f}, params={n})")
    else:
        print(f"fresh start: {n} params, pop={args.pop}x2, workers={args.workers}, dt={args.dt}")

    log_path = os.path.join(CKPT_DIR, "log.jsonl")
    pool = Pool(args.workers)
    t0 = time.time()
    try:
        for gen in range(start_gen, args.gens):
            gen_seed = (gen * 7919 + 1) & 0x7FFFFFFF
            eps = rng.standard_normal((args.pop, n))
            cands = np.concatenate([theta + args.sigma * eps, theta - args.sigma * eps])
            jobs = [(c, SIZES, gen_seed, args.k, args.dt, args.duration, RADIUS) for c in cands]
            rewards = np.array(pool.map(_score, jobs))
            util = _centered_ranks(rewards)
            up, um = util[:args.pop], util[args.pop:]
            grad = ((up - um)[:, None] * eps).sum(0) / (2 * args.pop * args.sigma)
            grad -= 0.001 * theta                       # mild L2
            # Adam ascent (bias-correct with a MONOTONIC step that survives
            # resume -- not gen-start_gen, which would reset after a restart and
            # blow up the first few steps).
            adam_t += 1
            m_adam = b1 * m_adam + (1 - b1) * grad
            v_adam = b2 * v_adam + (1 - b2) * (grad * grad)
            mhat = m_adam / (1 - b1 ** adam_t)
            vhat = v_adam / (1 - b2 ** adam_t)
            theta = theta + args.lr * mhat / (np.sqrt(vhat) + eps_a)

            if gen % 5 == 0 or gen == args.gens - 1:
                mt = _metrics(theta, SIZES, args.dt, args.duration, RADIUS)
                rate = (gen - start_gen + 1) / (time.time() - t0)
                rec = {"gen": gen, "train_return": float(rewards.mean()),
                       "gens_per_s": round(rate, 2), **mt}
                with open(log_path, "a") as f:
                    f.write(json.dumps(rec) + "\n")
                print(f"gen {gen:5d} | val_ret {mt['val_return']:8.1f} | "
                      f"within {mt['within_pct']:5.1f}% | mean_dist {mt['mean_dist_m']:4.2f}m | "
                      f"energy {mt['energy']:.3f} | {rate:.1f} gen/s", flush=True)
                TinyPolicy(sizes=SIZES, params=theta).save(
                    os.path.join(CKPT_DIR, "latest_policy.json"))
                if mt["val_return"] > best_val:
                    best_val = mt["val_return"]
                    TinyPolicy(sizes=SIZES, params=theta).save(
                        os.path.join(CKPT_DIR, "best_policy.json"))
                np.savez(state_path, theta=theta, m=m_adam, v=v_adam,
                         gen=gen + 1, best_val=best_val, adam_t=adam_t)
    except KeyboardInterrupt:
        print("\ninterrupted -- state is checkpointed; rerun to resume.")
    finally:
        pool.close(); pool.join()
        np.savez(state_path, theta=theta, m=m_adam, v=v_adam,
                 gen=gen + 1, best_val=best_val, adam_t=adam_t)
        print("checkpoint saved.")


if __name__ == "__main__":
    main()
