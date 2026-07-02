"""Learned virtual-anchor mode (``anchor_ml``): a tiny neural-net station-keeper.

A drop-in alternative to :class:`AnchorHoldMode`. A ~1.6k-parameter numpy MLP --
trained offline by Evolution Strategies on the Fossen physics across thousands of
randomised wind/current/boat scenarios (see ``experiments/anchor_policy/``) --
maps the boat's *perceived* station-keeping state directly to a motor command.
Pure numpy, no ML runtime: a forward pass is a few small matrix multiplies
(microseconds on a Raspberry Pi).

The observation is built to MATCH the training environment
(``experiments/anchor_policy/env.py``::``_frame``) exactly -- body-frame
anchor-position error, body-frame ground velocity, yaw rate, the previous action,
and range -- all from the same GPS/compass the rest of the controller uses. The
last ``history`` frames are stacked so the memoryless MLP can infer the
unobserved wind/current from the recent motion trend.
"""

from __future__ import annotations

import json
import math
import os
from collections import deque

import numpy as np

from ..core.geo import angle_difference, haversine_m, initial_bearing, knots_to_mps
from ..core.models import ControlModeName, ManualSetpoint
from ..core.state import NavigationState

_MODEL_PATH = os.path.join(os.path.dirname(__file__), "anchor_policy.json")
_M_PER_DEG = 111320.0
_OBS_DIM = 8


def pid_base(e_fwd, e_lat, vg_fwd, vg_lat, kp=0.12, kd=0.6, deadband=0.8):
    """Robust spot-lock base law (the AnchorHoldMode behaviour), from body-frame
    anchor error + ground velocity -> (thrust, steering). Idles inside a deadband;
    otherwise drives toward the mark, BACKING straight up when the mark is astern
    (instead of looping around, the naive-PID divergence). The shared base for the
    training env AND the deployed hybrid mode, so they match exactly."""
    dist = math.hypot(e_fwd, e_lat)
    if dist <= deadband:
        return 0.0, 0.0
    closing = (vg_fwd * e_fwd + vg_lat * e_lat) / dist   # +ve = approaching
    mag = min(1.0, max(0.0, kp * dist - kd * closing))
    if e_fwd >= 0.0:                                      # mark ahead -> forward
        return mag, max(-1.0, min(1.0, math.atan2(e_lat, e_fwd) / (math.pi / 4)))
    # mark astern -> reverse straight back; steering sign flips under reverse thrust
    return -mag, max(-1.0, min(1.0, -math.atan2(e_lat, -e_fwd) / (math.pi / 4)))


class _TinyMLP:
    """tanh-MLP inference; matches ``experiments/anchor_policy/policy.py``."""

    def __init__(self, sizes, params) -> None:
        self.sizes = tuple(sizes)
        theta = np.asarray(params, dtype=np.float64)
        self.layers = []
        i = 0
        for a, b in zip(self.sizes[:-1], self.sizes[1:]):
            w = theta[i:i + a * b].reshape(a, b); i += a * b
            bias = theta[i:i + b]; i += b
            self.layers.append((w, bias))

    def forward(self, x) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        last = len(self.layers) - 1
        for k, (w, bias) in enumerate(self.layers):
            x = x @ w + bias
            if k < last:
                x = np.tanh(x)
        return np.tanh(x)

    @classmethod
    def load(cls, path: str) -> "_TinyMLP":
        with open(path) as f:
            d = json.load(f)
        return cls(d["sizes"], d["params"])


class AnchorMLMode:
    """Hybrid learned spot-lock: a robust PID base plus a small bounded learned
    residual -- ``command = clip(pid_base + 0.3 * net(obs))``. The base (deadband
    idle, drive-to-mark, reverse-when-astern) provides robustness and the
    idle-at-rest guarantee; the tiny net (trained on the real deployment sensor
    pipeline) adds a correction that tightens the hold. Bounded by construction,
    so the worst case is just the PID. Produces a ManualSetpoint, holds
    ``state.anchor``. (Eval on the sign-faithful deployment pipeline: at rough
    parity with the PID base on time-in-radius (~75% vs 75.6%) but holds a
    *tighter* mean distance at **3-4x less motor energy** -- and recovers the
    stern mount, which the pre-retrain policy held at only 61.5% vs PID's 77%.
    The runtime residual-decay guardrail floors it to the PID base if it ever
    underperforms, so worst-case is still just the PID.)

    **Steering polarity / thruster mount (v2).** The mode emits its command in
    the *helm frame*: positive steering always turns the boat to starboard,
    because the Helm multiplies the whole ManualSetpoint by the boat's
    ``steer_sign`` (+1 bow mount, -1 stern) downstream -- that flip is what keeps
    a stern-mounted boat stable, for the PID base and the residual alike. The
    residual net, however, carries its own trained polarity: the shipped policy
    was trained in the bow/raw convention (identical to the helm frame), but a
    model file may declare ``"steer_sign": -1`` (trained raw on a stern mount),
    in which case the steering residual is flipped into the helm frame here so a
    mis-matched convention can never destabilise the hold. The mode also mirrors
    the boat's live ``steer_sign`` (kept in sync with the Helm by the app when
    boat profiles change) purely for telemetry/diagnosis -- it must NOT be
    applied to the command a second time, the Helm owns that flip.

    **Residual-decay guardrail (v2).** Pure PID is the safety floor, so the mode
    watches its own holding performance: an EMA (time constant
    ``guard_window_s``) of ``distance_to_anchor / anchor_radius``. If the boat is
    persistently outside the watch circle (EMA > ``guard_bad_ratio``) the
    effective residual scale decays exponentially toward ``guard_min_scale``
    (falling back to the known-good PID base); once holding is good again
    (EMA < ``guard_good_ratio``) it recovers toward the nominal
    ``residual_scale``. The gap between the two thresholds is a hysteresis dead
    band (no flapping), and recovery is deliberately slower than decay. A fresh
    activation always starts at the nominal scale."""

    name = ControlModeName.ANCHOR_ML

    def __init__(self, model_path: str = _MODEL_PATH, residual_scale: float = 0.3,
                 steer_sign: float = 1.0, *, guard_window_s: float = 30.0,
                 guard_bad_ratio: float = 1.0, guard_good_ratio: float = 0.7,
                 guard_decay_s: float = 15.0, guard_recover_s: float = 45.0,
                 guard_min_scale: float = 0.0) -> None:
        with open(model_path) as f:
            d = json.load(f)
        self._mlp = _TinyMLP(d["sizes"], d["params"])
        # Steering-polarity convention the residual net was trained in, recorded
        # in the model file by train/finetune: +1 = helm frame (bow/raw -- the
        # shipped policy), -1 = raw stern convention (flip into the helm frame).
        self.policy_steer_sign = 1.0 if float(d.get("steer_sign", 1.0)) >= 0 else -1.0
        self.history = max(1, self._mlp.sizes[0] // _OBS_DIM)
        self.residual_scale = residual_scale          # nominal (fresh-activation) scale
        # Boat mount sign, mirroring Helm.steer_sign (informational -- see class
        # docstring: the Helm applies it to the emitted command, not this mode).
        self.steer_sign = 1.0 if steer_sign >= 0 else -1.0
        # Guardrail configuration + live state.
        self.guard_window_s = guard_window_s
        self.guard_bad_ratio = guard_bad_ratio
        self.guard_good_ratio = min(guard_good_ratio, guard_bad_ratio)
        self.guard_decay_s = max(1e-6, guard_decay_s)
        self.guard_recover_s = max(1e-6, guard_recover_s)
        self.guard_min_scale = max(0.0, guard_min_scale)
        self._scale_eff = residual_scale
        self._hold_ema = 0.0
        self._hist: deque | None = None
        self._prev = np.zeros(2)
        self._prev_heading: float | None = None

    @property
    def residual_scale_effective(self) -> float:
        """The residual scale actually applied this tick (guardrail output),
        in [guard_min_scale, residual_scale]. Exposed for telemetry/debug."""
        return self._scale_eff

    @property
    def guard_hold_ratio(self) -> float:
        """The guardrail's EMA of distance/radius (its degradation signal)."""
        return self._hold_ema

    def activate(self, state: NavigationState) -> None:
        self._hist = None
        self._prev = np.zeros(2)
        self._prev_heading = None
        # Fresh activation: nominal residual scale, benign holding estimate
        # (the guardrail only decays after ~guard_window_s of observed drift).
        self._scale_eff = self.residual_scale
        self._hold_ema = 0.0

    def _update_guard(self, state: NavigationState, dt: float) -> None:
        """Online self-check: decay the residual toward the PID floor when the
        hold is degrading, recover it when the hold is good. Hysteretic (a dead
        band between the good/bad thresholds) and bounded on both sides."""
        if dt <= 0.0 or state.anchor is None or state.position is None:
            return
        radius = max(0.5, state.anchor_radius_m)
        ratio = state.distance_to_anchor_m / radius
        alpha = dt / (self.guard_window_s + dt)
        self._hold_ema += (ratio - self._hold_ema) * alpha
        if self._hold_ema > self.guard_bad_ratio:      # hybrid is hurting -> PID floor
            k = math.exp(-dt / self.guard_decay_s)
            self._scale_eff = self.guard_min_scale + (self._scale_eff - self.guard_min_scale) * k
        elif self._hold_ema < self.guard_good_ratio:   # holding well -> recover
            k = math.exp(-dt / self.guard_recover_s)
            self._scale_eff = self.residual_scale + (self._scale_eff - self.residual_scale) * k
        # else: inside the hysteresis band -> hold the current scale (no flap).

    # One observation frame, identical in layout/scaling to the training env.
    def _frame(self, state: NavigationState, dt: float) -> np.ndarray:
        anchor, pos = state.anchor, state.position
        h = math.radians(state.heading_deg)
        ch, sh = math.cos(h), math.sin(h)
        if anchor is None or pos is None:
            dn = de = 0.0
        else:
            coslat = math.cos(math.radians(anchor.lat))
            dn = (anchor.lat - pos.lat) * _M_PER_DEG
            de = (anchor.lon - pos.lon) * _M_PER_DEG * coslat
        e_fwd = dn * ch + de * sh          # anchor position relative to the bow
        e_lat = -dn * sh + de * ch
        fix = state.fix
        if fix is not None:
            v = knots_to_mps(fix.sog_knots)
            cog = math.radians(fix.cog_deg)
            vn, ve = v * math.cos(cog), v * math.sin(cog)
        else:
            vn = ve = 0.0
        vg_fwd = vn * ch + ve * sh          # ground velocity, body frame
        vg_lat = -vn * sh + ve * ch
        if self._prev_heading is None or dt <= 0:
            r = 0.0                          # no yaw-rate sensor -> heading diff
        else:
            r = math.radians(angle_difference(self._prev_heading, state.heading_deg)) / dt
        dist = math.hypot(dn, de)
        return np.array([
            e_fwd / 10.0, e_lat / 10.0, vg_fwd / 1.5, vg_lat / 1.5,
            r / 0.5, self._prev[0], self._prev[1], dist / 10.0,
        ])

    def update(self, state: NavigationState, dt: float) -> ManualSetpoint:
        # Keep the HUD range/bearing fresh AND feed the safety governor's drag
        # alarm (which reads state.distance_to_anchor_m), exactly like
        # AnchorHoldMode -- otherwise the learned spot-lock would show stale
        # distance and never trip a drag alarm.
        anchor, pos = state.anchor, state.position
        if anchor is not None and pos is not None:
            state.distance_to_anchor_m = haversine_m(pos, anchor)
            state.bearing_to_dest = initial_bearing(pos, anchor)

        # Residual-decay guardrail: track the actual holding quality and shrink
        # the residual toward the pure-PID floor when the hybrid is hurting.
        self._update_guard(state, dt)

        frame = self._frame(state, dt)
        if self._hist is None:
            self._hist = deque([frame] * self.history, maxlen=self.history)
        else:
            self._hist.append(frame)
        residual = self._mlp.forward(np.concatenate(self._hist))
        # Hybrid: robust PID base (from this frame) + the bounded learned
        # residual. Both are in the HELM frame (+steering = starboard; the Helm
        # applies the boat's mount steer_sign downstream): the steering residual
        # is mapped from its trained polarity via ``policy_steer_sign``.
        scale = self._scale_eff
        pid_th, pid_st = pid_base(frame[0] * 10.0, frame[1] * 10.0, frame[2] * 1.5, frame[3] * 1.5)
        th = float(np.clip(pid_th + scale * float(residual[0]), -1.0, 1.0))
        st = float(np.clip(pid_st + scale * float(residual[1]) * self.policy_steer_sign,
                           -1.0, 1.0))
        self._prev = np.array([th, st])   # the COMBINED command (matches training)
        self._prev_heading = state.heading_deg
        return ManualSetpoint(thrust=th, steering=st)
