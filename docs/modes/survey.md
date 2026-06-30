# Area survey (map mode)

> Draw a box or polygon over an area and the boat covers it with tidy, evenly-spaced back-and-forth passes — a "lawnmower" pattern for mapping or scanning a patch of water.

**What it does** — Survey planning (a.k.a. "map mode") takes an area you draw and
generates a **boustrophedon** ("as the ox ploughs") coverage route: a set of
parallel passes a settable distance apart, walked in alternating direction so the
end of one pass connects to the start of the next with short turns. By default
the passes run along the area's **longest axis**, so each pass is as long as
possible and there are as few turns as possible. The result is an ordinary
waypoint route (WP1…DEST) that loads into the route editor **unstarted** — you
review it and press Start, after which it's followed exactly like any other
[route](route.md).

**When to use it** — Building a depth map / sonar coverage of a bay, scanning a
flat for fish, systematically searching an area, or any job where you want even,
repeatable coverage rather than a hand-drawn path.

**How to use it**

1. Tap **📍 Route** on the mode rail to open the route panel, then open the
   **Survey area (map mode)** card.
2. Draw the area:
   - **▭ Box** — drag a rectangle over the area, or
   - **✎ Freehand** — pointer-draw a shape and release to close it.
   The live shape is shown while you draw. (**Clear** discards it.)
3. After the area closes, the panel shows the area size and reveals the
   **Pass spacing** slider — set the distance between passes in metres.
4. Press **Plan survey**. The coverage waypoints load into the editor for review.
5. Press **▶ Start route** to run it.

The route is followed by the normal autopilot, so all the route controls apply:
**Pause / Resume / Stop** on the nav bar, the speed/throttle control, and live
editing of waypoints. (Patrol/Loop work too, but a one-shot sweep is the usual
choice.)

**Settings / options**

- **Pass spacing (m)** — distance between parallel passes. *Default 20 m* (range
  2–100 m on the slider). Tighter spacing = more passes = finer coverage but many
  more waypoints and a much longer run; wider spacing is quicker and easier.
- **Sweep direction** — automatic: the area's longest axis (fewest, longest
  passes). There's no manual angle control in the UI.
- **Area shape** — Box or Freehand polygon; needs at least 3 points.

**Tips & gotchas**

- **Mind the spacing.** A tiny spacing on a big area produces a huge number of
  waypoints. The planner warns once a route gets long (≈900+ waypoints) and
  refuses outright if it would exceed ~5000 ("increase the spacing"); if the
  spacing is larger than the whole area you'll get "no passes fit". When in
  doubt, start wider and tighten only if you need to.
- **The survey area is a coverage box, not a water mask.** Passes are clipped to
  the *polygon you drew*, not to the shoreline — draw your area to stay on water,
  or expect a pass to run across land where your box overlaps it. (For
  water-aware pathing, use [smart routing](route.md) instead.)
- Like every planned route, the survey loads **unstarted** for review — nothing
  moves until you press **Start route**.
- If the survey endpoint isn't available on your runtime the card disables itself
  and says so.

**Safety** — **Stop always overrides.** The red **■ Stop** button (and the nav
bar Stop) ends the survey immediately and drops to [Manual](manual.md) with the
motor at zero. Because a survey is run as a normal route, the safety governor's
slew limiting, shallow-water and no-go auto-stop, and (if enabled) loss-of-fix
failsafe all apply on every pass.

**See also:** [Route / Waypoint navigation](route.md) · [Work Area](work-area.md) ·
[Follow APB](follow-apb.md)
