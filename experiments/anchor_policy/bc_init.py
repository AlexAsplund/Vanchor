"""Behavior-clone a PID station-keeper into a TinyPolicy (ES warm-start).

Why this exists (2026-07-16, the orbit saga): a PURE policy trained from
scratch — and even one warm-started from the shipped orbiter — reliably falls
into the "orbit" attractor: constant full thrust, circling the mark. Full
thrust gives a young policy robust control authority immediately, so ES's
local perturbations never cross the valley to the (much higher-reward)
hold-and-trim behavior, no matter how the endpoint reward is shaped: measured
30:1 per-step reward advantage for honest holding, and two reward patches
(in-circle speed penalty, engage-heading bonus) later the retrains still sat
at energy 0.999 / mean |heading err| 90 deg for 1000+ generations.

The fix is to shape the STARTING POINT instead of the endpoint: supervise the
net onto a PID station-keeper's actions (point thrust at the anchor, thrust
proportional to distance, damped by closing speed) so ES begins INSIDE the
honest basin and merely refines. The teacher is deliberately imperfect — BC
only needs to land in the right basin, not to be good.

    python -m experiments.anchor_policy.bc_init \
        --out experiments/anchor_policy/checkpoints/bc_init_headobs.json

Numpy-only (manual backprop through the tanh MLP), a few minutes of CPU.
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
from experiments.anchor_policy.policy import ACT_DIM, OBS_DIM, TinyPolicy
from experiments.anchor_policy.scenarios import scenario_batch, validation_batch

HIDDEN = (32, 16)   # must match train.py


def teacher_action(frame: np.ndarray, steer_range_deg: float) -> np.ndarray:
    """PID-style station-keeper on ONE obs frame (same law as eval._anchor_pid,
    steering rescaled so its intent maps onto the trained steer range)."""
    e_fwd, e_lat = frame[0] * 10.0, frame[1] * 10.0
    vg_fwd, vg_lat = frame[2] * 1.5, frame[3] * 1.5
    dist = math.hypot(e_fwd, e_lat)
    closing = (vg_fwd * e_fwd + vg_lat * e_lat) / dist if dist > 1e-6 else 0.0
    thrust = float(np.clip(0.12 * dist - 0.6 * closing, -1.0, 1.0))
    # Steering intent = bearing to the anchor in the body frame (deg), expressed
    # as a fraction of the boat's trained steer range.
    intent_deg = math.degrees(math.atan2(e_lat, e_fwd))
    steer = float(np.clip(intent_deg / steer_range_deg, -1.0, 1.0))
    return np.array([thrust, steer])


def collect(k: int, history: int, steer_range: float, hold_heading_obs: bool,
            env_kw: dict) -> tuple[np.ndarray, np.ndarray]:
    env = AnchorEnv(dt=0.2, duration_s=180.0, radius_m=5.0, history=history,
                    pure=True, steer_range_deg=steer_range,
                    hold_heading_obs=hold_heading_obs, **env_kw)
    frame_dim = OBS_DIM + (2 if hold_heading_obs else 0)
    X, Y = [], []
    for sc in scenario_batch(1234, k):
        obs = env.reset(sc)
        done = False
        while not done:
            a = teacher_action(obs[-frame_dim:], steer_range)
            X.append(obs)
            Y.append(a)
            obs, _, done, _ = env.step(a)
    return np.asarray(X), np.asarray(Y)


def fit(X: np.ndarray, Y: np.ndarray, sizes: tuple, epochs: int, seed: int = 0) -> TinyPolicy:
    """Adam + MSE on the tanh-MLP (manual backprop; layout matches TinyPolicy)."""
    rng = np.random.default_rng(seed)
    pol = TinyPolicy(sizes=sizes, rng=rng)
    Ws = [(W.copy(), b.copy()) for W, b in pol._layers]
    mW = [(np.zeros_like(W), np.zeros_like(b)) for W, b in Ws]
    vW = [(np.zeros_like(W), np.zeros_like(b)) for W, b in Ws]
    lr, b1, b2, eps = 1e-3, 0.9, 0.999, 1e-8
    n, t = len(X), 0
    for ep in range(epochs):
        idx = rng.permutation(n)
        tot = 0.0
        for s in range(0, n, 512):
            bi = idx[s:s + 512]
            x, y = X[bi], Y[bi]
            # forward, caching activations
            acts = [x]
            for k, (W, b) in enumerate(Ws):
                x = x @ W + b
                x = np.tanh(x)          # tanh on EVERY layer incl. output
                acts.append(x)
            err = acts[-1] - y
            tot += float(np.mean(err ** 2)) * len(bi)
            # backward
            g = 2.0 * err / len(bi)
            grads = []
            for k in range(len(Ws) - 1, -1, -1):
                g = g * (1.0 - acts[k + 1] ** 2)         # through tanh
                gW = acts[k].T @ g
                gb = g.sum(axis=0)
                grads.append((gW, gb))
                g = g @ Ws[k][0].T
            grads.reverse()
            t += 1
            for k, ((gW, gb), (W, b)) in enumerate(zip(grads, Ws)):
                for gi, pi, mi, vi in ((0, W, mW[k][0], vW[k][0]), (1, b, mW[k][1], vW[k][1])):
                    gg = (gW, gb)[gi]
                    mi[:] = b1 * mi + (1 - b1) * gg
                    vi[:] = b2 * vi + (1 - b2) * gg * gg
                    pi -= lr * (mi / (1 - b1 ** t)) / (np.sqrt(vi / (1 - b2 ** t)) + eps)
        print(f"epoch {ep:2d} | mse {tot / n:.5f}", flush=True)
    flat = np.concatenate([np.concatenate([W.ravel(), b.ravel()]) for W, b in Ws])
    pol.set_params(flat)
    return pol


def quick_eval(pol: TinyPolicy, history: int, steer_range: float,
               hold_heading_obs: bool, env_kw: dict, k: int = 48) -> None:
    env = AnchorEnv(dt=0.2, duration_s=180.0, radius_m=5.0, history=history,
                    pure=True, steer_range_deg=steer_range,
                    hold_heading_obs=hold_heading_obs, **env_kw)
    win, hold, msog, mhdg, en = [], [], [], [], []
    for sc in validation_batch(k):
        obs = env.reset(sc)
        dists, sogs, herrs, e, done = [], [], [], 0.0, False
        while not done:
            a = pol.forward(obs)
            obs, _, done, info = env.step(a)
            dists.append(info["dist"]); sogs.append(info["sog"]); herrs.append(info["hdg_err"])
            e += a[0] * a[0]
        d, sg, hr = (np.asarray(v) for v in (dists, sogs, herrs))
        n2 = len(d) // 2
        win.append(np.mean(d[n2:] <= 5.0) * 100)
        hold.append(np.mean((d[n2:] <= 5.0) & (sg[n2:] <= 0.5)) * 100)
        msog.append(np.mean(sg[n2:])); mhdg.append(np.mean(hr[n2:])); en.append(e / len(d))
    print(f"clone eval: within {np.mean(win):5.1f}% | hold {np.mean(hold):5.1f}% | "
          f"sog {np.mean(msog):4.2f} | hdg_err {np.mean(mhdg):5.1f} | energy {np.mean(en):.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--k", type=int, default=192, help="teacher rollout scenarios")
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--history", type=int, default=4)
    ap.add_argument("--steer-range", type=float, default=120.0)
    ap.add_argument("--hold-heading-obs", action="store_true", default=True)
    args = ap.parse_args()

    env_kw = dict(wind_cap=6.0, current_cap=0.6, gust_cap=1.5, steer_rate_dps=95.0)
    frame_dim = OBS_DIM + (2 if args.hold_heading_obs else 0)
    sizes = (frame_dim * args.history,) + HIDDEN + (ACT_DIM,)

    print("collecting teacher rollouts ...", flush=True)
    X, Y = collect(args.k, args.history, args.steer_range, args.hold_heading_obs, env_kw)
    print(f"dataset: {len(X)} transitions, input dim {X.shape[1]}")
    pol = fit(X, Y, sizes, args.epochs)
    quick_eval(pol, args.history, args.steer_range, args.hold_heading_obs, env_kw)
    meta = {"steer_sign": 1.0, "train_azimuth_deg": float(args.steer_range)}
    if args.hold_heading_obs:
        meta["obs_heading"] = True
    pol.save(args.out, meta=meta)
    print("saved", args.out)


if __name__ == "__main__":
    main()
