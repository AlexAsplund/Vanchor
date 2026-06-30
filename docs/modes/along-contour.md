# Along contour

> Tap a depth contour on the chart and the boat traces a track **along that
> isobath** — great for working a drop-off, a weed line, or a depth edge.

**What it does** — You tap a depth-contour line on the imported chart; the app
finds the nearest contour, **chains together its same-depth pieces** (the imported
isobaths arrive as many short fragments) into one continuous line through your tap,
and loads it into the **route editor** as an ordered track. A **closed** isobath
(a ring around a basin) comes back as a **loop**. From there it's a normal route —
you review/trim it and press **Start route**.

This is distinct from **[Contour follow](contour-follow.md)**, which dynamically
holds a depth from the *live sounder* as you go. "Along contour" traces a *fixed
line off the chart* — predictable and reviewable before you start.

**When to use it** — Following the 5 m break along a shoreline, tracing the edge of
a hole or hump, running a weed line, or any time you want the boat to stay on a
known depth contour rather than chase the live reading.

**How to use it**
1. Open the **Route** panel (the waypoint/route mode).
2. Expand **Along contour** → tap **▽ Pick a contour** (the depth-contour overlay
   switches on so you can see the lines).
3. **Tap a contour line** on the chart. The matching track loads into the editor
   above, and the status shows the depth, length, and waypoint count
   (e.g. *"Following the 5 m contour (loop, 2.8 km, 38 waypoints)"*).
4. Review — drag, insert, or delete waypoints like any route. Tick **Patrol** to
   run it there-and-back; **Loop** is set automatically for a closed contour.
5. Press **▶ Start route**.

**Settings / options**
- **Patrol (there & back)** — optional; at each end, reverse and run the contour
  back the other way continuously.
- **Loop** — set automatically when the contour is a closed ring; the boat circles
  it continuously.
- The loaded track is a normal route, so everything in [Route](route.md) applies
  (edit waypoints, save/GPX, pause/resume).

**Tips & gotchas**
- **Tap close to a line** — the snap tolerance is ~120 m; if you miss, the status
  says so, just try again nearer a contour.
- Needs an **imported depth chart** with contours (the lines you see on the chart).
- The track is the chained same-depth pieces **near your tap** (a windowed region),
  not necessarily the entire lake-long isobath — tap again further along to
  continue past the end.
- It loads **unstarted** for review — nothing moves until you press Start.

**Safety** — **Stop always overrides.** Once started it's a normal route, running
under the full safety governor (shallow/no-go auto-stop, link-loss hold,
loss-of-fix failsafe).

**See also:** [Route](route.md) · [Contour follow](contour-follow.md) · [Work Area](work-area.md)
