# Route / Waypoint navigation

> Build a route of waypoints (or let the boat plan one onto water for you), then have the autopilot steer it leg by leg — once, on a loop, or there-and-back.

**What it does** — Route mode steers the boat through an ordered list of
waypoints, tracking each *leg* rather than just aiming at the next pin. It holds
the line between the previous mark and the next by correcting for **cross-track
error** (drift off the straight leg), so it crabs into wind and current and the
*ground* track stays true. For a mark that ends up close behind the boat it will
**reverse** straight toward it instead of swinging the whole boat around, when
that arrives sooner. On reaching a mark (within ~5 m) it advances to the next.
At the *end* of the route it does one of three things: a plain route **completes**
(and runs your on-arrival action), a **Loop** route wraps back to waypoint 1 and
keeps circling, and a **Patrol** route reverses direction and runs back the
other way — continuously.

The waypoints can come from anywhere: tapping the map, the smart "take me here"
planner (Fastest / Along-shoreline), a loop around an island, a saved route, or
a GPX import. The boat doesn't move until you press **Start route**.

**When to use it** — Running a milk-run between known spots, getting from the
ramp out to a fishing area on a water-only path that dodges land and islands,
patrolling a shoreline back and forth, circling a reef or island, or replaying a
route you saved last trip.

**How to use it**

*Select the mode.* Tap **📍 Route** on the mode rail. The route panel opens.

*Build a route by tapping (the manual way):*
1. Tap **＋ Add waypoints** (it turns into "✓ Tap map to add — done"). Until this
   is armed, ordinary map taps do nothing — so you can pan without littering pins.
2. Tap the map to drop waypoints in order. Each appears in the numbered list,
   where you can **rename** it or delete it with **✕**. Drag a pending pin on the
   map to move it.
3. Tap **＋ Add waypoints** again (or it clears on start) when done.
4. Press **▶ Start route**.

*"Take me here" smart water routing:* open **Take me here (smart route)** in the
panel.
1. Tap **Pick destination**, then tap the map. (Or long-press / right-click any
   spot, or open a saved marker's popup, and choose a route option directly.)
2. Choose **Routing mode**: **Fastest (direct)** or **Along shoreline**.
3. Press **Plan route**. The planner returns water-only waypoints **unstarted**
   into the editor for you to review/edit.
4. Press **▶ Start route**.

*Loop, Patrol, and on-arrival* — see Settings below.

*Pause / Resume / Stop* — while navigating, the nav bar in the dock shows
**Pause**, **Resume**, and **Stop**:
- **Pause** holds position (anchors where you are) and remembers the route and
  your progress, speed and throttle.
- **Resume** picks the route back up exactly where it left off.
- **Stop** ends navigation immediately and drops to Manual (and clears any
  paused route).

*Editing an active route* — you can edit a route while it's running. Drag a
numbered (coloured) waypoint to move it; **press-and-hold** a waypoint (~3 s)
for a menu to **Add waypoint before / after** or **Delete waypoint**. The boat
re-plans live and keeps navigating from its current target — it does **not**
restart at waypoint 1.

**Settings / options**

- **Routing mode** (smart route): **Fastest** = the shortest navigable
  water-only path (an exact obstacle-avoiding route that bends only at
  shore/island corners). **Along shoreline** = head to the nearest shore, hug an
  offset ring toward the destination into bays, and cut straight in once there's
  clear open water. *Default: Fastest.*
- **Shoreline offset** (Along-shoreline only) — how far off the bank to hug.
  *Default 30 m* (range 5–200 m). If the water is too narrow for the ring it
  falls back to a Fastest route automatically.
- **Patrol (there & back)** — checkbox in the route panel. At each end, reverse
  and run the route back, indefinitely. *Default off.* A **↩ patrol** badge lights
  while active.
- **Loop** — set by the **Loop around island** planner (not a manual checkbox).
  At the end the route wraps to waypoint 1 and circles continuously. A **↻ loop**
  badge lights. Clearing or starting a normal route drops the loop flag.
- **On arrival** (the tap-map **go-to** action) — **anchor** / **stop** / **none**.
  *Default anchor:* on reaching the destination the boat drops a virtual anchor
  and holds the spot; **stop** returns to Manual; **none** just sits idle. (Applies
  to single go-to destinations; loop/patrol routes never "arrive".)
- **Speed** — the dock nav bar holds a target speed in **knots** (cruise control
  holds speed-over-ground) or **% engine power**; toggle the unit on the bar.

*Around-island loop:* open **Loop around island**, set a **Shore offset**
(*default 20 m*), tap **Tap the island**, then tap (or drag) on a patch of land
fully surrounded by the lake. **Drag north for clockwise, south for
counter-clockwise** (a plain tap = clockwise). It loads a closed ring of
waypoints, starting nearest the boat, flagged as a loop — review and **Start
route** to circle continuously. If the island is too close to shore (or another
island) to fit a navigable offset all the way around, the offset is shrunk
automatically, or the request is rejected with a reason.

*Saved routes & GPX:* under **Saved routes** you can **Save route** (names the
current pending waypoints, stored on the device), **Load** one back into the
editor, **Delete**, **Export GPX**, or **Import GPX** (route points, then
waypoints, then track points). Imported/loaded routes land unstarted for review.

**Tips & gotchas**

- **Routes always load unstarted.** Smart routing, island loops, survey, saved
  routes and imports all drop waypoints into the editor for you to review — the
  boat doesn't move until you press **Start route**. This is deliberate: check
  the legs before committing.
- **Smart routing needs water data.** Fastest/Along-shoreline/island loops plan
  over OpenStreetMap water polygons — they need that data cached (offline) or an
  internet connection. If the planning endpoint isn't available the cards
  disable themselves and say so.
- **Start or destination on land** is rejected ("on land or outside known
  water"). The planner will snap a point that's only just off the mapped water,
  but not far.
- **Very large lakes are coarsened.** The shore is generalised (sub-10 m detail
  is dropped) and, on a huge merged water body, planning is clipped to a corridor
  around your route and the detail capped — so a route is still produced quickly
  rather than hanging. Expect the line to be a sensible approximation, not a
  pixel-perfect hug.
- A leading waypoint sitting right on the boat is dropped, so **WP1** is the
  first place it actually steers to.
- Route count is capped (≈50 waypoints for a route, ≈60 for an island loop); the
  planner simplifies to stay under, but never so far that a leg crosses land.
- The top-bar route chip shows **travelled ▸ remaining** distance plus time taken
  and an ETA from your rolling average speed.

**Safety** — **Stop always overrides.** The red **■ Stop** button (and the nav
bar Stop) ends the route at once and returns to [Manual](manual.md) with the
motor commanded to zero; it also clears any paused route. The safety governor
runs underneath every leg: it slew-limits the motor, auto-stops for shallow water
(if a minimum depth is set) and for no-go zones, and — if you've enabled it —
triggers the loss-of-fix failsafe. A plain route's **on-arrival anchor/stop**
fires once when the route completes.

**See also:** [Area survey (map mode)](survey.md) · [Follow APB](follow-apb.md) ·
[Work Area](work-area.md) · [Manual](manual.md)
