# Work Area

> Hop the boat across a set of fishing/work spots, holding station on each, and advance when you're ready.

**What it does** — Work Area visits a set of **spots** one at a time. It travels
to the active spot, then **holds position there** (a virtual anchor / spot-lock)
while you fish or work. When you're done, it advances to the next spot — either
on your command (Manual) or after a set dwell time (Timed). Optionally it can
hold a chosen heading at each spot, and loop or patrol the whole set.

**When to use it** — methodically working a string of spots: a row of brush
piles, a drop-off, marked waypoints from your last trip, or an even grid swept
across a bay so you cover the water without missing patches.

## How to use it

1. **Enter the mode.** On the mode rail tap **More** (🎣), then **Work Area**.
   The Work Area panel opens. Entering the mode is setup-only — the boat does
   **not** start moving until you press Start.

2. **Define your spots** — two ways, and you can mix them:
   - **Tap the map.** Tap **＋ Add spots** (it changes to *✓ Tap map to add —
     done*), then tap the map to drop each spot. Tap the button again when
     finished. Each spot appears in the numbered list.
   - **Draw an area and auto-generate a grid.** Open **Generate spots from an
     area**, pick **▭ Box** or **✎ Freehand**, and draw over the water. Set the
     **Spot spacing** slider, then tap **Generate spots**. This lays an even
     grid of spots inside your shape, ordered in a tidy back-and-forth
     (serpentine) sweep, and loads them into the list to review.

3. **Review / edit the list.** Each row shows the spot number, an editable
   **name**, an optional **hdg°** field, and a ✕ to delete it. **Clear spots**
   removes them all.

4. **Set a per-spot hold heading (optional).** Type a heading (0–359°) into a
   spot's **hdg°** box if you want the bow pointed a particular way while parked
   there (into the wind, along a ledge). Leave it blank for no specific heading.

5. **Choose how to advance.**
   - **Manual** (default) — the boat holds each spot until you tap the big
     **Go to next spot** button.
   - **Timed** — the boat auto-advances after the **Dwell per spot** time. The
     dwell slider lights up only in Timed mode. (The next-spot button still
     works in Timed mode to skip ahead early.)

6. **Pick end-of-list behavior (optional).**
   - **Loop (back to first)** — after the last spot, wrap to the first and keep
     going round.
   - **Patrol (there & back)** — at the end, reverse and work the spots back the
     other way.
   - Leave both off to simply **hold the final spot** once the set is done.

7. **Set Throttle** for the travel legs between spots.

8. **Tap ▶ Start Work Area.** The boat heads to the first spot and begins
   holding. Your editable pins are cleared and the active spots come back as the
   coloured route line.

### The "Go to next spot" button

A large **Go to next spot →** button appears at the bottom of the screen
**only while the boat is holding a spot** in Work Area mode. Its subtitle shows
your progress — **Spot 3 / 8** — and in Timed mode it also shows the dwell
**countdown** (e.g. `Spot 3 / 8 · 1:45`). Tap it to release the current spot and
travel to the next one. It disappears while travelling and reappears once the
boat settles on the next spot.

## Settings / options

| Control | Default | Effect |
|---|---|---|
| **＋ Add spots** | off | Arms map taps to drop spots. Tap again to finish. |
| **Spot list (name / hdg° / ✕)** | — | Rename, set an optional hold heading, or delete each spot. |
| **Clear spots** | — | Removes all pending spots. |
| **Advance** | Manual | *Manual* = hold until you tap the button; *Timed* = auto-advance after the dwell. |
| **Dwell per spot** | 60 s (range 5–600) | Timed-mode hold time before auto-advancing. |
| **Hold heading at each spot** | off | When on, holds a heading at every spot. With no per-spot value it uses the heading the boat had at Start. Per-spot **hdg°** values always win. |
| **Loop (back to first)** | off | Cycle the spots endlessly. |
| **Patrol (there & back)** | off | Reverse at the end and work back. |
| **Throttle** | 60% (range 10–100) | Forward drive used on the travel legs between spots. |
| **▶ Start Work Area** | — | Sends the spots and engages the mode. |
| **Spot spacing** (area card) | 40 m (range 5–200) | Grid spacing when auto-generating spots from a drawn area. |
| **Generate spots** | — | Builds the serpentine grid from your drawn area. |

A spot is considered "arrived" once the boat is within about **8 m** of it; the
hold itself uses the standard anchor radius (about **5 m** by default — see
[Anchor hold](anchor-hold.md)).

## Tips & gotchas

- **Travel between spots is direct.** Legs run straight to the next spot with
  cross-track correction — there is **no land-avoidance routing yet**. Place
  spots (or draw your area) so the straight hops stay over open water. For
  routing around islands, use a [Route](route.md) instead.
- **Heading hold is best-effort.** A single bow-mounted trolling motor is
  underactuated: it can't perfectly hold a heading **and** a position at the same
  time, because steering needs thrust, which nudges the boat off station.
  **Position keeps priority** — if the boat drifts out of the hold radius it will
  re-point and drive back to the spot, abandoning the heading until it's parked
  again. Treat per-spot headings as a gentle preference in calm conditions, not a
  guarantee.
- **Spot count caps.** Auto-generated grids are capped at **250 spots**; too
  tight a spacing on a big area is refused with a message — widen the spacing.
- **Generated spots can land on land.** The grid fills your drawn shape; spots
  that fall outside the water are clipped where the runtime can determine the
  shoreline, but on a runtime without that data they aren't — eyeball the result
  and delete any spot that sits on dry ground before starting.
- **Tiny areas / wide spacing.** If no spots fit inside the shape at the chosen
  spacing you'll get a "no spots" message — use a smaller spacing or a bigger
  area.
- **Spot-generation may be unavailable.** On some runtimes the area card
  disables itself; the tap-to-place flow and Start still work normally.

## Safety

- **STOP always overrides.** The hardware/UI STOP cuts propulsion and drops out
  of Work Area regardless of phase (travelling or holding).
- The hold at each spot is an **active spot-lock** (the same virtual anchor used
  by [Anchor hold](anchor-hold.md)): the motor keeps working to fight wind and
  current drift, so expect the prop to run intermittently even while "parked."
- Loop and Patrol keep the boat working **indefinitely** — it won't stop on its
  own at the end. Keep an eye on it, or use a plain (non-loop) set so it settles
  on the last spot.
