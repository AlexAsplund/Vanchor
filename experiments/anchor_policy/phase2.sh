#!/usr/bin/env bash
# Phase-2 anchor experiments — auto-starts when the phase-1 trainers exit.
#   180-pair : full 360° directional coverage with FORWARD thrust only
#              (the servo allows >=±360; ±180 covers every direction, beyond it
#              only helps wrap-free repointing which needs the slew model)
#   slew-pair: actuator fidelity — head slews at 50 deg/s AND has the full
#              ±360 range, so thrust modulation during rotation and wrap-free
#              shortest-path repointing are both learnable (owner experiment).
set -u
cd "$(dirname "$0")/../.."
RUNS=experiments/anchor_policy/runs
CAPS="--history 4 --wind-cap 6 --current-cap 0.6 --gust-cap 1.5 --pop 48 --k 12 --hours 24"
PY=.venv/bin/python

while pgrep -f "anchor_policy\.train" > /dev/null; do sleep 300; done

mkdir -p "$RUNS"/{smart180,leif180,smartslew,leifslew}-20260710
setsid nohup $PY -m experiments.anchor_policy.train $CAPS --steer-range 180 \
  --workers 4 --init-policy src/vanchor/controller/anchor_policy.json \
  --ckpt-dir "$RUNS/smart180-20260710" > "$RUNS/smart180-20260710/train.log" 2>&1 &
setsid nohup $PY -m experiments.anchor_policy.train $CAPS --steer-range 180 --pure \
  --workers 4 --init-policy src/vanchor/controller/anchor_leif.json \
  --ckpt-dir "$RUNS/leif180-20260710" > "$RUNS/leif180-20260710/train.log" 2>&1 &
setsid nohup $PY -m experiments.anchor_policy.train $CAPS --steer-range 360 --steer-rate-dps 50 \
  --workers 4 --init-policy src/vanchor/controller/anchor_policy.json \
  --ckpt-dir "$RUNS/smartslew-20260710" > "$RUNS/smartslew-20260710/train.log" 2>&1 &
setsid nohup $PY -m experiments.anchor_policy.train $CAPS --steer-range 360 --steer-rate-dps 50 --pure \
  --workers 3 --init-policy src/vanchor/controller/anchor_leif.json \
  --ckpt-dir "$RUNS/leifslew-20260710" > "$RUNS/leifslew-20260710/train.log" 2>&1 &
echo "phase-2 launched: $(date)"
