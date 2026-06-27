"""Evaluate a trained anchor policy on held-out scenarios.

Reports: (1) the held-out hold metrics, (2) **transfer** from the training step
(dt=0.1) to the runtime step (dt=0.05) -- confirming the "speed-up" didn't change
the physics the policy relies on, and (3) a comparison against a faithful
re-implementation of the PID `AnchorHoldMode` control law (the classical baseline
every DP-RL paper includes). v2 frame-stacked policies are handled automatically
(history is inferred from the network's input width).

    python -m experiments.anchor_policy.eval
    python -m experiments.anchor_policy.eval --policy checkpoints/best_policy.json --k 128
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
from experiments.anchor_policy.policy import OBS_DIM, TinyPolicy
from experiments.anchor_policy.scenarios import validation_batch

CKPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkpoints")


def _anchor_pid(obs, kp=0.12, kd=0.6):
    """The PID `AnchorHoldMode` law (src/vanchor/controller/modes.py AnchorConfig):
    thrust = kp*range - kd*closing_speed (the kd term anticipates drift via the
    ground-velocity component toward the mark), bow pointed at the mark. Reads the
    last observation frame, so it works for v1 and v2 policies' envs alike."""
    f = obs[-OBS_DIM:]
    e_fwd, e_lat = f[0] * 10.0, f[1] * 10.0          # un-normalise (see env._frame)
    vg_fwd, vg_lat = f[2] * 1.5, f[3] * 1.5
    dist = math.hypot(e_fwd, e_lat)
    closing = (vg_fwd * e_fwd + vg_lat * e_lat) / dist if dist > 1e-6 else 0.0
    thrust = float(np.clip(kp * dist - kd * closing, -1.0, 1.0))
    steer = float(np.clip(math.atan2(e_lat, e_fwd) / (math.pi / 3), -1.0, 1.0))
    return np.array([thrust, steer])


def _evaluate(action_fn, dt, dur, rad, k, history):
    env = AnchorEnv(dt=dt, duration_s=dur, radius_m=rad, history=history)
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
    ap.add_argument("--duration", type=float, default=180.0)
    ap.add_argument("--radius", type=float, default=5.0)
    ap.add_argument("--k", type=int, default=128)
    args = ap.parse_args()

    pol = TinyPolicy.load(args.policy)
    history = max(1, pol.sizes[0] // OBS_DIM)
    print(f"policy: {os.path.basename(args.policy)}  sizes={pol.sizes}  history={history}")
    print(f"validation: {args.k} held-out scenarios, {args.duration}s episodes\n")

    # Transfer check: train dt (0.1) vs runtime dt (0.05) should match closely.
    for dt in (0.10, 0.05):
        w, d, e = _evaluate(lambda o: pol.forward(o), dt, args.duration, args.radius, args.k, history)
        tag = "train dt" if dt == 0.10 else "RUNTIME dt"
        print(f"  POLICY @ dt={dt:.2f} ({tag:9s}): within {w:5.1f}% | mean_dist {d:4.2f} m | energy {e:.3f}")

    w2, d2, e2 = _evaluate(_anchor_pid, 0.05, args.duration, args.radius, args.k, 1)
    print(f"  PID AnchorHoldMode @ dt=0.05    : within {w2:5.1f}% | mean_dist {d2:4.2f} m | energy {e2:.3f}")


if __name__ == "__main__":
    main()
