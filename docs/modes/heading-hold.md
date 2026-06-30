# Heading hold

> A classic autopilot: pick a compass heading and the boat steers to hold it while you set the throttle.

**What it does** — Heading hold drives forward at a fixed throttle while the
shared autopilot helm steers to keep the boat on a **target heading**. The mode
itself just emits "hold *this* heading at *this* throttle"; the helm's tuned
heading PID does the actual steering, correcting continuously for wind and
current so the boat tracks the compass course rather than wandering. It holds
heading — not a ground track — so in a strong crosswind the bow points at your
heading while the boat may crab slightly downwind. Throttle defaults to a gentle
cruise (0.4) when you engage it from the UI.

**When to use it** — Running a straight line across open water, holding a course
into or along the wind, trolling a bearing, or any time you want a steady heading
without minding the wheel. Pair it with Cruise Control to also hold a speed.

**How to use it** — Tap **🧭 Heading** on the mode rail. The panel has:

- **Target heading** slider (0–359°, default 0).
- **🧭 Hold this heading** button — engages the mode at the slider's heading.

Set the slider to the bearing you want and tap **Hold this heading**. If you
engage heading hold without specifying a heading (e.g. via the dock nav bar), it
captures the boat's *current* heading and holds that. Adjust the throttle from
the dock nav bar's **Speed** control. Tap **Stop** (or pick another mode) to end.

**Settings**

- **Target heading** — the compass bearing to hold (default 0°). Re-tapping the
  button with a new slider value retargets immediately.
- **Throttle** — fixed forward power, default 0.4 (40%) when engaged from the UI.
  Override it with the **Speed** control: as a **% engine** throttle, or as a
  **knots** target (Cruise Control), which then owns the throttle and holds
  speed-over-ground instead of a fixed power.

**Tips & gotchas**

- It holds *heading*, not a track between two points. To follow a line that
  corrects for drift toward a destination, use Route/Waypoint mode instead.
- The helm low-passes its steering and only steers while making way, so at very
  low throttle the boat is sluggish to correct — give it enough thrust to steer.
- Cruise Control and the % throttle override both apply here (it's a "cruising"
  mode), so you can hold heading *and* speed at once.

**Safety** — **Stop** always overrides and drops to [Manual](manual.md) with the
motor zeroed. The safety governor's slew limiting, shallow-water / no-go
auto-stop, and (if enabled) loss-of-fix failsafe all apply. If you want the boat
to instead hold a fixed *spot*, see [Anchor hold](anchor-hold.md).
