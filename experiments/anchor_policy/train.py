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
from experiments.anchor_policy.policy import ACT_DIM, OBS_DIM, TinyPolicy
from experiments.anchor_policy.scenarios import scenario_batch, validation_batch

CKPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkpoints")

# Defaults (overridable via CLI).
HIDDEN = (32, 16)   # net shape = (OBS_DIM*history,) + HIDDEN + (ACT_DIM,)
DT = 0.2            # CONTROL period = the 5 Hz runtime rate (physics sub-steps internally)
DURATION = 120.0    # seconds per episode
RADIUS = 5.0        # watch-circle radius (m)
K_TRAIN = 10        # scenarios scored per candidate per generation (v2: lower variance)
K_VALID = 64        # held-out validation scenarios

# Steering-polarity convention this training pipeline produces (recorded into
# every saved policy JSON so the runtime can map the residual correctly): the
# env normalises all mounts into the helm frame (+steering = starboard), i.e.
# the bow/raw convention -> +1. See env.py ``_steer_sign``.
POLICY_META = {"steer_sign": 1.0}

# EXPERIMENT globals (set in main before the Pool forks so workers inherit them).
_PURE = False
_STEER = None
_STEER_RATE = None
_PID_CAL = None
_WIND_CAP = None
_CUR_CAP = None
_GUST_CAP = None
_SPEED_PEN = 0.0
_HEADING_BONUS = 0.0
_HOLD_HEAD_OBS = False
_YAW_PEN = 0.0
_DQ_ROT = 0.0


def _rollout(pol: TinyPolicy, env: AnchorEnv, scenario: dict):
    obs = env.reset(scenario)
    ret = 0.0
    dists = []
    sogs = []
    herrs = []
    energy = 0.0
    done = False
    while not done:
        a = pol.forward(obs)
        obs, rew, done, info = env.step(a)
        ret += rew
        dists.append(info["dist"])
        sogs.append(info["sog"])
        herrs.append(info["hdg_err"])
        energy += a[0] * a[0]
    dists = np.asarray(dists)
    return ret, dists, energy / len(dists), np.asarray(sogs), np.asarray(herrs)


def _score(args):
    """Mean episode RETURN of `theta` over a batch (gen_seed<0 -> validation).

    Reward weights ride the ARGS (not the frozen worker globals) so the
    adaptive-curriculum controller (--adapt) can change them mid-run."""
    theta, sizes, gen_seed, k, dt, dur, rad, history, arate, anticip, wts = args
    pol = TinyPolicy(sizes=sizes, params=theta)
    env = AnchorEnv(dt=dt, duration_s=dur, radius_m=rad, history=history, arate=arate, anticip=anticip, pure=_PURE, steer_range_deg=_STEER, wind_cap=_WIND_CAP, current_cap=_CUR_CAP, gust_cap=_GUST_CAP, steer_rate_dps=_STEER_RATE, pid_cal_deg=_PID_CAL, speed_pen=wts[0], heading_bonus=wts[1], hold_heading_obs=_HOLD_HEAD_OBS, yaw_pen=wts[2], dq_rotation_deg=_DQ_ROT)
    batch = validation_batch(k) if gen_seed < 0 else scenario_batch(gen_seed, k)
    return float(np.mean([_rollout(pol, env, sc)[0] for sc in batch]))


def _metrics(theta, sizes, dt, dur, rad, history, arate, anticip, wts):
    """Interpretable validation metrics for the learning curve (main process)."""
    pol = TinyPolicy(sizes=sizes, params=theta)
    env = AnchorEnv(dt=dt, duration_s=dur, radius_m=rad, history=history, arate=arate, anticip=anticip, pure=_PURE, steer_range_deg=_STEER, wind_cap=_WIND_CAP, current_cap=_CUR_CAP, gust_cap=_GUST_CAP, steer_rate_dps=_STEER_RATE, pid_cal_deg=_PID_CAL, speed_pen=wts[0], heading_bonus=wts[1], hold_heading_obs=_HOLD_HEAD_OBS, yaw_pen=wts[2], dq_rotation_deg=_DQ_ROT)
    win, md, en, rr, hold, msog, mhdg, dq = [], [], [], [], [], [], [], []
    for sc in validation_batch(K_VALID):
        ret, dists, energy, sogs, herrs = _rollout(pol, env, sc)
        n2 = len(dists) // 2                       # second half = steady state
        settled, ssog = dists[n2:], sogs[n2:]
        mhdg.append(float(np.mean(herrs[n2:])))
        dq.append(1.0 if env._dq else 0.0)
        win.append(float(np.mean(settled <= rad) * 100.0))
        # hold% = settled AND slow: containment alone is gameable by orbiting
        # inside the circle at speed (the Leif v1 exploit), so the headline
        # metric requires actually sitting still (<= 0.5 m/s).
        hold.append(float(np.mean((settled <= rad) & (ssog <= 0.5)) * 100.0))
        msog.append(float(np.mean(ssog)))
        md.append(float(np.mean(settled)))
        en.append(float(energy))
        rr.append(ret)
    return {
        "val_return": float(np.mean(rr)),
        "within_pct": float(np.mean(win)),
        "hold_pct": float(np.mean(hold)),
        "mean_sog": float(np.mean(msog)),
        "mean_hdg_err": float(np.mean(mhdg)),
        "dq_pct": float(np.mean(dq) * 100.0),
        "mean_dist_m": float(np.mean(md)),
        "energy": float(np.mean(en)),
    }


def _canon_score(mt: dict) -> float:
    """FIXED merit score for best-checkpoint selection, independent of the
    (possibly adapting) reward weights. val_return is incomparable across
    weight changes -- selecting on it would make best_policy chase whichever
    trait was most recently up-weighted. Mirrors the promote gauntlet:
    stationary containment first, then heading, dq heavily charged."""
    return (mt["hold_pct"] + 0.25 * mt["within_pct"]
            - 0.5 * mt["mean_hdg_err"] - 2.0 * mt["dq_pct"])


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
    ap.add_argument("--history", type=int, default=1)    # v2: stacked obs frames
    ap.add_argument("--arate", type=float, default=0.0)   # v2: action-rate penalty
    ap.add_argument("--anticip", type=float, default=0.0)  # v6: anticipation (outward-drift) penalty
    ap.add_argument("--speed-pen", type=float, default=0.0)  # v7: in-circle ground-speed^2 penalty (orbit-exploit fix)
    ap.add_argument("--heading-bonus", type=float, default=0.0)  # v7: hold-engage-heading bonus while inside the circle
    ap.add_argument("--hold-heading-obs", action="store_true")   # v7: append sin/cos heading error to the obs (frame 8 -> 10)
    ap.add_argument("--yaw-pen", type=float, default=0.0)         # v7b: near-mark yaw-rate^2 penalty (pirouette fix)
    ap.add_argument("--dq-rotation", type=float, default=0.0)     # v8: disqualify past +/- this net rotation (deg, 0=off)
    # v9: adaptive trait pressure (owner idea): every --adapt-every gens,
    # raise the weight of each trait that misses its target (x1.25, capped at
    # 16x the CLI base) and decay met-with-margin weights back toward base
    # (x0.95, never below). Met traits are protected by hysteresis: if one
    # regresses past its target its weight climbs again. Best checkpoint is
    # selected by the FIXED _canon_score, not the moving val_return.
    ap.add_argument("--adapt", action="store_true")
    ap.add_argument("--adapt-every", type=int, default=50)
    ap.add_argument("--target-hold", type=float, default=80.0)   # hold_pct >=
    ap.add_argument("--target-hdg", type=float, default=20.0)    # mean_hdg_err <=
    ap.add_argument("--target-dq", type=float, default=0.0)      # dq_pct <=
    ap.add_argument("--pure", action="store_true")         # EXPERIMENT: command = net (no PID base)
    ap.add_argument("--steer-range", type=float, default=None)  # EXPERIMENT: wide azimuth (deg)
    ap.add_argument("--ckpt-dir", default=CKPT_DIR)
    ap.add_argument("--wind-cap", type=float, default=None)     # EXPERIMENT: cap wind (m/s)
    ap.add_argument("--current-cap", type=float, default=None)  # EXPERIMENT: cap current (m/s)
    ap.add_argument("--gust-cap", type=float, default=None)     # EXPERIMENT: cap gust amplitude
    ap.add_argument("--steer-rate-dps", type=float, default=None,
                    help="EXPERIMENT: model the head's slew rate (deg/s); the policy "
                         "must learn thrust modulation while the head swings")
    ap.add_argument("--pid-cal-deg", type=float, default=None,
                    help="rescale the hybrid PID base's steering to this design range "
                         "when steer-range is wider (residual keeps full authority)")
    ap.add_argument("--hours", type=float, default=None,
                    help="wall-clock budget; stop cleanly (checkpointed) when exceeded")
    ap.add_argument("--init-policy", default=None,
                    help="warm-start theta from a policy JSON (fresh starts only; "
                         "sizes must match the --history-derived net shape)")
    args = ap.parse_args()
    global _PURE, _STEER, _WIND_CAP, _CUR_CAP, _GUST_CAP, _STEER_RATE, _PID_CAL, _SPEED_PEN
    global _HEADING_BONUS, _HOLD_HEAD_OBS, POLICY_META, _YAW_PEN, _DQ_ROT
    _PURE, _STEER = args.pure, args.steer_range
    _SPEED_PEN = args.speed_pen
    _HEADING_BONUS = args.heading_bonus
    _HOLD_HEAD_OBS = args.hold_heading_obs
    _YAW_PEN = args.yaw_pen
    _DQ_ROT = args.dq_rotation
    # Stamp deployment-relevant training facts into the policy JSON so the
    # runtime mode + eval can reconstruct the matching pipeline (azimuth
    # rescale, heading-aware obs) without manual editing.
    POLICY_META = dict(POLICY_META)
    if args.steer_range:
        POLICY_META["train_azimuth_deg"] = float(args.steer_range)
    if args.hold_heading_obs:
        POLICY_META["obs_heading"] = True
    _STEER_RATE = args.steer_rate_dps
    _PID_CAL = args.pid_cal_deg
    _WIND_CAP, _CUR_CAP, _GUST_CAP = args.wind_cap, args.current_cap, args.gust_cap

    ckpt = args.ckpt_dir
    os.makedirs(ckpt, exist_ok=True)
    rng = np.random.default_rng(0)
    frame_dim = OBS_DIM + (2 if args.hold_heading_obs else 0)
    sizes = (frame_dim * args.history,) + HIDDEN + (ACT_DIM,)
    proto = TinyPolicy(sizes=sizes, rng=rng)
    n = proto.n_params
    theta = proto.get_params()
    m_adam = np.zeros(n); v_adam = np.zeros(n)
    start_gen, best_val, adam_t = 0, -1e18, 0
    b1, b2, eps_a = 0.9, 0.999, 1e-8

    # live reward weights (adapted when --adapt; constant otherwise)
    wts = [args.speed_pen, args.heading_bonus, args.yaw_pen]
    w_base = list(wts)
    best_canon = -1e18

    state_path = os.path.join(ckpt, "state.npz")
    if os.path.exists(state_path) and not args.no_resume:
        st = np.load(state_path)
        theta, m_adam, v_adam = st["theta"], st["m"], st["v"]
        start_gen, best_val = int(st["gen"]), float(st["best_val"])
        adam_t = int(st["adam_t"]) if "adam_t" in st.files else start_gen
        if "wts" in st.files:
            wts = [float(x) for x in st["wts"]]
        if "best_canon" in st.files:
            best_canon = float(st["best_canon"])
        # Restore the perturbation RNG so a resume continues the EXACT stream
        # (previously a restart replayed a fresh stream from seed 0).
        if "rng_state" in st.files:
            rng.bit_generator.state = json.loads(str(st["rng_state"]))
        print(f"resumed at gen {start_gen} (best_val={best_val:.1f}, params={n})")
    elif args.init_policy:
        init = TinyPolicy.load(args.init_policy)
        if tuple(init.sizes) != tuple(sizes):
            raise SystemExit(f"--init-policy sizes {tuple(init.sizes)} != net {tuple(sizes)} "
                             f"(check --history)")
        theta = init.get_params()
        print(f"warm start from {args.init_policy}: {n} params, pop={args.pop}x2, "
              f"workers={args.workers}, dt={args.dt}")
    else:
        print(f"fresh start: {n} params, pop={args.pop}x2, workers={args.workers}, dt={args.dt}")

    log_path = os.path.join(ckpt, "log.jsonl")
    pool = Pool(args.workers)
    t0 = time.time()
    try:
        for gen in range(start_gen, args.gens):
            if args.hours is not None and (time.time() - t0) > args.hours * 3600.0:
                print(f"wall-clock budget ({args.hours} h) reached at gen {gen}; stopping.")
                break
            gen_seed = (gen * 7919 + 1) & 0x7FFFFFFF
            eps = rng.standard_normal((args.pop, n))
            cands = np.concatenate([theta + args.sigma * eps, theta - args.sigma * eps])
            wtup = tuple(wts)
            jobs = [(c, sizes, gen_seed, args.k, args.dt, args.duration, RADIUS,
                     args.history, args.arate, args.anticip, wtup) for c in cands]
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
                mt = _metrics(theta, sizes, args.dt, args.duration, RADIUS,
                              args.history, args.arate, args.anticip, tuple(wts))
                canon = _canon_score(mt)
                rate = (gen - start_gen + 1) / (time.time() - t0)
                rec = {"gen": gen, "train_return": float(rewards.mean()),
                       "gens_per_s": round(rate, 2), "canon": round(canon, 2),
                       "w_speed": round(wts[0], 3), "w_head": round(wts[1], 3),
                       "w_yaw": round(wts[2], 3), **mt}
                with open(log_path, "a") as f:
                    f.write(json.dumps(rec) + "\n")
                print(f"gen {gen:5d} | val_ret {mt['val_return']:8.1f} | "
                      f"within {mt['within_pct']:5.1f}% | hold {mt['hold_pct']:5.1f}% | "
                      f"sog {mt['mean_sog']:4.2f} | hdg {mt['mean_hdg_err']:5.1f} | dq {mt['dq_pct']:3.0f}% | mean_dist {mt['mean_dist_m']:4.2f}m | "
                      f"energy {mt['energy']:.3f} | {rate:.1f} gen/s", flush=True)
                TinyPolicy(sizes=sizes, params=theta).save(
                    os.path.join(ckpt, "latest_policy.json"), meta=POLICY_META)
                # Best selection: FIXED canon score under --adapt (val_return
                # is incomparable across weight changes); legacy val_return
                # otherwise (unchanged behaviour for old recipes).
                if args.adapt:
                    if canon > best_canon:
                        best_canon = canon
                        TinyPolicy(sizes=sizes, params=theta).save(
                            os.path.join(ckpt, "best_policy.json"), meta=POLICY_META)
                elif mt["val_return"] > best_val:
                    best_val = mt["val_return"]
                    TinyPolicy(sizes=sizes, params=theta).save(
                        os.path.join(ckpt, "best_policy.json"), meta=POLICY_META)
                # Adaptive trait pressure: raise lagging traits' weights, decay
                # met-with-margin ones back toward base (never below base).
                if args.adapt and gen > start_gen and gen % args.adapt_every == 0:
                    CAP = 16.0
                    changes = []
                    def _bump(i, name):
                        old = wts[i]
                        wts[i] = min(wts[i] * 1.25, w_base[i] * CAP)
                        if wts[i] != old:
                            changes.append(f"{name} {old:.2f}->{wts[i]:.2f}")
                    def _decay(i, name):
                        old = wts[i]
                        wts[i] = max(wts[i] * 0.95, w_base[i])
                        if abs(wts[i] - old) > 1e-9:
                            changes.append(f"{name} {old:.2f}->{wts[i]:.2f} (relax)")
                    if mt["hold_pct"] < args.target_hold:
                        _bump(0, "speed_pen")
                    elif mt["hold_pct"] > args.target_hold + 5:
                        _decay(0, "speed_pen")
                    if mt["mean_hdg_err"] > args.target_hdg:
                        _bump(1, "heading_bonus")
                    elif mt["mean_hdg_err"] < args.target_hdg * 0.7:
                        _decay(1, "heading_bonus")
                    if mt["dq_pct"] > args.target_dq:
                        _bump(2, "yaw_pen")
                    elif mt["dq_pct"] <= args.target_dq:
                        _decay(2, "yaw_pen")
                    if changes:
                        print(f"adapt @gen {gen}: " + "; ".join(changes), flush=True)
                np.savez(state_path, theta=theta, m=m_adam, v=v_adam,
                         gen=gen + 1, best_val=best_val, adam_t=adam_t,
                         best_canon=best_canon, wts=np.array(wts),
                         rng_state=json.dumps(rng.bit_generator.state))
    except KeyboardInterrupt:
        print("\ninterrupted -- state is checkpointed; rerun to resume.")
    finally:
        pool.close(); pool.join()
        np.savez(state_path, theta=theta, m=m_adam, v=v_adam,
                 gen=gen + 1, best_val=best_val, adam_t=adam_t,
                 best_canon=best_canon, wts=np.array(wts),
                 rng_state=json.dumps(rng.bit_generator.state))
        print("checkpoint saved.")


if __name__ == "__main__":
    main()
