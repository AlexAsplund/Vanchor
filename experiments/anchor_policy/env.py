"""The anchor (station-keeping) task as a tiny RL environment over the REAL
Fossen physics + the REAL gust/weather disturbance pipeline.

The observation is everything a real boat actually has (position error to the
anchor, its own velocity, heading rate) expressed in the BODY frame so the
policy is heading-invariant and directly deployable from GPS + compass. The
action is a MotorCommand [thrust, steering].

Time is "sped up" purely by not pacing to wall-clock and (optionally) using a
slightly larger dt -- the integrator is ~30x inside its stability limit, so a
0.1 s training step gives the same physics as the 0.05 s runtime step (validate
with eval.py --dt 0.05).
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np

from vanchor.core.models import BoatState, Environment, GeoPoint, MotorCommand
from vanchor.sim.fossen import FossenBoat, FossenParams
from vanchor.sim.gust import GustModel
from vanchor.sim.weather import WeatherModel

ANCHOR = GeoPoint(59.3293, 18.0686)
_M_PER_DEG = 111320.0


class AnchorEnv:
    def __init__(self, dt: float = 0.1, duration_s: float = 120.0, radius_m: float = 5.0,
                 deadband_m: float = 2.0):
        self.dt = dt
        self.duration_s = duration_s
        self.radius_m = radius_m
        self.deadband_m = deadband_m

    # -- lifecycle -------------------------------------------------------- #
    def reset(self, scenario: dict) -> np.ndarray:
        s = scenario
        self.anchor = ANCHOR
        coslat = math.cos(math.radians(self.anchor.lat))
        dn = s["start_dist"] * math.cos(s["start_bearing"])
        de = s["start_dist"] * math.sin(s["start_bearing"])
        start = GeoPoint(
            self.anchor.lat + dn / _M_PER_DEG,
            self.anchor.lon + de / (_M_PER_DEG * coslat),
        )
        params = FossenParams(
            mass=s["mass"],
            hull_tracking=s["hull_tracking"],
            thruster_x_m=s["thruster_x_m"],
            max_thrust_n=s["max_thrust_n"],
        )
        self.boat = FossenBoat(BoatState(point=start, heading_deg=s["heading"]), params)
        self.boat._nu[:] = [s["u0"], s["v0"], 0.0]
        self.base_env = Environment(
            current_speed=s["current_speed"], current_dir=s["current_dir"],
            wind_speed=s["wind_speed"], wind_dir=s["wind_dir"],
            gust_amplitude_mps=s["gust"], gust_tau_s=s["gust_tau"],
            wind_variability=s["wind_var"], current_variability=s["cur_var"],
        )
        self.env = dataclasses.replace(self.base_env)
        self._base_wind = self.base_env.wind_speed
        self._base_wind_dir = self.base_env.wind_dir
        self._base_cur = self.base_env.current_speed
        self._gust = GustModel(s["gust"], s["gust_tau"], seed=s["seed"] & 0x7FFFFFFF)
        self._weather = WeatherModel(
            wind_variability=s["wind_var"], current_variability=s["cur_var"],
            seed=(s["seed"] >> 1) & 0x7FFFFFFF,
        )
        self._prev = np.zeros(2)
        self.t = 0.0
        return self._obs()

    # -- helpers ---------------------------------------------------------- #
    def _err_ned(self):
        b = self.boat.state.point
        coslat = math.cos(math.radians(self.anchor.lat))
        dn = (self.anchor.lat - b.lat) * _M_PER_DEG
        de = (self.anchor.lon - b.lon) * _M_PER_DEG * coslat
        return dn, de

    def _obs(self) -> np.ndarray:
        dn, de = self._err_ned()
        h = math.radians(self.boat.state.heading_deg)
        ch, sh = math.cos(h), math.sin(h)
        e_fwd = dn * ch + de * sh        # anchor position relative to the bow
        e_lat = -dn * sh + de * ch
        vn, ve = self.boat.state.ground_vn, self.boat.state.ground_ve
        vg_fwd = vn * ch + ve * sh       # boat velocity over ground, body frame
        vg_lat = -vn * sh + ve * ch
        r = math.radians(self.boat.yaw_rate_dps)
        dist = math.hypot(dn, de)
        return np.array([
            e_fwd / 10.0, e_lat / 10.0, vg_fwd / 1.5, vg_lat / 1.5,
            r / 0.5, self._prev[0], self._prev[1], dist / 10.0,
        ])

    # -- step ------------------------------------------------------------- #
    def step(self, action):
        th = float(np.clip(action[0], -1.0, 1.0))
        st = float(np.clip(action[1], -1.0, 1.0))
        env = self.env
        # Slow weather wander (exactly as simulator.step does it).
        if env.wind_variability > 0.0 or env.current_variability > 0.0:
            self._weather.step(self.dt)
            env.wind_speed = self._weather.wind_speed(self._base_wind)
            env.wind_dir = self._weather.wind_dir(self._base_wind_dir)
            env.current_speed = self._weather.current_speed(self._base_cur)
        # Gusts ride on top of the base wind for this step only.
        gust = self._gust.step(self.dt)
        step_env = env
        if gust:
            step_env = dataclasses.replace(env, wind_speed=max(0.0, env.wind_speed + gust))
        self.boat.step(self.dt, MotorCommand(thrust=th, steering=st), step_env)
        self._prev = np.array([th, st])
        self.t += self.dt
        dn, de = self._err_ned()
        dist = math.hypot(dn, de)
        r_rate = math.radians(self.boat.yaw_rate_dps)
        # Reward: stay in the watch circle with MINIMAL energy. A central deadband
        # removes any incentive to thrash the motor to sit dead on the anchor
        # (the cause of the brute-force, near-full-thrust local optimum). Past the
        # deadband a gentle pull; once outside the circle a firm pull. The energy
        # term then makes the policy idle and correct only as needed; a small
        # anti-spin term discourages burning thrust to pirouette in place.
        over = max(0.0, dist - self.deadband_m)
        reward = (
            -over
            - (0.8 if dist > self.radius_m else 0.0)
            - 0.20 * (th * th)
            - 0.02 * (r_rate * r_rate)
        )
        done = self.t >= self.duration_s
        return self._obs(), reward, done, {"dist": dist}
