# Simulation & physics developer guide

> Part of the `docs/llms/` developer guide. 🔁 **If you change the physics, a
> boat parameter, sensor noise, a preset, or an environment effect, update this
> file** (and `docs/nav-control-api.md` / `docs/simulator-options.md`).

The simulator lets the whole stack run + be tested with no hardware. It is the
project's main asset — *use it to reproduce and measure before changing control*.

## Two boat models

`sim/simulator.py` wraps a **boat model** + `Environment` + simulated devices +
`bathymetry`/`battery`. Choose the model with `model=`:

- `"simple"` (`sim/boat.py`) — kinematic, fast, good for logic tests. The
  harness default.
- `"fossen"` (`sim/fossen.py`) — a **3-DOF Fossen maneuvering model** (numpy):
  `M·ν̇ + C(ν)·ν + D·ν = τ`. Realistic surge/sway/yaw dynamics, sideslip, and
  turn behaviour. Use it for anything about *how the boat handles*.

## The Fossen model (`sim/fossen.py`)

`FossenParams` is the hull's hydrodynamic fingerprint. The physically meaningful
knobs:

- Geometry/power: `length`, `beam`, `mass`, `max_thrust_n`, `max_speed_mps`,
  `reverse_efficiency`, `thruster_x_m` (+bow/−stern), `thruster_y_m`
  (+starboard), `max_steer_angle_deg`.
- Added mass: `x_udot, y_vdot, n_rdot, y_rdot, n_vdot`.
- Linear damping: `y_v` (lateral resistance / leeway), **`n_r` (yaw damping =
  directional stability + turn rate)**, `y_r, n_v` (sway↔yaw coupling = crabbing).
- Quadratic damping: `x_uu, y_vv, n_rr`.
- Derived in `__post_init__`: `iz = m/12·(L²+B²)` (yaw inertia); `x_u` (surge
  drag, so full thrust balances drag at `max_speed_mps`).

Yaw moment includes the thruster offsets: `N = thruster_x_m·F_y − thruster_y_m·F_x`
— a stern mount yaws opposite (hence `steer_sign`), a lateral offset yaws under
straight thrust (cancelled by the thrust-yaw feed-forward).

### Hull character (`hull_tracking`)

A single knob spanning **jon boat (loose, snappy, lots of leeway) → keelboat
(tracks straight, sluggish)**. In `__post_init__`:

```
k = hull_tracking · clamp( (length/beam) / (4.1/1.7), 0.7, 1.6 )   # slenderness
n_r, n_rr, y_v, y_vv  *=  k
```

At `hull_tracking=1.0` and the default L/B, `k==1.0` → **byte-identical to
before** (the no-op-default rule). It *also* biases helm authority + smoothing in
`_apply_boat_specs` (see [backend.md](backend.md)), so it matters on real
hardware too — as a prior the calibration drive refines.

## Simulated sensors (`sim/devices.py`)

`SimGps`, `SimCompass`, `SimDepthSounder` build NMEA from ground truth + add
noise. Config defaults live in `SensorConfig` (`core/config.py`).

**Sensor loop robustness.** Each sensor's `_loop` is wrapped in a top-level
`try/except` (with `logger.exception` + `continue`) so a publish error doesn't
kill the sim loop. The loops use **monotonic cadence** — the period accounts for
any drift in the previous iteration so sensor timing stays steady across the
session.

## `SimMotorController` — opt-in actuation shaping (`sim/devices.py`)

`SimMotorController` models a real propulsion system's physical imperfections,
defaulting to zero (= OFF, same behaviour as before). Set any parameter non-zero
to enable:

| Parameter | Effect |
|-----------|--------|
| `reverse_delay_s` | When commanded thrust crosses zero, holds at zero for this many seconds before applying the opposite direction (propeller reverse lag). |
| `thrust_slew_per_s` | Maximum rate of thrust change per second (soft-start / ramp limiting). |
| `thrust_lag_tau_s` | First-order exponential lag from the slewed target toward the actual applied thrust (prop inertia). |

**`step(dt)`** must be called each physics step (the `Simulator` does this
automatically). The shaping chain is: commanded → reverse interlock → slew →
lag → applied. All three parameters are independent — you can mix any subset.
These are intended for future sim/real parity work; they are not yet wired to
the config YAML / device-config API.

> ### ⚠️ The GPS-noise lesson (read this)
> A real marine GPS/chart-plotter **smooths (Kalman/SBAS) the fix before emitting
> NMEA**, so the track is *steady* frame-to-frame (~0.2–0.4 m), not the ~1.5 m
> raw-receiver scatter. `gps_noise_m` defaults to **0.35** for this reason.
> Symptom of getting it wrong: the autopilot weaves down a waypoint leg in
> otherwise calm water (it chases phantom cross-track error). The fix is the
> *sim realism*, **not** adding controller-side position filtering (which adds
> lag and duplicates what the plotter already does). When you see "it
> oscillates", isolate noise vs. control law first (run the harness with
> `gps.position_noise_m = 0`).

## Boat presets (`core/boat_profiles.py`)

Starter presets seed on first run with distinct `hull_tracking`/thrust/speed so
they handle differently: jon boat (`hull_tracking≈0.35`), bow/stern trolling
(≈1.0), off-centre bow trolling (`thruster_y_m≈0.35`), 15 HP outboard
(`hull_tracking≈1.6`, ~700 N, ~7 m/s).

### Recipe: add a preset
Add an entry to the preset table in `boat_profiles.py` (the seed runs only when
`boats.json` is absent — never clobbers user profiles). Pick `BoatConfig` fields
so it *feels* distinct. Update the preset test + this doc.

## Environment, teleport, misc

- `weather.py` / `gust.py` → `Environment` (wind/current speed+dir, gusts,
  variability). Set via `weather_preset` / `set_environment` commands.
- `bathymetry.py` — depth field the depth sounder reads.
- `battery.py` — battery drain model for the battery monitor / RTL.
- **Teleport** (sim only): `Simulator.teleport(lat, lon[, heading])` snaps ground
  truth + zeros velocity; reached via the `teleport` command.

### Recipe: change/extend the physics
1. Edit `FossenParams` / the model equations (or `boat.py` for the simple model).
2. Wire any new `BoatConfig` field through `_build_boat_params` (`app.py`).
3. **Reproduce + measure** with the harness (`model="fossen"`): assert the
   effect (turn rate, tracking, top speed) *and* the no-op default.
4. Update `tests/test_fossen.py` (or a new test) + this doc +
   `docs/nav-control-api.md`.
