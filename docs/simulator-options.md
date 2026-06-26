# Simulator options: reuse vs. build

Investigation of whether to replace our hand-rolled boat simulator with existing
open-source software, while keeping realistic, easy integration into this
NMEA-based Python controller.

## Our integration surface (what any option must plug into)

Two clean seams already exist:

- **Physics seam** — `Boat.step(dt, command, env) -> BoatState` in
  `sim/boat.py`. Anything that turns a `(thrust, steering)` command into
  ground-truth `(lat/lon, heading, speed)` can replace it.
- **NMEA seam** — `SimGps`/`SimCompass` in `sim/devices.py` derive noisy NMEA
  from `get_truth()` and publish to `events.NMEA_IN`. An external simulator
  could instead feed real NMEA onto that bus.

This matters: heavyweight options replace the *whole* truth source (bridge NMEA
in), whereas a lightweight Python dynamics model just swaps `Boat`.

## Evaluated options

### Lightweight Python dynamics (swap `Boat`)
- **PythonVehicleSimulator** — Fossen models, **MIT**, numpy-only.
  <https://github.com/cybergalactic/PythonVehicleSimulator>. Provides published
  3-DOF maneuvering models including the **`otter` twin-prop USV** (a close
  analog to a trolling-motor boat) and `shipClarke83`/Nomoto. Dynamics are
  importable standalone (plotting is separate). **Best realism-for-weight.**
- **MSS** (Fossen, MATLAB) — the canonical reference, not directly importable;
  use as the spec. <https://github.com/cybergalactic/MSS>.

### NMEA generators / GPS simulators
- **pynmea2** (MIT) — parse + build NMEA. Useful to harden our encoder; does not
  simulate motion. <https://github.com/Knio/pynmea2>
- **gpsd/gpsfake** (BSD), **nmeasimulator** (Win), **marnav** (BSD, C++) — replay
  canned tracks or emit open-loop NMEA; none react to our motor commands, so
  none replaces the physics. Good only for static regression fixtures.

### Marine platforms with sim/NMEA output
- **Signal K server** + `signalk-to-nmea0183` / `udp-nmea` (Apache-2.0) — a data
  bus/multiplexer, **no boat physics**; would add Node for no dynamics gain.
- **OpenCPN** (GPL) — excellent *visualizer*; point it at a TCP NMEA feed we
  emit. Not a physics source.

### Physics-grade USV simulators (heavy)
- **VRX / VORC** (Apache-2.0) — Gazebo + ROS 2, GPU, multi-GB. Highest fidelity
  (waves/hydro) but contradicts "lightweight, local, no GPU". Reserve for future
  wave-disturbance or hardware-in-the-loop work.
  <https://github.com/osrf/vrx>
- **usv_sim_lsa** — Gazebo Classic + ROS 1 (ageing stack), heavy bridging.
- **MOOS-IvP `uSimMarine`** (**GPL**) — a genuinely good 3-DOF surface sim with
  wind/current drift and no GPU, but pulls in the whole MOOS pub/sub runtime and
  needs a MOOS-var→NMEA bridge. Conceptually closest; heavy to embed.
  <https://oceanai.mit.edu/ivpman>

## Recommendation

**Don't bridge to an external simulator, and don't keep a naive model. Take the
middle path:**

1. **Vendor the Fossen `otter`/3-DOF model from PythonVehicleSimulator (MIT,
   numpy-only) behind the existing `Boat` interface.** Map our normalized
   `(thrust, steering)` to the model's two propeller speeds (otter) or
   thrust+rudder; keep `SimGps`/`SimCompass`/geo/NMEA unchanged. Main effort is
   tuning the otter's parameters down to trolling-motor scale. Risk: low.
2. *(optional)* Tighten NMEA encoding with **pynmea2**.
3. *(optional)* Emit NMEA to a TCP port so **OpenCPN**/Signal K can visualise
   alongside the Leaflet UI.

This raises realism (coupled sway/yaw, momentum, proper turning dynamics) with
**one new dependency and no GPU/ROS**, while preserving the asyncio loop, geo
helpers, noisy-sensor layer, and web UI. Tracked as roadmap item #1.

> Conclusion for now: the in-repo simulator stays as the zero-dependency default
> (good enough to develop and verify the control loops, as the integration tests
> demonstrate); the Fossen model is the recommended realism upgrade when needed.
