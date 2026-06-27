"""Evaluate a trained anchor policy -- and check it transfers from the training
dt (0.1 s) to the runtime dt (0.05 s), confirming the "speed-up" didn't change
the physics the policy relies on. Also reports a simple PD baseline for context.

    python -m experiments.anchor_policy.eval                       # best, dt=0.05
    python -m experiments.anchor_policy.eval --policy checkpoints/latest_policy.json --dt 0.1
"""

from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from experiments.anchor_policy.env import AnchorEnv
from experiments.anchor_policy.policy import TinyPolicy
from experiments.anchor_policy.scenarios import validation_batch

CKPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkpoints")


def _pd_action(obs):
    """A hand-tuned PD reference: point at the anchor and thrust by range/closing."""
    e_fwd, e_lat = obs[0] * 10.0, obs[1] * 10.0
    vg_fwd = obs[2] * 1.5
    dist = math.hypot(e_fwd, e_lat)
    bearing = math.atan2(e_lat, e_fwd)               # anchor dir in body frame
    steer = float(np.clip(bearing / (math.pi / 3), -1.0, 1.0))
    thrust = float(np.clip(0.25 * dist - 0.4 * vg_fwd, -1.0, 1.0))
    return np.array([thrust, steer])


def _evaluate(action_fn, dt, dur, rad, k):
    env = AnchorEnv(dt=dt, duration_s=dur, radius_m=rad)
    win, md, en = [], [], []
    for sc in validation_batch(k):
        obs = env.reset(sc)
        dists, energy, done = [], 0.0, False
        while not done:
            a = action_fn(obs)
            obs, _, done, info = env.step(a)
            dists.append(info["dist"]); energy += a[0] * a[0]
        dists = np.asarray(dists)
        settled = dists[len(dists) // 2:]
        win.append(float(np.mean(settled <= rad) * 100.0))
        md.append(float(np.mean(settled)))
        en.append(energy / len(dists))
    return np.mean(win), np.mean(md), np.mean(en)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default=os.path.join(CKPT_DIR, "best_policy.json"))
    ap.add_argument("--dt", type=float, default=0.05)     # runtime step
    ap.add_argument("--duration", type=float, default=180.0)
    ap.add_argument("--radius", type=float, default=5.0)
    ap.add_argument("--k", type=int, default=128)
    args = ap.parse_args()

    print(f"validation: {args.k} held-out scenarios, dt={args.dt}s, {args.duration}s episodes\n")
    pol = TinyPolicy.load(args.policy)
    w, d, e = _evaluate(lambda o: pol.forward(o), args.dt, args.duration, args.radius, args.k)
    print(f"  POLICY ({os.path.basename(args.policy)}): within {w:5.1f}% | "
          f"mean_dist {d:4.2f} m | energy {e:.3f}")
    w2, d2, e2 = _evaluate(_pd_action, args.dt, args.duration, args.radius, args.k)
    print(f"  PD baseline             : within {w2:5.1f}% | "
          f"mean_dist {d2:4.2f} m | energy {e2:.3f}")


if __name__ == "__main__":
    main()
