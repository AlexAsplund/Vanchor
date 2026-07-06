# `vanchor.controller`

<a id="vanchor.controller"></a>

# vanchor.controller

Control layer: turn navigation state into motor commands.

Each control mode (manual, anchor-hold, heading-hold, waypoint, work-area,
along-contour, drift, orbit, trolling, …) implements ``activate(state)`` +
``update(state, dt) -> Setpoint``. The controller runs the active mode at a fixed
rate, converts its setpoint through the PID/steering logic into a
``MotorCommand`` under the safety limits, and drives the motor. Also home to the
autopilot calibration routines and the ML-based anchor hold.


<a id="vanchor.controller.anchor_ml"></a>

# vanchor.controller.anchor\_ml

Learned virtual-anchor mode (``anchor_ml``): a tiny neural-net station-keeper.

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

<a id="vanchor.controller.anchor_ml.pid_base"></a>

#### pid\_base

```python
def pid_base(e_fwd, e_lat, vg_fwd, vg_lat, kp=0.12, kd=0.6, deadband=0.8)
```

Robust anchor hold base law (the AnchorHoldMode behaviour), from body-frame
anchor error + ground velocity -> (thrust, steering). Idles inside a deadband;
otherwise drives toward the mark, BACKING straight up when the mark is astern
(instead of looping around, the naive-PID divergence). The shared base for the
training env AND the deployed hybrid mode, so they match exactly.

<a id="vanchor.controller.anchor_ml.AnchorMLMode"></a>

## AnchorMLMode Objects

```python
class AnchorMLMode()
```

Hybrid learned anchor hold: a robust PID base plus a small bounded learned
residual -- ``command = clip(pid_base + 0.3 * net(obs))``. The base (deadband
idle, drive-to-mark, reverse-when-astern) provides robustness and the
idle-at-rest guarantee; the tiny net (trained on the real deployment sensor
pipeline) adds a correction that tightens the hold. Bounded by construction,
so the worst case is just the PID. Produces a ManualSetpoint, holds
``state.anchor``. (Eval: residual lifts the PID base from ~66% to ~80% within
the watch circle on the deployment pipeline, while staying idle-at-rest safe.)


<a id="vanchor.controller.calibration"></a>

# vanchor.controller.calibration

Auto-calibration drive for the 'Init boat' wizard.

Runs a short, scripted sequence of maneuvers on the live runtime (sim or real
hardware), measures the boat's response, then runs the auto-tuner and applies the
result. Progress is exposed as a snapshot the UI streams via telemetry.

  straight  full ahead -> measure top speed + acceleration time constant
  coast     thrust off -> measure deceleration (drag) time constant
  turn      steer hard -> measure max turn rate + steering sign (bow vs stern)
  tuning    run the heading + anchor auto-tuners with the measured params
  done      apply measured params + tuned gains

It only issues ordinary manual commands, so it is hardware-agnostic. A safety
disclaimer is the UI's job (it drives the boat).


<a id="vanchor.controller.controller"></a>

# vanchor.controller.controller

The controller: the heart of the event-driven system.

Responsibilities:
  * Own the active control mode and the steering helm.
  * On every control tick, ask the mode for a setpoint, turn it into a concrete
    :class:`MotorCommand` via the helm, and hand it to the motor controller.
  * Translate inbound commands (from the UI/bus) into mode/state changes.

The control logic is exposed as a synchronous ``control_tick(dt)`` so it can be
driven deterministically by tests, and wrapped by an async ``run`` loop for the
live system.

<a id="vanchor.controller.controller.Helm"></a>

## Helm Objects

```python
class Helm()
```

Turns a heading intent into a steering command via one shared PID.

This is the "autopilot" inner loop: every guided mode benefits from a single
well-tuned heading controller instead of re-implementing steering.

<a id="vanchor.controller.controller.Controller"></a>

## Controller Objects

```python
class Controller()
```

<a id="vanchor.controller.controller.Controller.handle_command"></a>

#### handle\_command

```python
def handle_command(command: dict) -> None
```

Apply a command dict. Shape: ``{"type": ..., ...}``.

<a id="vanchor.controller.controller.Controller.maybe_record_launch"></a>

#### maybe\_record\_launch

```python
def maybe_record_launch() -> None
```

Auto-record the launch point on the first good fix (idempotent).


<a id="vanchor.controller.modes"></a>

# vanchor.controller.modes

Control modes -- the high-level steering behaviours.

Each mode is a pure strategy: given the current :class:`NavigationState` and a
timestep, produce a :class:`Setpoint`. A mode never touches hardware; it only
expresses *intent* (either drive the motor directly, or hold a target heading).
The controller's helm turns a heading intent into actual steering, so all the
guided modes share one well-tuned heading loop.

This separation makes every behaviour independently unit-testable with no
hardware and no event loop.

<a id="vanchor.controller.modes.ControlMode"></a>

## ControlMode Objects

```python
class ControlMode(abc.ABC)
```

<a id="vanchor.controller.modes.ControlMode.activate"></a>

#### activate

```python
def activate(state: NavigationState) -> None
```

Called when this mode becomes active. Reset internal loops here.

<a id="vanchor.controller.modes.HeadingHoldMode"></a>

## HeadingHoldMode Objects

```python
class HeadingHoldMode(ControlMode)
```

Hold the heading stored in ``state.target_heading`` while applying a
user-set cruise throttle.

<a id="vanchor.controller.modes.DriftMode"></a>

## DriftMode Objects

```python
class DriftMode(ControlMode)
```

Controlled drift: hold a heading while a bidirectional SOG PID holds a
*low* target speed-over-ground (``state.drift_target_knots``).

Wind/current carry the boat along the chosen bearing; the motor only trims
speed -- adding thrust if the drift is too slow, or reversing to brake if it
is too fast. Mirrors the Drift mode of high-end GPS trolling motors.

<a id="vanchor.controller.modes.AnchorConfig"></a>

## AnchorConfig Objects

```python
@dataclass
class AnchorConfig()
```

<a id="vanchor.controller.modes.AnchorConfig.kp"></a>

#### kp

thrust per metre of position error

<a id="vanchor.controller.modes.AnchorConfig.kd"></a>

#### kd

*braking* thrust per (m/s) of closing speed toward the anchor

<a id="vanchor.controller.modes.AnchorConfig.feedforward_gain"></a>

#### feedforward\_gain

fraction of the estimated drift to counter

<a id="vanchor.controller.modes.AnchorConfig.boat_max_speed_mps"></a>

#### boat\_max\_speed\_mps

to estimate our thrust's contribution to v

<a id="vanchor.controller.modes.AnchorConfig.drift_alpha"></a>

#### drift\_alpha

EMA gain/tick for the drift estimate (~10 s @ 5 Hz)

<a id="vanchor.controller.modes.AnchorConfig.drift_min_mps"></a>

#### drift\_min\_mps

below this, no significant drift to point into

<a id="vanchor.controller.modes.AnchorHoldMode"></a>

## AnchorHoldMode Objects

```python
class AnchorHoldMode(ControlMode)
```

Virtual anchor (position hold): hold position with reverse thrust + braking.

A PD controller on the (ground) distance to the mark: ``kp`` pulls toward the
anchor, ``kd`` brakes using the GPS closing speed so the boat doesn't
overshoot and orbit. It uses **reverse thrust** -- braking an overshoot, and
when the anchor ends up *behind* the boat it backs straight up toward it
rather than looping all the way around. Within ``idle_deadband_m`` it idles;
with no thrust the (thrust-to-steer) motor produces no yaw, so the heading is
held passively and the servo isn't worked.

Note: a single bow-mounted, thrust-to-steer motor is underactuated -- it
cannot actively hold an arbitrary heading while sitting still (steering needs
thrust, which moves it off station). So this holds *position* and lets the
heading settle, exactly like a real GPS trolling motor.

<a id="vanchor.controller.modes.maneuver_to_bearing"></a>

#### maneuver\_to\_bearing

```python
def maneuver_to_bearing(heading_deg: float,
                        bearing_deg: float,
                        distance_m: float,
                        *,
                        turn_rate_dps: float,
                        fwd_speed_mps: float,
                        reverse_efficiency: float,
                        currently_reverse: bool = False,
                        hysteresis: float = 0.85) -> tuple[float, float, bool]
```

Pick **forward** (bow toward ``bearing``) or **reverse** (stern toward
``bearing``) to reach a point ``distance_m`` away on ``bearing_deg``, by
whichever has the lower estimated *time to arrive*: ``turn_time + travel_time``.

Reversing trades a smaller heading change for slower travel (a trolling-motor
prop is weaker astern), so it wins when the target is **behind AND near** —
sometimes that means "turn a little and reverse" rather than swinging the
whole boat around; for a *far* target it is quicker to turn around and run
forward. ``hysteresis`` (<1) makes switching require the alternative be
clearly better, to stop chatter near the crossover.

Returns ``(target_heading_deg, thrust_sign, reverse)`` where ``thrust_sign``
is ``+1`` forward / ``-1`` reverse. (The helm flips steering authority under
negative thrust, so a reverse setpoint steers correctly.)

<a id="vanchor.controller.modes.WaypointMode"></a>

## WaypointMode Objects

```python
class WaypointMode(ControlMode)
```

Steer through ``state.waypoints`` in order, correcting for cross-track
error so the boat tracks each leg rather than just aiming at the mark.

<a id="vanchor.controller.modes.WorkAreaConfig"></a>

## WorkAreaConfig Objects

```python
@dataclass
class WorkAreaConfig()
```

Work Area mode: visit each spot, hold position there, then advance.

<a id="vanchor.controller.modes.WorkAreaConfig.arrival_radius_m"></a>

#### arrival\_radius\_m

within this of a spot -> begin the hold

<a id="vanchor.controller.modes.WorkAreaConfig.dwell_s"></a>

#### dwell\_s

auto-advance after this (when advance="timed")

<a id="vanchor.controller.modes.WorkAreaConfig.advance"></a>

#### advance

"manual" (on-screen button) | "timed" (dwell)

<a id="vanchor.controller.modes.WorkAreaConfig.orient_thrust"></a>

#### orient\_thrust

gentle thrust used to orient to a spot's

<a id="vanchor.controller.modes.WorkAreaMode"></a>

## WorkAreaMode Objects

```python
class WorkAreaMode(ControlMode)
```

Work an area spot by spot: travel to ``state.waypoints[active]``, HOLD
position there (active anchor hold) while the user works, then advance to the
next spot -- after ``dwell_s`` ("timed" advance) and/or when the user taps
"Go to next spot" (``state.work_next_requested``). ``route_loop`` cycles the
spots; ``route_patrol`` runs them there-and-back; otherwise the boat holds the
final spot once the route is done.

Travel reuses the waypoint leg logic (cross-track + forward/reverse helm); the
hold delegates to AnchorHoldMode. Dwell time is accumulated from
``dt`` so the deterministic harness drives it without a wall clock.

<a id="vanchor.controller.modes.FollowApbConfig"></a>

## FollowApbConfig Objects

```python
@dataclass
class FollowApbConfig()
```

<a id="vanchor.controller.modes.FollowApbConfig.xte_gain"></a>

#### xte\_gain

degrees of correction per metre of cross-track error

<a id="vanchor.controller.modes.FollowApbMode"></a>

## FollowApbMode Objects

```python
class FollowApbMode(ControlMode)
```

Steer from an externally supplied APB sentence (e.g. a phone nav app or
chartplotter acting as the route source). Uses the APB's bearing-to-
destination biased by its cross-track error and steer-to direction.

<a id="vanchor.controller.modes.ContourConfig"></a>

## ContourConfig Objects

```python
@dataclass
class ContourConfig()
```

<a id="vanchor.controller.modes.ContourConfig.throttle"></a>

#### throttle

default forward drive when no cruise (knots) hold

<a id="vanchor.controller.modes.ContourFollowMode"></a>

## ContourFollowMode Objects

```python
class ContourFollowMode(ControlMode)
```

Follow a depth contour (isobath).

Drives forward at the set speed while steering to keep ``state.depth_m`` at
``target_depth_m``. It estimates the bottom's slope *along the track* from
the depth TREND -- how much the depth changed over the last few metres of
travel -- and curves toward deeper or shallower water to null the depth
error, so the boat tracks the chosen isobath rather than just driving
straight. ``side`` ("deep"/"shallow") picks which way it turns to correct,
matching the bank the operator wants to favour. If no sounding is available
it simply holds heading.

<a id="vanchor.controller.modes.OrbitMode"></a>

## OrbitMode Objects

```python
class OrbitMode(ControlMode)
```

Orbit a centre point at a fixed radius (circle / racetrack hold).

Each tick it computes the bearing from the centre to the boat, advances that
bearing a little in the travel direction (cw/ccw) to get a point slightly
*ahead* on the ring, aims there, and biases the heading by a radial-error
correction so the boat both converges to the ring and holds it. Drives
forward at the set speed. ``range_m`` (distance to centre) is exposed for
telemetry.

<a id="vanchor.controller.modes.TrollingMode"></a>

## TrollingMode Objects

```python
class TrollingMode(ControlMode)
```

S-curve trolling weave.

Adds a sinusoidal heading offset ``amplitude_deg * sin(2*pi*t/period_s)``
around a base heading (the heading when engaged, unless one is given) while
driving forward at the set speed -- the lazy S-pattern used to cover water
and vary lure action when trolling. ``phase`` (radians) is exposed for
telemetry.


<a id="vanchor.controller.safety"></a>

# vanchor.controller.safety

Safety governor: the last line of defence before a command reaches the motor.

This is a pure, synchronous filter sitting between the helm (which produces an
*intended* :class:`MotorCommand`) and the motor controller (which actuates it).
It never decides *where* to go -- it only restrains *how* commands are applied,
so that a misbehaving mode, a flaky GPS, or a dragging anchor cannot drive the
boat dangerously.

The governor keeps a small amount of internal state between ticks (the last
applied thrust, a reverse cooldown timer, and the time since the last fresh
GPS fix). It is deliberately free of I/O and of the event bus so it can be
exhaustively unit-tested.

Behaviours, all applied within a single :meth:`SafetyGovernor.govern` call:

* **Thrust slew limiting** -- the magnitude of thrust change per tick is capped
  at ``max_thrust_slew_per_s * dt`` so the prop cannot slam between settings.
* **Reverse protection** -- a sign flip of thrust (ahead<->astern) is blocked
  until thrust has rested near zero for ``reverse_delay_s`` seconds, avoiding
  abrupt gear-style reversals.
* **Loss-of-fix failsafe** -- once the time since the last fresh fix exceeds
  ``fix_timeout_s`` thrust is forced to zero so the boat coasts rather than
  steaming blind.
* **Anchor drag alarm** -- in anchor-hold mode, drifting beyond
  ``drag_alarm_factor * anchor_radius_m`` from the anchor raises an alarm.
* **Steering slew limiting** -- the steering change per tick is capped at
  ``max_steer_slew_per_s * dt`` so the command stays within the steering head's
  real rotation speed (and isn't a gear-shredding high-frequency jitter).

<a id="vanchor.controller.safety.SafetyConfig"></a>

## SafetyConfig Objects

```python
@dataclass
class SafetyConfig()
```

Tunable limits for the :class:`SafetyGovernor`.

<a id="vanchor.controller.safety.SafetyStatus"></a>

## SafetyStatus Objects

```python
@dataclass
class SafetyStatus()
```

What the governor did on a single tick, for telemetry and alarms.

<a id="vanchor.controller.safety.SafetyGovernor"></a>

## SafetyGovernor Objects

```python
class SafetyGovernor()
```

Filters motor commands and raises alarms, holding state across ticks.

<a id="vanchor.controller.safety.SafetyGovernor.set_nogo_zones"></a>

#### set\_nogo\_zones

```python
def set_nogo_zones(zones: list[list[tuple[float, float]]]) -> None
```

Replace the no-go polygons. ``zones`` is a list of rings, each a list
of ``(lat, lon)`` vertices. Degenerate rings (<3 points) are skipped.

<a id="vanchor.controller.safety.SafetyGovernor.reset"></a>

#### reset

```python
def reset() -> None
```

Forget all internal state (e.g. on mode change or restart).

<a id="vanchor.controller.safety.SafetyGovernor.govern"></a>

#### govern

```python
def govern(command: MotorCommand, state: NavigationState, dt: float,
           fix_is_fresh: bool) -> tuple[MotorCommand, SafetyStatus]
```

Filter ``command`` and report what was done.

``dt`` is the elapsed time since the previous call in seconds.
``fix_is_fresh`` is True when a new GPS fix arrived since the last tick;
the governor accumulates the gap itself for the loss-of-fix failsafe.

