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
import json
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


_ENV_KW = {}   # native-env options threaded from the CLI (see main)


def _evaluate(action_fn, dt, dur, rad, k, history):
    env = AnchorEnv(dt=dt, duration_s=dur, radius_m=rad, history=history, **_ENV_KW)
    win, hold, msog, md, en, mhdg = [], [], [], [], [], []
    for sc in validation_batch(k):
        obs = env.reset(sc)
        dists, sogs, herrs, energy, done = [], [], [], 0.0, False
        while not done:
            a = action_fn(obs)
            obs, _, done, info = env.step(a)
            dists.append(info["dist"]); sogs.append(info["sog"])
            herrs.append(info["hdg_err"]); energy += a[0] * a[0]
        dists, sogs = np.asarray(dists), np.asarray(sogs)
        n2 = len(dists) // 2
        settled, ssog = dists[n2:], sogs[n2:]
        mhdg.append(float(np.mean(np.asarray(herrs)[n2:])))
        win.append(float(np.mean(settled <= rad) * 100.0))
        # hold% = inside the circle AND actually stationary (<= 0.5 m/s):
        # plain containment is gameable by orbiting at speed inside the radius
        # (the Leif v1 exploit), so the headline number requires sitting still.
        hold.append(float(np.mean((settled <= rad) & (ssog <= 0.5)) * 100.0))
        msog.append(float(np.mean(ssog)))
        md.append(float(np.mean(settled)))
        en.append(energy / len(dists))
    return np.mean(win), np.mean(hold), np.mean(msog), np.mean(mhdg), np.mean(md), np.mean(en)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default=os.path.join(CKPT_DIR, "best_policy.json"))
    ap.add_argument("--duration", type=float, default=180.0)
    ap.add_argument("--radius", type=float, default=5.0)
    ap.add_argument("--k", type=int, default=128)
    ap.add_argument("--pure", action="store_true")
    ap.add_argument("--steer-range", type=float, default=None)
    ap.add_argument("--steer-rate-dps", type=float, default=None)
    ap.add_argument("--pid-cal-deg", type=float, default=None)
    args = ap.parse_args()

    _ENV_KW.update({k: v for k, v in {
        "pure": args.pure or None,
        "steer_range_deg": args.steer_range,
        "steer_rate_dps": args.steer_rate_dps,
        "pid_cal_deg": args.pid_cal_deg,
    }.items() if v})
    pol = TinyPolicy.load(args.policy)
    with open(args.policy) as f:
        _meta = json.load(f)
    if _meta.get("obs_heading"):
        _ENV_KW["hold_heading_obs"] = True
    frame_dim = OBS_DIM + (2 if _meta.get("obs_heading") else 0)
    history = max(1, pol.sizes[0] // frame_dim)
    print(f"policy: {os.path.basename(args.policy)}  sizes={pol.sizes}  history={history}")
    print(f"validation: {args.k} held-out scenarios, {args.duration}s episodes, deployment pipeline\n")

    # Deployment conditions (5 Hz control, 1 Hz noisy/stale GPS) are baked into the env.
    w, h, sg, hd, d, e = _evaluate(lambda o: pol.forward(o), 0.2, args.duration, args.radius, args.k, history)
    print(f"  POLICY            : within {w:5.1f}% | hold {h:5.1f}% | sog {sg:4.2f} m/s | "
          f"hdg_err {hd:5.1f} deg | mean_dist {d:4.2f} m | energy {e:.3f}")
    w2, h2, sg2, hd2, d2, e2 = _evaluate(_anchor_pid, 0.2, args.duration, args.radius, args.k, 1)
    print(f"  PID AnchorHoldMode: within {w2:5.1f}% | hold {h2:5.1f}% | sog {sg2:4.2f} m/s | "
          f"hdg_err {hd2:5.1f} deg | mean_dist {d2:4.2f} m | energy {e2:.3f}")


if __name__ == "__main__":
    main()
