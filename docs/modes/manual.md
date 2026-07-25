# Manual

> Direct, hands-on control of the trolling motor — you drive the thrust and steering yourself, no autopilot loop.

**What it does** — Manual mode passes your two inputs straight to the motor:
a **thrust** value (−1 … +1, reverse to full forward) and a **steering** value
(−1 … +1, hard port to hard starboard). There is no heading hold, no GPS
station-keeping, and no course correction — the boat does exactly what the
sliders say and nothing else. (Internally it is a `ManualSetpoint` that the helm
forwards verbatim, only applying the boat's steering sign and a small thrust-yaw
trim so a hands-off, steering-0 command tracks straight on an off-centre motor.)
Manual is also the mode the boat falls back to whenever you tap **Stop**.

**When to use it** — Casting around by hand, docking and close-quarters
maneuvering, nudging the boat off a snag, or any time you simply want the motor
to do precisely what you tell it. It's also the safe "everything off" resting
state between guided runs.

**How to use it** — Tap **🕹 Manual** on the mode rail. You drive with the
on-screen **steering wheel** (drag around the dial for direction, outward for
power — one thumb sets both) or the thrust slider. The two inputs:

- **Thrust** (−1 … +1, default 0) — reverse through forward.
- **Steering** (−1 … +1, default 0) — port through starboard.

Drag to drive. On the **steering wheel**, thrust **snaps back to 0 the moment you
release** (a deadman) so the boat can't run away on a dropped finger. To keep a
speed hands-free there are two ways:

- **Hold-to-lock (cruise):** hold the knob roughly still for ~1 second at a
  non-zero speed. A ring fills around the rim, then locks solid amber and the hub
  reads **CRUISE n%** — releasing now keeps that speed. **Grab the knob again** to
  pick the throttle back up (the lock clears on touch); release then snaps back to
  0, or hold still to re-lock. Dragging back to the centre also stops.
- **HOLD toggle:** flip **HOLD** on to keep the last thrust on every release
  (trolling), off to snap to 0.

To stop at any time, drag thrust to 0 or tap the red **■ Stop** button — which
snaps thrust and steering to 0 and stays in Manual.

**Settings** — None beyond the two live sliders. Manual ignores Cruise Control
and the % throttle override (those only act on guided modes), so the thrust
slider is the whole story.

**Tips & gotchas**

- The sliders hold their last value — if you walk away with thrust set, the boat
  keeps driving. Zero the thrust or hit **Stop** when you're done.
- Steering only bites when the prop is turning: a trolling motor steers by
  vectoring thrust, so at zero (or near-zero) thrust, turning the wheel does
  little. Add some thrust to turn.
- Want the boat to *hold* a course instead of you fighting wind on the steering
  slider? Use [Heading hold](heading-hold.md). Want it to *stay put*? Use
  [Anchor hold](anchor-hold.md).

**Safety** — Manual is the baseline mode and the destination of every **Stop**.
The red **■ Stop** button always overrides whatever you're doing and drops you
here with the motor commanded to zero. The safety governor still applies its
slew limits, shallow-water / no-go auto-stop, and (if enabled) loss-of-fix
failsafe even in Manual, so a command is always rate-limited rather than slammed
straight to the motor.
