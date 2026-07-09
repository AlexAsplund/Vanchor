#!/usr/bin/env bash
# Stage B — launches when the stage-A (±360 azimuth) trainers exit.
# Owner plan 2026-07-09: "all out on first 360 degree azi and then 120" —
# sequential full-compute stages instead of a 4-way split. Both stages use the
# realistic actuator (95 deg/s effective slew) and warm-start from shipped.
set -u
cd "$(dirname "$0")/../.."
RUNS=experiments/anchor_policy/runs
CAPS="--history 4 --wind-cap 6 --current-cap 0.6 --gust-cap 1.5 --pop 48 --k 12 --hours 24 --steer-rate-dps 95"
PY=.venv/bin/python

while pgrep -f "anchor_policy\.train" > /dev/null; do sleep 300; done

mkdir -p "$RUNS"/{smart120b,leif120b}-20260710
setsid nohup $PY -m experiments.anchor_policy.train $CAPS --steer-range 120 \
  --workers 8 --init-policy src/vanchor/controller/anchor_policy.json \
  --ckpt-dir "$RUNS/smart120b-20260710" > "$RUNS/smart120b-20260710/train.log" 2>&1 &
setsid nohup $PY -m experiments.anchor_policy.train $CAPS --steer-range 120 --pure \
  --workers 7 --init-policy src/vanchor/controller/anchor_leif.json \
  --ckpt-dir "$RUNS/leif120b-20260710" > "$RUNS/leif120b-20260710/train.log" 2>&1 &
echo "stage B (±120) launched: $(date)"
