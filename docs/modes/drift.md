# Drift

> A controlled drift: let wind and current carry you along a chosen heading while the motor trims your speed to a gentle target.

**What it does** — Drift mode holds a **target heading** while a bidirectional
speed PID holds a *low* target **speed-over-ground**. Wind and current do the
work of carrying the boat along the bearing; the motor only trims speed — adding
forward thrust if the drift is too slow, or *reversing to brake* if it's too
fast. It mirrors the Drift mode of high-end GPS trolling motors: the heading is
held by the shared autopilot helm, and the SOG PID keeps the boat moving at the
speed you asked for, no faster. If the natural drift is already faster than your
target, the motor backs off / brakes to slow it down.

**When to use it** — Drift-fishing a flat or a wind-line at a controlled pace,
presenting bait at a slow steady speed over structure, or covering water downwind
without the boat running away with the breeze. It keeps a fast drift fishable and
a slow drift moving.

**How to use it** — Tap **🌀 Drift** on the mode rail. The panel has:

- **Drift speed** slider (0–2.0 kn, default 0.5) — the target speed over ground.
- **Target heading** slider (0–359°, default 0) — the bearing to hold.
- **🌀 Start drift** button — engages with the slider values.

Set the speed and heading, then tap **Start drift**. Dragging the **Drift speed**
slider while already drifting updates the target speed live. Tap **Stop** to end.

**Settings**

- **Drift speed** (default 0.5 kn) — the target speed-over-ground the PID holds.
  Set it at or below the natural drift to keep things slow; the motor brakes in
  reverse if the real drift exceeds it.
- **Target heading** (default 0°, or current heading if engaged without one) — the
  bearing the helm holds while drifting. If you don't pass a heading, it captures
  the boat's current heading.
- The speed loop's gains (`kp` 0.5, `ki` 0.25) are internal tuning, not
  user-facing.

**Tips & gotchas**

- It holds *heading*, not a track — wind/current set where you actually go; the
  heading just keeps the bow pointed the way you want for the presentation.
- The motor steers by thrusting, so when it's braking in reverse the steering
  authority is weaker; heading control is firmest when it's nudging forward.
- This is meant for *low* speeds (the slider caps at 2.0 kn). For a true straight-
  line run at a set cruise, use [Heading hold](heading-hold.md) with Cruise
  Control instead.
- To stay on one *spot* rather than drift along a line, use
  [Anchor hold](anchor-hold.md).

**Safety** — **Stop** always overrides and drops to [Manual](manual.md) with the
motor zeroed. The safety governor's slew limiting, reverse delay, shallow-water /
no-go auto-stop, and (if enabled) loss-of-fix failsafe all apply — and because
the speed hold is GPS-derived, a lost fix means it can't trim speed correctly.
