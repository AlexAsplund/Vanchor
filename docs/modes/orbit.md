# Circle / Orbit

> Loop a point at a set radius — circle a piece of structure, a buoy, or a hot spot, hands-free.

**What it does** — You drop a centre point on the map and the boat circles it at a fixed **radius**, in the direction you choose (clockwise or counter-clockwise), driving forward at your set speed. Each tick it works out the bearing from the centre to the boat, aims a little *ahead* along the ring (the tangent), and biases that heading by a radial correction — so if it's outside the ring it steers inward, if inside it steers outward. The result is that it spirals onto the circle from wherever it starts and then holds it. The live **range to centre** is shown so you can watch it converge.

**When to use it** — Circling an isolated piece of structure (a brushpile, rockpile, standing timber, a wreck) to present baits from every angle. Holding a working radius around a marker buoy or a school you've marked. Slow-trolling a circle over a spot to keep lures in the zone.

**How to use it**
1. Tap **🎣 More** to open the guided-modes flyout.
2. Pick **🎯 Orbit**. The Orbit panel opens.
3. Tap **📍 Tap map to set centre**, then tap the map where you want the centre. The chosen centre is shown, and a dashed preview circle is drawn at the current radius.
4. Set **Radius**.
5. Choose **Direction**: **↻ CW** or **↺ CCW**.
6. Set **Speed** (knots).
7. Tap **🎯 Start orbit** (it stays disabled until a centre is set).
8. The panel shows live **Range to centre** vs the target radius. Changing radius, direction or speed while running re-sends and takes effect live.
9. Stop with **■ Stop** in the nav bar.

**Settings**
- **Centre** — tapped on the map. Required before you can start.
- **Radius** — slider 5–200 m (panel default 25 m; backend default 20 m if unspecified). Distance the boat holds from the centre.
- **Direction** — **cw** (default) or **ccw**.
- **Speed** — slider 0–3.5 kn (panel default 2.0 kn). Held via cruise control; with no speed set the mode falls back to its built-in cruise throttle (~50%).
- Convergence onto the ring is proportional to the radial error and capped at 60° of correction off the tangent, so it eases onto the circle rather than cutting in hard.

**Tips & gotchas**
- It needs a **GPS fix** and a centre point; with no fix it idles and holds heading.
- A very **small radius** relative to the boat's turning ability will look loose — the boat can only turn so tight at speed. If the circle looks sloppy, increase the radius or drop the speed.
- It converges from wherever you start it, so you don't have to be on the ring when you press start — it will spiral in.
- Wind and current will skew the circle slightly; it corrects continuously but doesn't pre-empt drift the way the anchor/waypoint modes do.

**Safety** — **STOP always overrides.** Tapping **■ Stop** immediately drops to Manual with zero thrust. The safety governor (loss-of-fix failsafe, minimum-depth, no-go zones) still applies.

See also: [Contour follow](contour-follow.md) · [Trolling pattern](trolling.md)
