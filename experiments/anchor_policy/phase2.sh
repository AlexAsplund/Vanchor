#!/usr/bin/env bash
# Phase-2 anchor experiments — auto-starts when the phase-1 trainers exit.
#   170-pair : how much does widening the azimuth toward the ±185° cable-wrap
#              limit buy over ±120°? (±170 leaves slew/wrap-guard margin)
#   slew-pair: actuator-fidelity training — the head slews at 50 deg/s in the
#              env, so the policy must learn to modulate thrust while the
#              motor rotates (owner experiment, 2026-07-09).
set -u
cd "$(dirname "$0")/../.."
RUNS=experiments/anchor_policy/runs
CAPS="--history 4 --wind-cap 6 --current-cap 0.6 --gust-cap 1.5 --pop 48 --k 12 --hours 24"
PY=.venv/bin/python

while pgrep -f "anchor_policy\.train" > /dev/null; do sleep 300; done

mkdir -p "$RUNS"/{smart170,leif170,smartslew,leifslew}-20260710
setsid nohup $PY -m experiments.anchor_policy.train $CAPS --steer-range 170 \
  --workers 4 --init-policy src/vanchor/controller/anchor_policy.json \
  --ckpt-dir "$RUNS/smart170-20260710" > "$RUNS/smart170-20260710/train.log" 2>&1 &
setsid nohup $PY -m experiments.anchor_policy.train $CAPS --steer-range 170 --pure \
  --workers 4 --init-policy src/vanchor/controller/anchor_leif.json \
  --ckpt-dir "$RUNS/leif170-20260710" > "$RUNS/leif170-20260710/train.log" 2>&1 &
setsid nohup $PY -m experiments.anchor_policy.train $CAPS --steer-range 120 --steer-rate-dps 50 \
  --workers 4 --init-policy src/vanchor/controller/anchor_policy.json \
  --ckpt-dir "$RUNS/smartslew-20260710" > "$RUNS/smartslew-20260710/train.log" 2>&1 &
setsid nohup $PY -m experiments.anchor_policy.train $CAPS --steer-range 120 --steer-rate-dps 50 --pure \
  --workers 3 --init-policy src/vanchor/controller/anchor_leif.json \
  --ckpt-dir "$RUNS/leifslew-20260710" > "$RUNS/leifslew-20260710/train.log" 2>&1 &
echo "phase-2 launched: $(date)"
