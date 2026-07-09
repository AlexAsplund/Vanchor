"""The anchor (station-keeping) task over the REAL Fossen physics -- now driven
by the SAME perceived, noisy, low-rate sensor pipeline the deployed controller
sees (v3). This closes the sim-to-real gap that made the v1/v2 policy (trained on
clean 10 Hz ground truth) drive off in the field:

  * physics integrates finely (0.05 s) but the POLICY acts at the control rate
    (5 Hz, dt 0.2 s) -- the motor command is held between control ticks;
  * GPS is 1 Hz: position is NOISY (0.35 m) and STALE (held ~1 s between fixes);
    SOG/COG come from the (stale) ground velocity, and COG -> heading when slow
    (exactly as SimGps does);
  * the compass is 5 Hz with 1 deg noise; the yaw rate is ESTIMATED from
    perceived-heading differences (no rate sensor), as the deployed mode does;
  * a configurable action-rate penalty (CAPS) discourages the bang-bang thrust
    that the clean-feedback training produced.

history=1 still gives a single frame; history>1 stacks frames for memory.

v6: the env applies the runtime Helm's mount-polarity flip (``steer_sign``: +1
bow, -1 stern) to the physical steering command, so stern-mount scenarios train
in the same normalised "helm frame" the deployed hybrid executes in (the policy
and PID base always see +steering = starboard). Policies retrained on this env
therefore generalise across mounts; the model JSON records ``steer_sign`` so the
runtime knows the trained convention.
"""

from __future__ import annotations

import dataclasses
import math
from collections import deque

import numpy as np

from vanchor.controller.anchor_ml import pid_base
from vanchor.core.geo import normalize_deg, offset_meters
from vanchor.core.models import BoatState, Environment, GeoPoint, MotorCommand
from vanchor.sim.fossen import FossenBoat, FossenParams
from vanchor.sim.gust import GustModel
from vanchor.sim.weather import WeatherModel

ANCHOR = GeoPoint(59.3293, 18.0686)
_M_PER_DEG = 111320.0


class AnchorEnv:
    def __init__(self, dt: float = 0.2, duration_s: float = 120.0, radius_m: float = 5.0,
                 history: int = 1, arate: float = 0.0, physics_dt: float = 0.05,
                 gps_hz: float = 5.0, compass_hz: float = 5.0,
                 gps_noise_m: float = 0.35, heading_noise_deg: float = 1.0,
                 residual_scale: float = 0.3, anticip: float = 0.0,
                 pure: bool = False, steer_range_deg: float | None = None,
                 wind_cap: float | None = None, current_cap: float | None = None,
                 gust_cap: float | None = None,
                 steer_rate_dps: float | None = None,
                 pid_cal_deg: float | None = None):
        # EXPERIMENT: pure=True makes the net output the command DIRECTLY
        # (command = clip(net), no PID base) -- a from-scratch learned controller
        # rather than a PID refiner. steer_range_deg widens the boat's physical
        # steering swing (default 35 deg) so a pure policy can learn to VECTOR
        # thrust through the full rotation.
        self.pure = bool(pure)
        self.steer_range_deg = steer_range_deg
        # EXPERIMENT (actuator fidelity): the real steering head slews at
        # ~120 deg/s (BoatConfig.max_steer_rate_dps, 20 rpm gearmotor) but the env historically
        # applied commanded steering INSTANTLY. With steer_rate_dps set, the
        # physical deflection tracks the command at that rate, so thrust fires
        # at the head's ACTUAL angle mid-swing -- making thrust modulation
        # during rotation a learnable behaviour. None = legacy instant.
        self.steer_rate_dps = steer_rate_dps
        self._head_st = 0.0   # actual physical steering, normalised [-1, 1]
        # EXPERIMENT: at very wide steer ranges the PID base's +/-45deg-intent
        # steering becomes absurdly hot (x8 at 360). pid_cal_deg rescales the
        # BASE's steering so it keeps commanding the physical angles it was
        # designed for (e.g. 120), while the learned residual retains the full
        # +/-1 = +/-steer_range authority. None = legacy (no rescale).
        self._pid_scale = (pid_cal_deg / steer_range_deg
                           if pid_cal_deg and steer_range_deg else 1.0)
        # EXPERIMENT: cap the environmental severity to the HOLDABLE regime so the
        # policy trains on conditions a trolling motor can actually station-keep in
        # (the un-holdable 12 m/s tail otherwise dominates the average). Applied to
        # both the training and validation scenarios on reset.
        self.wind_cap = wind_cap
        self.current_cap = current_cap
        self.gust_cap = gust_cap
        # anticip: extra reward (penalty) for letting the boat drift OUTWARD from
        # the anchor -- rewards arresting drift *before* it becomes position error,
        # i.e. anticipatory / feed-forward control rather than chase-after-the-fact.
        self.anticip = float(anticip)
        # v5 RESIDUAL: the command is the robust PID base + a bounded learned
        # correction: clip(pid + residual_scale * policy(obs)). residual_scale=0
        # => pure PID; the policy starts near zero so it begins at PID quality and
        # only searches small improvements (no bang-bang local optimum).
        self.residual_scale = residual_scale
        self.dt = dt                       # CONTROL period (policy acts each step)
        self.duration_s = duration_s
        self.radius_m = radius_m
        self.history = max(1, int(history))
        self.arate = float(arate)
        self.physics_dt = physics_dt
        self.gps_period = 1.0 / gps_hz
        self.compass_period = 1.0 / compass_hz
        self.gps_noise_m = gps_noise_m
        self.heading_noise_deg = heading_noise_deg

    # -- lifecycle -------------------------------------------------------- #
    def reset(self, scenario: dict) -> np.ndarray:
        self._head_st = 0.0
        s = scenario
        self.anchor = ANCHOR
        coslat = math.cos(math.radians(self.anchor.lat))
        dn = s["start_dist"] * math.cos(s["start_bearing"])
        de = s["start_dist"] * math.sin(s["start_bearing"])
        start = GeoPoint(self.anchor.lat + dn / _M_PER_DEG,
                         self.anchor.lon + de / (_M_PER_DEG * coslat))
        _extra = {"max_steer_angle_deg": self.steer_range_deg} if self.steer_range_deg else {}
        params = FossenParams(mass=s["mass"], hull_tracking=s["hull_tracking"],
                              thruster_x_m=s["thruster_x_m"], max_thrust_n=s["max_thrust_n"],
                              **_extra)
        # Mirror the runtime Helm's mount-polarity normalisation (v6): at
        # deployment the Helm multiplies the WHOLE mode command by steer_sign
        # (+1 bow, -1 stern) before it reaches the motor, so the policy always
        # acts in the normalised "helm frame" (+steering = starboard) regardless
        # of the mount. Apply the identical flip here so stern-mount scenarios
        # train the SAME polarity the runtime will execute -- without this a
        # stern scenario inverts the PID base + residual steering relative to
        # the deployed pipeline (the v5 sign bug).
        self._steer_sign = 1.0 if s["thruster_x_m"] >= 0 else -1.0
        self.boat = FossenBoat(BoatState(point=start, heading_deg=s["heading"]), params)
        self.boat._nu[:] = [s["u0"], s["v0"], 0.0]
        _ws = min(s["wind_speed"], self.wind_cap) if self.wind_cap is not None else s["wind_speed"]
        _cs = min(s["current_speed"], self.current_cap) if self.current_cap is not None else s["current_speed"]
        _g = min(s["gust"], self.gust_cap) if self.gust_cap is not None else s["gust"]
        self.base_env = Environment(
            current_speed=_cs, current_dir=s["current_dir"],
            wind_speed=_ws, wind_dir=s["wind_dir"],
            gust_amplitude_mps=_g, gust_tau_s=s["gust_tau"],
            wind_variability=s["wind_var"], current_variability=s["cur_var"])
        self.env = dataclasses.replace(self.base_env)
        self._base_wind = self.base_env.wind_speed
        self._base_wind_dir = self.base_env.wind_dir
        self._base_cur = self.base_env.current_speed
        self._gust = GustModel(s["gust"], s["gust_tau"], seed=s["seed"] & 0x7FFFFFFF)
        self._weather = WeatherModel(wind_variability=s["wind_var"],
                                     current_variability=s["cur_var"],
                                     seed=(s["seed"] >> 1) & 0x7FFFFFFF)
        # Independent RNG for sensor noise (so it's decoupled from gust/weather).
        self._srng = np.random.default_rng((s["seed"] * 2654435761) & 0xFFFFFFFF)
        self._t = 0.0
        self._next_gps = 0.0
        self._next_compass = 0.0
        self._prev = np.zeros(2)
        # Perceived sensor state -- sampled once now (a real fix at t=0).
        self._p_heading = None
        self._sample_gps(); self._sample_compass()
        self._prev_p_heading = self._p_heading
        f = self._frame()
        self._cur_frame = f                # frame the policy acts on (for the PID base)
        self._hist = deque([f] * self.history, maxlen=self.history)
        return np.concatenate(self._hist)

    # -- perceived sensors (mirror SimGps / SimCompass) ------------------- #
    def _sample_gps(self) -> None:
        s = self.boat.state
        n = self.gps_noise_m
        self._p_pos = offset_meters(s.point, self._srng.normal(0.0, n), self._srng.normal(0.0, n))
        sog = math.hypot(s.ground_ve, s.ground_vn)
        self._p_sog = sog
        # COG is undefined when nearly stationary -> report heading (as SimGps).
        self._p_cog = (math.degrees(math.atan2(s.ground_ve, s.ground_vn)) % 360.0
                       if sog > 0.05 else s.heading_deg)

    def _sample_compass(self) -> None:
        self._p_heading = self.boat.state.heading_deg + self._srng.normal(0.0, self.heading_noise_deg)

    # -- observation (from PERCEIVED state, like the deployed mode) -------- #
    def _frame(self) -> np.ndarray:
        coslat = math.cos(math.radians(self.anchor.lat))
        dn = (self.anchor.lat - self._p_pos.lat) * _M_PER_DEG
        de = (self.anchor.lon - self._p_pos.lon) * _M_PER_DEG * coslat
        h = math.radians(self._p_heading)
        ch, sh = math.cos(h), math.sin(h)
        e_fwd = dn * ch + de * sh
        e_lat = -dn * sh + de * ch
        cog = math.radians(self._p_cog)
        vn, ve = self._p_sog * math.cos(cog), self._p_sog * math.sin(cog)
        vg_fwd = vn * ch + ve * sh
        vg_lat = -vn * sh + ve * ch
        r = math.radians(normalize_deg(self._p_heading - self._prev_p_heading)) / self.dt
        dist = math.hypot(dn, de)
        return np.array([e_fwd / 10.0, e_lat / 10.0, vg_fwd / 1.5, vg_lat / 1.5,
                         r / 0.5, self._prev[0], self._prev[1], dist / 10.0])

    # -- step (sub-steps physics; samples sensors at their rates) --------- #
    def step(self, residual):
        # v5: command = robust PID base (from the frame the policy acted on) + a
        # bounded learned residual.
        f = self._cur_frame
        if self.pure:
            # Pure learned controller: the net IS the command (no PID base).
            th = float(np.clip(residual[0], -1.0, 1.0))
            st = float(np.clip(residual[1], -1.0, 1.0))
        else:
            pid_th, pid_st = pid_base(f[0] * 10.0, f[1] * 10.0, f[2] * 1.5, f[3] * 1.5)
            pid_st *= self._pid_scale
            th = float(np.clip(pid_th + self.residual_scale * float(residual[0]), -1.0, 1.0))
            st = float(np.clip(pid_st + self.residual_scale * float(residual[1]), -1.0, 1.0))
        dth, dst = th - self._prev[0], st - self._prev[1]
        # The physical steering deflection carries the Helm's mount sign (see
        # reset); the OBSERVATION (_prev) keeps the helm-frame command, exactly
        # as the deployed mode sees its own pre-Helm setpoint.
        st_cmd = st * self._steer_sign
        n_sub = max(1, round(self.dt / self.physics_dt))
        pdt = self.dt / n_sub
        if self.steer_rate_dps is None:
            self._head_st = st_cmd                      # legacy: instant head
        cmd = MotorCommand(thrust=th, steering=self._head_st)
        self._prev_p_heading = self._p_heading
        max_ang = self.steer_range_deg or 35.0
        for _ in range(n_sub):
            if self.steer_rate_dps is not None:
                step_norm = (self.steer_rate_dps / max_ang) * pdt
                delta = st_cmd - self._head_st
                if abs(delta) <= step_norm:
                    self._head_st = st_cmd
                else:
                    self._head_st += math.copysign(step_norm, delta)
                cmd = MotorCommand(thrust=th, steering=self._head_st)
            env = self.env
            if env.wind_variability > 0.0 or env.current_variability > 0.0:
                self._weather.step(pdt)
                env.wind_speed = self._weather.wind_speed(self._base_wind)
                env.wind_dir = self._weather.wind_dir(self._base_wind_dir)
                env.current_speed = self._weather.current_speed(self._base_cur)
            gust = self._gust.step(pdt)
            step_env = (dataclasses.replace(env, wind_speed=max(0.0, env.wind_speed + gust))
                        if gust else env)
            self.boat.step(pdt, cmd, step_env)
            self._t += pdt
            if self._t >= self._next_compass:
                self._sample_compass(); self._next_compass += self.compass_period
            if self._t >= self._next_gps:
                self._sample_gps(); self._next_gps += self.gps_period

        self._prev = np.array([th, st])
        # Reward on GROUND TRUTH (the real objective), control on perceived obs.
        dn, de = (self.anchor.lat - self.boat.state.point.lat) * _M_PER_DEG, \
                 (self.anchor.lon - self.boat.state.point.lon) * _M_PER_DEG * math.cos(math.radians(self.anchor.lat))
        dist = math.hypot(dn, de)
        # Anticipation: penalize OUTWARD radial speed (the boat drifting away from
        # the mark) so the policy learns to arrest drift before it becomes error.
        s = self.boat.state
        out = max(0.0, -(s.ground_vn * dn + s.ground_ve * de) / dist) if dist > 0.1 else 0.0
        # Lighter shaping than the pure-ML versions: the PID base already provides
        # smoothness + anti-saturation, so the residual just needs to improve the hold.
        reward = (-dist
                  - (0.6 if dist > self.radius_m else 0.0)
                  - 0.05 * (th * th)
                  - self.arate * (dth * dth + dst * dst)
                  - self.anticip * out)
        done = self._t >= self.duration_s
        new_f = self._frame()
        self._hist.append(new_f)
        self._cur_frame = new_f
        return np.concatenate(self._hist), reward, done, {"dist": dist}
