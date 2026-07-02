"""Offline fine-tuning of the anchor policy from recorded real-water sessions.

The app's DebugRecorder captures every telemetry frame of a session as gzipped
NDJSON (``<data_dir>/debug/<name>/0001.ndjson.gz``, chunked). This tool reads
those recordings READ-ONLY and turns them into training signal:

1. **Extract** per-tick transitions ``(obs, action, dist)`` from the telemetry
   frames recorded while an anchor mode (``anchor_hold`` / ``anchor_ml``) was
   station-keeping. The observation is rebuilt with the exact layout/scaling of
   the runtime mode (``vanchor.controller.anchor_ml._frame``) and the training
   env (``env.py``): body-frame anchor error, body-frame ground velocity
   (finite-differenced from the recorded positions), yaw rate, previous action
   (mapped back into the helm frame via the recorded thruster mount), range.

2. **Match** the recorded conditions: the persistent wind/current estimator's
   drift (``est_drift_mps/dir``) and the recorded boat spec (mass, mount,
   thrust, hull character) parameterise a *scenario distribution* centred on
   the real water the boat actually experienced.

3. **Fine-tune** the current policy checkpoint with the same OpenAI-ES used for
   base training, rolled out on those matched scenarios in the deterministic
   Fossen env -- plus an L2 trust-region pull toward the starting checkpoint so
   a short fine-tune can only *specialise*, never wander far from the shipped,
   validated policy. Pure numpy; single process (fine-tunes are short).

This is an OFFLINE tool -- it is never imported by the app. The updated policy
is written as a normal ``anchor_policy.json`` (with the ``steer_sign``
convention metadata the runtime reads); deploy it by replacing
``src/vanchor/controller/anchor_policy.json`` after eval.py sign-off.

    # inspect what a recording would contribute (no training, no writes)
    python -m experiments.anchor_policy.finetune ~/vanchor-data/debug/session-x --dry-run

    # fine-tune the shipped policy on one or more sessions
    python -m experiments.anchor_policy.finetune ~/vanchor-data/debug \
        --gens 30 --out checkpoints/finetuned_policy.json
"""

from __future__ import annotations

import argparse
import gzip
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
from experiments.anchor_policy.train import POLICY_META, _rollout

_M_PER_DEG = 111320.0
_SUFFIX = ".ndjson.gz"
_ANCHOR_MODES = ("anchor_hold", "anchor_ml")
_MOUNT_X = {"bow": 1.7, "stern": -1.7, "center": 0.0}  # matches scenarios.py
_DEFAULT_POLICY = os.path.join(
    _ROOT, "src", "vanchor", "controller", "anchor_policy.json"
)

# An episode is cut when consecutive telemetry frames are further apart than
# this (a pause / recorder gap) or when the mark moves more than _ANCHOR_JUMP_M
# (a re-drop or a jog starts a new hold).
_MAX_GAP_S = 5.0
_ANCHOR_JUMP_M = 0.75


# --------------------------------------------------------------------------- #
# Recording ingestion (READ-ONLY)
# --------------------------------------------------------------------------- #
def session_paths(path: str) -> list[str]:
    """Resolve ``path`` to a list of session paths.

    Accepts: a single legacy ``*.ndjson.gz`` file, a session directory (of
    ``NNNN.ndjson.gz`` parts), or a directory of sessions (e.g. the app's
    ``<data_dir>/debug``). Sorted for determinism."""
    if os.path.isfile(path) and path.endswith(_SUFFIX):
        return [path]
    if not os.path.isdir(path):
        raise FileNotFoundError(f"no such session file/dir: {path}")
    entries = sorted(os.listdir(path))
    if any(e.endswith(_SUFFIX) for e in entries):  # a session dir of parts
        return [path]
    out = []
    for e in entries:
        p = os.path.join(path, e)
        if os.path.isdir(p) and any(f.endswith(_SUFFIX) for f in os.listdir(p)):
            out.append(p)
        elif e.endswith(_SUFFIX):
            out.append(p)
    return out


def iter_records(session_path: str):
    """Yield parsed NDJSON records from one session (all parts, in order).

    Tolerant of a truncated final part (a crash mid-write): bad/partial lines
    are skipped rather than raising, so every intact record is still used."""
    if os.path.isdir(session_path):
        parts = [os.path.join(session_path, p)
                 for p in sorted(os.listdir(session_path)) if p.endswith(_SUFFIX)]
    else:
        parts = [session_path]
    for part in parts:
        try:
            with gzip.open(part, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue  # partial trailing line of a crashed part
        except (OSError, EOFError):
            continue  # unreadable / truncated-beyond-recovery part


# --------------------------------------------------------------------------- #
# Transition extraction
# --------------------------------------------------------------------------- #
def _mount_steer_sign(boat: dict | None) -> float:
    """The Helm's steer_sign for the recorded boat (+1 bow/center, -1 stern) --
    used to map the recorded POST-Helm motor command back into the helm frame
    the policy acts in. Mirrors BoatConfig.thruster_x_m()/Helm exactly."""
    if not isinstance(boat, dict):
        return 1.0
    off = boat.get("thruster_offset_m")
    if off is not None:
        return 1.0 if float(off) >= 0 else -1.0
    return -1.0 if boat.get("thruster_mount") == "stern" else 1.0


def _norm_deg(d: float) -> float:
    return (d + 180.0) % 360.0 - 180.0


def extract_episodes(records) -> list[dict]:
    """Extract station-keeping episodes from a session's records.

    Each episode dict:
      ``obs``   (N, 8) float64 -- runtime-layout observation frames (see
                anchor_ml._frame; stack with ``history`` at training time),
      ``act``   (N, 2) -- the helm-frame (thrust, steering) the boat executed,
      ``dist``  (N,)   -- recorded distance to the mark (m) = the outcome,
      ``radius_m``, ``drift_mps``, ``drift_dir``, ``boat`` -- the conditions.
    """
    episodes: list[dict] = []
    cur: dict | None = None
    prev = None  # (t, lat, lon, heading, act_helm[2], anchor_lat, anchor_lon)

    def _close():
        nonlocal cur
        if cur is not None and len(cur["obs"]) >= 2:
            cur["obs"] = np.asarray(cur["obs"])
            cur["act"] = np.asarray(cur["act"])
            cur["dist"] = np.asarray(cur["dist"])
            cur["drift_mps"] = float(np.mean(cur["drift_mps"])) if cur["drift_mps"] else 0.0
            cur["drift_dir"] = float(np.mean(cur["drift_dir"])) if cur["drift_dir"] else 0.0
            episodes.append(cur)
        cur = None

    for rec in records:
        if rec.get("kind") != "telemetry" or not isinstance(rec.get("data"), dict):
            continue
        d = rec["data"]
        t = float(rec.get("t", 0.0))
        pos, anchor = d.get("position"), d.get("anchor")
        if d.get("mode") not in _ANCHOR_MODES or not pos or not anchor:
            _close(); prev = None
            continue
        sign = _mount_steer_sign(d.get("boat"))
        motor = d.get("motor") or {}
        # Helm-frame action: undo the Helm's mount flip on the recorded steering.
        act = (float(motor.get("thrust", 0.0)),
               float(motor.get("steering", 0.0)) * sign)
        heading = float(d.get("heading_deg", 0.0))
        if prev is not None:
            dt = t - prev[0]
            anchor_moved = (
                math.hypot(anchor["lat"] - prev[5], anchor["lon"] - prev[6])
                * _M_PER_DEG > _ANCHOR_JUMP_M
            )
            if dt <= 0.0 or dt > _MAX_GAP_S or anchor_moved:
                _close()
                prev = (t, pos["lat"], pos["lon"], heading, act,
                        anchor["lat"], anchor["lon"])
                continue
            # -- rebuild the runtime observation frame (anchor_ml._frame) ---- #
            coslat = math.cos(math.radians(anchor["lat"]))
            dn = (anchor["lat"] - pos["lat"]) * _M_PER_DEG
            de = (anchor["lon"] - pos["lon"]) * _M_PER_DEG * coslat
            h = math.radians(heading)
            ch, sh = math.cos(h), math.sin(h)
            e_fwd = dn * ch + de * sh
            e_lat = -dn * sh + de * ch
            # Ground velocity finite-differenced from the recorded track (the
            # recording has SOG but not COG, and the difference is less noisy
            # at station-keeping speeds anyway).
            vn = (pos["lat"] - prev[1]) * _M_PER_DEG / dt
            ve = (pos["lon"] - prev[2]) * _M_PER_DEG * coslat / dt
            vg_fwd = vn * ch + ve * sh
            vg_lat = -vn * sh + ve * ch
            r = math.radians(_norm_deg(heading - prev[3])) / dt
            dist = math.hypot(dn, de)
            frame = [e_fwd / 10.0, e_lat / 10.0, vg_fwd / 1.5, vg_lat / 1.5,
                     r / 0.5, prev[4][0], prev[4][1], dist / 10.0]
            if cur is None:
                cur = {"obs": [], "act": [], "dist": [],
                       "radius_m": float(d.get("anchor_radius_m", 5.0)),
                       "drift_mps": [], "drift_dir": [],
                       "boat": d.get("boat") or {}}
            cur["obs"].append(frame)
            cur["act"].append(list(act))
            cur["dist"].append(float(d.get("distance_to_anchor_m", dist)))
            if d.get("est_drift_settled"):
                cur["drift_mps"].append(float(d.get("est_drift_mps", 0.0)))
                cur["drift_dir"].append(float(d.get("est_drift_dir", 0.0)))
        prev = (t, pos["lat"], pos["lon"], heading, act,
                anchor["lat"], anchor["lon"])
    _close()
    return episodes


# --------------------------------------------------------------------------- #
# Matched scenarios from the recorded conditions
# --------------------------------------------------------------------------- #
def derive_scenarios(episodes: list[dict], k: int, seed: int = 0) -> list[dict]:
    """Build ``k`` env scenarios centred on the recorded real-water conditions.

    The estimator's drift can't be split into wind vs current offline, so the
    measured drift is attributed to the current (jittered ±30% / ±20°) with a
    light random wind on top for robustness; the boat spec comes from the
    recorded boat profile (jittered) so the fine-tune specialises to *this*
    boat in *this* water while keeping enough spread to avoid overfitting one
    afternoon. Start offsets span the offsets actually seen in the recording."""
    if not episodes:
        raise ValueError("no station-keeping episodes to derive scenarios from")
    rng = np.random.default_rng(seed)
    drift = float(np.mean([e["drift_mps"] for e in episodes]))
    # Circular mean of the recorded drift directions, weighted by magnitude.
    dx = np.mean([e["drift_mps"] * math.cos(math.radians(e["drift_dir"])) for e in episodes])
    dy = np.mean([e["drift_mps"] * math.sin(math.radians(e["drift_dir"])) for e in episodes])
    drift_dir = math.degrees(math.atan2(dy, dx)) % 360.0
    boat = episodes[0]["boat"]
    mass = float(boat.get("mass_kg", 300.0))
    thrust = float(boat.get("max_thrust_n", 250.0))
    hull = float(boat.get("hull_tracking", 1.0))
    mount_x = _MOUNT_X.get(boat.get("thruster_mount", "bow"), 1.7)
    if boat.get("thruster_offset_m") is not None:
        mount_x = float(boat["thruster_offset_m"])
    max_dist = max(2.0, float(np.percentile(
        np.concatenate([e["dist"] for e in episodes]), 95)))

    out = []
    for i in range(k):
        cur = max(0.0, drift * rng.uniform(0.7, 1.3))
        out.append({
            "seed": int(seed + i),
            "wind_speed": float(rng.uniform(0.0, 3.0)),
            "wind_dir": float(rng.uniform(0.0, 360.0)),
            "current_speed": float(cur),
            "current_dir": float((drift_dir + rng.uniform(-20.0, 20.0)) % 360.0),
            "gust": float(rng.uniform(0.0, 0.5)),
            "gust_tau": float(rng.uniform(3.0, 8.0)),
            "wind_var": float(rng.uniform(0.0, 0.3)),
            "cur_var": float(rng.uniform(0.0, 0.2)),
            "mass": float(mass * rng.uniform(0.9, 1.1)),
            "hull_tracking": float(np.clip(hull * rng.uniform(0.8, 1.2), 0.35, 2.5)),
            "thruster_x_m": float(mount_x),
            "max_thrust_n": float(thrust * rng.uniform(0.9, 1.1)),
            "start_dist": float(rng.uniform(0.0, max_dist)),
            "start_bearing": float(rng.uniform(0.0, 2 * math.pi)),
            "heading": float(rng.uniform(0.0, 360.0)),
            "u0": float(rng.uniform(-0.3, 0.3)),
            "v0": float(rng.uniform(-0.2, 0.2)),
        })
    return out


# --------------------------------------------------------------------------- #
# ES fine-tune (single process; fine-tunes are short)
# --------------------------------------------------------------------------- #
def finetune(theta0: np.ndarray, sizes, scenarios: list[dict], *, gens: int = 30,
             pop: int = 24, sigma: float = 0.05, lr: float = 0.05,
             k: int = 8, trust: float = 0.02, radius_m: float = 5.0,
             rng: np.random.Generator | None = None, log=print) -> np.ndarray:
    """OpenAI-ES from ``theta0`` on the matched scenarios.

    Two things keep a short fine-tune NEAR the shipped, validated policy: the
    estimated gradient is normalised to unit length so each generation moves
    theta by at most ``lr`` in parameter space (a bounded step, robust to the
    rank-utility scale), and an L2 trust-region pull of strength ``trust``
    drags theta back toward ``theta0`` -- so the tool can only specialise the
    base policy, never wander far from it."""
    rng = rng or np.random.default_rng(1)
    history = max(1, int(sizes[0] // OBS_DIM))
    env = AnchorEnv(dt=0.2, duration_s=120.0, radius_m=radius_m, history=history)
    theta = theta0.copy()
    n = theta.size
    for gen in range(gens):
        batch = [scenarios[int(i)] for i in
                 rng.integers(0, len(scenarios), size=min(k, len(scenarios)))]
        eps = rng.standard_normal((pop, n))
        rewards = np.empty(2 * pop)
        for j, cand in enumerate(np.concatenate(
                [theta + sigma * eps, theta - sigma * eps])):
            pol = TinyPolicy(sizes=sizes, params=cand)
            rewards[j] = np.mean([_rollout(pol, env, sc)[0] for sc in batch])
        ranks = np.empty(2 * pop)
        ranks[np.argsort(rewards)] = np.arange(2 * pop)
        util = ranks / (2 * pop - 1) - 0.5
        grad = ((util[:pop] - util[pop:])[:, None] * eps).sum(0) / (2 * pop * sigma)
        gnorm = float(np.linalg.norm(grad))
        if gnorm > 1e-12:
            theta = theta + lr * grad / gnorm    # bounded step: |dtheta| <= lr
        theta = theta - trust * (theta - theta0)  # trust-region pull to the base
        if gen % 5 == 0 or gen == gens - 1:
            log(f"  gen {gen:3d} | mean return {rewards.mean():9.1f} | "
                f"|dtheta| {np.linalg.norm(theta - theta0):.3f}")
    return theta


def save_policy(theta: np.ndarray, sizes, path: str, sessions: list[str]) -> None:
    """Write the fine-tuned policy JSON in the exact shape the runtime loads
    (sizes/params + the steer_sign convention metadata + provenance)."""
    meta = dict(POLICY_META)
    meta["finetuned_from"] = [os.path.basename(s.rstrip("/")) for s in sessions]
    TinyPolicy(sizes=tuple(sizes), params=theta).save(path, meta=meta)


# --------------------------------------------------------------------------- #
def _summarize(episodes: list[dict]) -> dict:
    dists = np.concatenate([e["dist"] for e in episodes]) if episodes else np.zeros(0)
    frames = int(sum(len(e["obs"]) for e in episodes))
    within = [float(np.mean(e["dist"] <= e["radius_m"]) * 100.0) for e in episodes]
    return {
        "episodes": len(episodes),
        "frames": frames,
        "rms_m": float(np.sqrt(np.mean(dists ** 2))) if frames else 0.0,
        "pct_in_radius": float(np.mean(within)) if within else 0.0,
        "drift_mps": float(np.mean([e["drift_mps"] for e in episodes])) if episodes else 0.0,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sessions", nargs="+",
                    help="debug session dir(s)/file(s), or a debug/ root dir")
    ap.add_argument("--policy", default=_DEFAULT_POLICY,
                    help="checkpoint to fine-tune from (default: shipped policy)")
    ap.add_argument("--out", default=None,
                    help="where to write the updated policy JSON")
    ap.add_argument("--dry-run", action="store_true",
                    help="only report what would be extracted/trained; no writes")
    ap.add_argument("--gens", type=int, default=30)
    ap.add_argument("--pop", type=int, default=24)
    ap.add_argument("--sigma", type=float, default=0.05)
    ap.add_argument("--lr", type=float, default=0.05,
                    help="max parameter-space step per generation")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--trust", type=float, default=0.02,
                    help="per-gen L2 pull back toward the starting checkpoint")
    ap.add_argument("--scenarios", type=int, default=64)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    paths: list[str] = []
    for s in args.sessions:
        paths.extend(session_paths(s))
    if not paths:
        print("no debug sessions found", file=sys.stderr)
        return 2

    episodes: list[dict] = []
    for p in paths:
        eps = extract_episodes(iter_records(p))
        print(f"{p}: {len(eps)} episode(s), "
              f"{sum(len(e['obs']) for e in eps)} frame(s)")
        episodes.extend(eps)
    summary = _summarize(episodes)
    print(f"extracted: {summary['episodes']} episodes / {summary['frames']} frames | "
          f"recorded hold: rms {summary['rms_m']:.2f} m, "
          f"{summary['pct_in_radius']:.1f}% in radius | "
          f"drift ~{summary['drift_mps']:.2f} m/s")
    if not episodes:
        print("nothing to fine-tune on (no anchor-mode holding in the recordings)",
              file=sys.stderr)
        return 2

    scenarios = derive_scenarios(episodes, args.scenarios, seed=args.seed)
    radius = float(np.median([e["radius_m"] for e in episodes]))
    if args.dry_run:
        sc = scenarios[0]
        print(f"dry-run: would fine-tune {args.policy} for {args.gens} gens on "
              f"{len(scenarios)} matched scenarios (radius {radius:.1f} m), e.g. "
              f"current {sc['current_speed']:.2f} m/s @ {sc['current_dir']:.0f} deg, "
              f"mount x {sc['thruster_x_m']:+.1f} m, mass {sc['mass']:.0f} kg")
        return 0

    base = json.load(open(args.policy))
    sizes = tuple(base["sizes"])
    theta0 = np.asarray(base["params"], dtype=np.float64)
    print(f"fine-tuning {args.policy} ({theta0.size} params, history "
          f"{max(1, sizes[0] // OBS_DIM)}) ...")
    theta = finetune(theta0, sizes, scenarios, gens=args.gens, pop=args.pop,
                     sigma=args.sigma, lr=args.lr, k=args.k, trust=args.trust,
                     radius_m=radius, rng=np.random.default_rng(args.seed))
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "checkpoints", "finetuned_policy.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    save_policy(theta, sizes, out, paths)
    print(f"wrote {out} -- evaluate with:\n"
          f"  python -m experiments.anchor_policy.eval --policy {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
