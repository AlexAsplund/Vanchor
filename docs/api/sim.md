# `vanchor.sim`

<a id="vanchor.sim"></a>

# vanchor.sim


<a id="vanchor.sim.bathymetry"></a>

# vanchor.sim.bathymetry

A simple synthetic lake bottom for the simulator.

There is no real depth sensor in simulation, so we model a smooth, deterministic
bathymetry as a function of position. It is varied enough to make the depth HUD
and the auto depth-map overlay interesting without pretending to be a real chart.

<a id="vanchor.sim.bathymetry.Bathymetry"></a>

## Bathymetry Objects

```python
@dataclass
class Bathymetry()
```

Depth (m) as a smooth function of position around a reference point.


<a id="vanchor.sim.battery"></a>

# vanchor.sim.battery

A simple simulated battery model (`60`).

Trolling-motor autopilots run off a deep-cycle battery; how much charge is left
(and how far the boat can still get on it) is a first-class safety concern. This
model is intentionally lightweight -- it is *not* an electrochemical model -- but
it is believable enough to drive the UI gauges and the Return-to-Launch
auto-recommend logic (`61`):

  * It draws a current that is the sum of a constant *idle* (hotel/standby) load
    and a *propulsion* load proportional to ``|thrust|``. The propulsion draw is
    modelled at full thrust as ``load_a`` amps, scaled linearly with the thrust
    magnitude (a coarse but monotonic stand-in for the real cubic-ish curve).
  * It integrates the drawn charge out of the pack over time, lowering the
    state-of-charge (SOC).
  * It estimates time-to-empty and remaining range from a *recent average* draw
    and the boat's speed-over-ground, so the figures track how the boat is
    actually being used rather than an instantaneous spike.

On real hardware the SOC / voltage / current come from a battery monitor (a
shunt + gauge) over the HAL instead of being integrated here; the telemetry
shape and the range/time estimates stay identical so the UI and the RTL logic
do not care which source is wired in. See ``BatteryConfig``.

<a id="vanchor.sim.battery.BatteryConfig"></a>

## BatteryConfig Objects

```python
@dataclass
class BatteryConfig()
```

Battery pack sizing + the load model.

``reserve_pct`` is the usable-charge reserve: range / time-to-empty are
reported down to this reserve (not to a flat-dead pack), matching how a
skipper plans "I must turn back while I still have my reserve".

<a id="vanchor.sim.battery.BatteryConfig.capacity_ah"></a>

#### capacity\_ah

pack capacity (amp-hours)

<a id="vanchor.sim.battery.BatteryConfig.nominal_v"></a>

#### nominal\_v

nominal terminal voltage

<a id="vanchor.sim.battery.BatteryConfig.reserve_pct"></a>

#### reserve\_pct

usable-charge reserve (%) kept in hand

<a id="vanchor.sim.battery.BatteryConfig.idle_a"></a>

#### idle\_a

constant hotel/standby draw (A)

<a id="vanchor.sim.battery.BatteryConfig.load_a"></a>

#### load\_a

propulsion draw at full |thrust| (A)

<a id="vanchor.sim.battery.Battery"></a>

## Battery Objects

```python
class Battery()
```

Integrates state-of-charge from the applied thrust and reports estimates.

Pure and synchronous (no I/O, no clock) so it can be stepped deterministically
from the simulator and unit-tested. ``step(dt, thrust, sog_mps)`` advances it.

<a id="vanchor.sim.battery.Battery.set_soc"></a>

#### set\_soc

```python
def set_soc(soc_pct: float) -> None
```

Set/reset the state-of-charge (e.g. swapping a fresh battery in).

<a id="vanchor.sim.battery.Battery.step"></a>

#### step

```python
def step(dt: float, thrust: float, sog_mps: float) -> None
```

Advance the SOC by one step under the given thrust + speed.

``thrust`` is the normalized applied thrust (-1..1); ``sog_mps`` is the
boat's speed over ground in m/s (used only for the range estimate).

<a id="vanchor.sim.battery.Battery.voltage_v"></a>

#### voltage\_v

```python
@property
def voltage_v() -> float
```

A crude terminal voltage that sags as the pack drains.

Lead-acid resting voltage runs roughly from ~12.7 V full to ~11.8 V
empty; we linearly interpolate over that span around ``nominal_v`` so the
UI voltage gauge moves. (Real hardware reports a measured voltage.)

<a id="vanchor.sim.battery.Battery.draw_w"></a>

#### draw\_w

```python
@property
def draw_w() -> float
```

Instantaneous power draw in watts.

<a id="vanchor.sim.battery.Battery.time_to_empty_s"></a>

#### time\_to\_empty\_s

```python
@property
def time_to_empty_s() -> float
```

Estimated seconds until the usable reserve is hit, at the recent draw.

Returns ``inf`` when there is effectively no draw (nothing to estimate
against).

<a id="vanchor.sim.battery.Battery.range_m"></a>

#### range\_m

```python
@property
def range_m() -> float
```

Estimated metres of travel left at the recent average draw + speed.

Returns 0 when the boat isn't making way (no usable distance estimate).


<a id="vanchor.sim.boat"></a>

# vanchor.sim.boat

A simple but believable boat motion model.

The model is intentionally minimal -- enough to exercise the control loops
realistically without pretending to be a hydrodynamics simulator:

  * Forward speed follows the commanded thrust through a first-order lag
    (the boat takes a moment to spin up / coast down).
  * Steering directly produces a yaw rate (a trolling motor turns the hull by
    rotating its thrust vector).
  * Wind and current add a drift velocity that displaces the boat without
    changing its heading -- this is what anchor-hold and track-keeping fight.

<a id="vanchor.sim.boat.BoatParams"></a>

## BoatParams Objects

```python
@dataclass
class BoatParams()
```

<a id="vanchor.sim.boat.BoatParams.max_speed_mps"></a>

#### max\_speed\_mps

~3 knots, typical small trolling motor

<a id="vanchor.sim.boat.BoatParams.accel_tau_s"></a>

#### accel\_tau\_s

time constant of the speed response

<a id="vanchor.sim.boat.BoatParams.max_turn_rate_deg"></a>

#### max\_turn\_rate\_deg

yaw rate at full steering (with full thrust)

<a id="vanchor.sim.boat.BoatParams.reverse_efficiency"></a>

#### reverse\_efficiency

reverse thrust as a fraction of forward

<a id="vanchor.sim.boat.Boat"></a>

## Boat Objects

```python
class Boat()
```

<a id="vanchor.sim.boat.Boat.teleport"></a>

#### teleport

```python
def teleport(point: GeoPoint, heading: float | None = None) -> None
```

Instantly move ground truth to ``point`` (optionally set heading) and
zero all motion so the boat doesn't keep coasting from its old velocity.

<a id="vanchor.sim.boat.Boat.truth"></a>

#### truth

```python
def truth() -> BoatState
```

An immutable-ish snapshot of the current ground truth.


<a id="vanchor.sim.devices"></a>

# vanchor.sim.devices

Simulated devices that implement the real hardware interfaces.

Because these subclass the same ABCs as future serial devices, the controller,
navigator and event wiring cannot tell the difference between simulated and
real hardware. The simulated GPS/compass derive noisy NMEA from the boat's
ground-truth state; the simulated motor records the latest command so the boat
physics can read it.

<a id="vanchor.sim.devices.SimMotorController"></a>

## SimMotorController Objects

```python
class SimMotorController(MotorController)
```

Records the most recent command; the boat physics reads ``command``.

<a id="vanchor.sim.devices.SimServo"></a>

## SimServo Objects

```python
class SimServo(Actuator)
```

A trivial simulated servo/stepper, demonstrating the generic actuator
interface. Not required for the control loop, but shows how a steering
actuator would be modelled and tested.

<a id="vanchor.sim.devices.SimGps"></a>

## SimGps Objects

```python
class SimGps(Sensor)
```

<a id="vanchor.sim.devices.SimGps.sample"></a>

#### sample

```python
def sample(truth: BoatState | None = None) -> str
```

Build one noisy RMC sentence from ground truth (pure, for tests).

Course/speed-over-ground are derived from the *ground* velocity (hull
motion plus drift), exactly as a real GPS reports them -- so the
controller can observe the wind/current drift in COG/SOG.

<a id="vanchor.sim.devices.SimDepthSounder"></a>

## SimDepthSounder Objects

```python
class SimDepthSounder(Sensor)
```

Simulated depth sounder: samples the synthetic bathymetry under the boat
and emits DPT NMEA, exactly like a real transducer.


<a id="vanchor.sim.fossen"></a>

# vanchor.sim.fossen

A higher-fidelity 3-DOF (surge-sway-yaw) maneuvering boat model.

This is a drop-in alternative to :class:`vanchor.sim.boat.Boat`. It exposes the
same interface (``state``, ``step``, ``truth``) but instead of a first-order
speed lag plus a kinematic yaw rate it integrates a proper rigid-body +
hydrodynamic model in body-fixed coordinates ``[u, v, r]`` (surge, sway, yaw
rate).

The structure is inspired by the MIT-licensed Fossen "otter" USV from
``cybergalactic/PythonVehicleSimulator``:

    M * nu_dot + C(nu_r) * nu_r + (D_lin + D_quad(nu_r)) * nu_r = tau

where ``nu = [u, v, r]`` are body velocities, ``M = M_rb + M_a`` is the rigid-body
plus added mass, ``C(nu)`` is the Coriolis-centripetal matrix (the body-frame /
rotating-hull coupling + added-mass Munk moment -- NOT the negligible planetary
Earth-rotation Coriolis effect), ``D_lin`` / ``D_quad`` are linear and quadratic
damping, and ``tau`` is the generalized
force/moment from the thruster plus the aerodynamic wind force. The hydrodynamic
terms act on the velocity **through the water** ``nu_r = nu - nu_c`` (so a current
advects the hull and is felt as drag), while the kinematics integrate the
absolute velocity ``nu``. Wind enters ``tau`` as a quadratic aerodynamic force /
yaw moment (not a fixed leeway), so leeway and weathervaning emerge from the
force balance.

Thrust mapping: this models a **single steerable trolling motor mounted at the
bow**, at a signed longitudinal offset ``thruster_x_m`` from the centre of
gravity (positive = forward / bow). The motor produces a thrust of magnitude
``T = command.thrust * max_thrust_n`` (negative = reverse) directed along its
steered axis ``delta = command.steering * radians(max_steer_angle_deg)``:

    Fx = T * cos(delta)        # surge
    Fy = T * sin(delta)        # sway (+ = starboard)
    N  = thruster_x_m * Fy - thruster_y_m * Fx   # yaw from both lever arms

    tau = [Fx, Fy, N]

The key consequence of this (vectored-thrust / outboard) model is that **steering
authority scales with thrust**: with no thrust the motor produces no force and so
no yaw moment, hence essentially no turning -- a trolling motor cannot steer
without running. With a bow mount (``thruster_x_m > 0``) positive steering turns
the boat to starboard (heading increases). Cross-coupling between sway and yaw in
the damping / added-mass matrices makes the boat visibly "crab" (sway) during a
turn.

Integration is semi-implicit Euler: solve for ``nu_dot``, advance ``nu``, then
update heading by ``r*dt`` and the NED position from the absolute body velocity
rotated into the local tangent plane (current/wind already live inside ``nu``).

<a id="vanchor.sim.fossen.RHO_AIR"></a>

#### RHO\_AIR

kg/m^3

<a id="vanchor.sim.fossen.FossenParams"></a>

## FossenParams Objects

```python
@dataclass
class FossenParams()
```

Physical constants for the 3-DOF model, tuned to a 4.1 m boat driven by a
single steerable trolling motor mounted at the bow (~1.6 m/s top speed,
~12-25 deg/s full-thrust/full-steer turn rate).

Masses are in kg, lengths in m, the yaw inertia in kg*m^2. Damping
coefficients are in SI units consistent with forces in N and moments in
N*m acting on velocities in m/s and rad/s.

The surge linear-damping coefficient is *derived* from ``max_thrust_n`` and
``max_speed_mps`` so that full thrust converges to ~``max_speed_mps``; the
yaw inertia is derived from the hull geometry. See :meth:`__post_init__`.

<a id="vanchor.sim.fossen.FossenParams.length"></a>

#### length

overall length (m)

<a id="vanchor.sim.fossen.FossenParams.beam"></a>

#### beam

beam (m)

<a id="vanchor.sim.fossen.FossenParams.mass"></a>

#### mass

kg, hull + motor + battery + 1 person

<a id="vanchor.sim.fossen.FossenParams.max_thrust_n"></a>

#### max\_thrust\_n

~55 lbf trolling motor at full thrust (N)

<a id="vanchor.sim.fossen.FossenParams.reverse_efficiency"></a>

#### reverse\_efficiency

reverse thrust as a fraction of forward

<a id="vanchor.sim.fossen.FossenParams.max_speed_mps"></a>

#### max\_speed\_mps

target top speed at full thrust (m/s)

<a id="vanchor.sim.fossen.FossenParams.thruster_x_m"></a>

#### thruster\_x\_m

CG -> thruster longitudinal distance, + = bow

<a id="vanchor.sim.fossen.FossenParams.thruster_y_m"></a>

#### thruster\_y\_m

CG -> thruster lateral distance, + = starboard

<a id="vanchor.sim.fossen.FossenParams.max_steer_angle_deg"></a>

#### max\_steer\_angle\_deg

max motor steer deflection (deg)

<a id="vanchor.sim.fossen.FossenParams.x_udot"></a>

#### x\_udot

added mass in surge

<a id="vanchor.sim.fossen.FossenParams.y_vdot"></a>

#### y\_vdot

added mass in sway

<a id="vanchor.sim.fossen.FossenParams.n_rdot"></a>

#### n\_rdot

added inertia in yaw

<a id="vanchor.sim.fossen.FossenParams.y_rdot"></a>

#### y\_rdot

sway/yaw added-mass coupling

<a id="vanchor.sim.fossen.FossenParams.n_vdot"></a>

#### n\_vdot

yaw/sway added-mass coupling

<a id="vanchor.sim.fossen.FossenParams.y_v"></a>

#### y\_v

sway drag (large: hull resists sideways motion)

<a id="vanchor.sim.fossen.FossenParams.n_r"></a>

#### n\_r

yaw drag: sets the sustained turn rate

<a id="vanchor.sim.fossen.FossenParams.y_r"></a>

#### y\_r

sway/yaw damping coupling

<a id="vanchor.sim.fossen.FossenParams.n_v"></a>

#### n\_v

yaw/sway damping coupling

<a id="vanchor.sim.fossen.FossenBoat"></a>

## FossenBoat Objects

```python
class FossenBoat()
```

A 3-DOF surge-sway-yaw boat. Drop-in for :class:`~vanchor.sim.boat.Boat`.

<a id="vanchor.sim.fossen.FossenBoat.teleport"></a>

#### teleport

```python
def teleport(point: GeoPoint, heading: float | None = None) -> None
```

Instantly move ground truth to ``point`` (optionally set heading) and
zero the body-frame velocities (surge/sway/yaw) so the boat stops dead
instead of coasting from its pre-teleport momentum.

<a id="vanchor.sim.fossen.FossenBoat.truth"></a>

#### truth

```python
def truth() -> BoatState
```

An immutable-ish snapshot of the current ground truth.


<a id="vanchor.sim.gust"></a>

# vanchor.sim.gust

Wind gusts: a smooth, time-varying perturbation on top of the base wind.

Real wind is not steady -- it gusts and lulls. We model that as an
Ornstein-Uhlenbeck process (a smoothed random walk that decays back toward
zero), which gives realistic ramping gusts rather than white noise. The result
is added to the base wind speed each physics step, so the controller has to cope
with a wind that surges and eases -- a good stress test for station-keeping.

<a id="vanchor.sim.gust.GustModel"></a>

## GustModel Objects

```python
@dataclass
class GustModel()
```

<a id="vanchor.sim.gust.GustModel.amplitude_mps"></a>

#### amplitude\_mps

~std of the gust component; 0 disables gusts

<a id="vanchor.sim.gust.GustModel.tau_s"></a>

#### tau\_s

correlation time -- how slowly gusts build and fade

<a id="vanchor.sim.gust.GustModel.step"></a>

#### step

```python
def step(dt: float) -> float
```

Advance the gust process by ``dt`` and return the current offset (m/s).


<a id="vanchor.sim.simulator"></a>

# vanchor.sim.simulator

The simulator ties the boat, the environment and the simulated devices
together and runs the physics forward in time.

It owns ground truth. The simulated GPS/compass read that truth and publish
noisy NMEA; the simulated motor controller is read each physics tick to drive
the boat. The result is a closed loop:

    motor command -> boat physics -> GPS/compass NMEA -> navigator -> state
        -> control mode -> helm -> motor command -> ...

``step(dt)`` advances physics once (used by deterministic tests); ``run``
drives it in real time for the live UI.

<a id="vanchor.sim.simulator.Simulator"></a>

## Simulator Objects

```python
class Simulator()
```

<a id="vanchor.sim.simulator.Simulator.set_weather_base"></a>

#### set\_weather\_base

```python
def set_weather_base() -> None
```

Re-capture the current environment values as the steady base.

Call after externally setting ``environment`` (e.g. a ``set_environment``
command or a preset) so the slow wander wanders around the new values
rather than treating an already-evolved value as the base.

<a id="vanchor.sim.simulator.Simulator.step"></a>

#### step

```python
def step(dt: float) -> None
```

Advance the boat one physics step using the latest motor command.

Gusts are layered on top of the (user-set) base wind for this step only,
so ``self.environment.wind_speed`` remains the steady base value.

<a id="vanchor.sim.simulator.Simulator.teleport"></a>

#### teleport

```python
def teleport(lat: float, lon: float, heading: float | None = None) -> None
```

Snap the simulated boat's ground truth to ``(lat, lon)`` and stop it.

Optionally sets the heading. The boat's surge/sway/yaw velocities are
zeroed so it doesn't keep coasting from its pre-teleport momentum.


<a id="vanchor.sim.weather"></a>

# vanchor.sim.weather

Realistic, variable, tunable weather (task `44`).

Today the simulator has a steady base wind/current plus a fast Ornstein-
Uhlenbeck *gust* (:mod:`.gust`). Real weather also drifts slowly over a session:
the wind speed eases and freshens, the direction backs and veers, and on rivers
the current is strong and steady while lakes have almost none.

:class:`WeatherModel` adds that slow wander. It evolves three quantities with a
**much slower** OU process than gusts (minutes, not seconds):

- wind speed (m/s)
- wind direction (deg), as a wandering offset added to the base direction
- current speed (m/s)

A ``wind_variability`` / ``current_variability`` amount in ``[0, 1]`` scales how
far each wanders; ``0`` means perfectly steady (the value never changes). Gusts
still ride on top of the evolving base wind, applied in the simulator.

Presets (:data:`WEATHER_PRESETS`) bundle sensible base values + variability for
common water bodies (calm / lake / river / coastal) and can be applied live.

<a id="vanchor.sim.weather.MAX_WIND_SPEED_SWING_MPS"></a>

#### MAX\_WIND\_SPEED\_SWING\_MPS

+/- a few m/s of slow freshening/easing

<a id="vanchor.sim.weather.MAX_WIND_DIR_SWING_DEG"></a>

#### MAX\_WIND\_DIR\_SWING\_DEG

+/- backing/veering of the wind

<a id="vanchor.sim.weather.MAX_CURRENT_SWING_MPS"></a>

#### MAX\_CURRENT\_SWING\_MPS

+/- slow current variation

<a id="vanchor.sim.weather.WeatherModel"></a>

## WeatherModel Objects

```python
@dataclass
class WeatherModel()
```

Slow, bounded wander of wind speed/direction and current.

The model holds *offsets* from the steady base values; call :meth:`apply`
each tick with the live base values to get the evolved values to use.

<a id="vanchor.sim.weather.WeatherModel.wind_variability"></a>

#### wind\_variability

0 = steady, 1 = full slow wander

<a id="vanchor.sim.weather.WeatherModel.step"></a>

#### step

```python
def step(dt: float) -> None
```

Advance the slow wander by ``dt`` seconds.

