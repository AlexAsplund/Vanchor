# Follow APB

> Let an external chartplotter or phone nav app do the routing — Vanchor-NG just steers the boat along the course it broadcasts.

**What it does** — Follow APB turns Vanchor-NG into the *autopilot* for a route
that lives somewhere else. It listens for the **APB** (Autopilot sentence "B")
that a chartplotter or navigation app emits, which carries the **bearing to the
destination**, the **cross-track error** (how far off the active leg you are),
and which way to steer to regain the track. Vanchor-NG steers to that bearing,
biased by a correction proportional to the cross-track error so it converges back
onto the leg — the same cross-track-following autopilot the built-in [route
mode](route.md) uses, just driven by someone else's waypoints.

The route source owns the waypoints, leg switching and arrival logic; Vanchor-NG
owns only the helm. If no APB sentence has been received yet, it simply **holds
the current heading and idles** (no thrust) until one arrives.

**When to use it** — When you already plan routes on a dedicated chartplotter,
MFD, or a phone app you trust, and you want Vanchor-NG to follow that route
instead of rebuilding it in this app. Also handy for integrating with existing
electronics on the boat.

**How to use it**

1. Set up your external device/app to output the **APB** sentence to
   Vanchor-NG's NMEA input, and start navigating a route on *that* device.
2. Tap **🛰 APB** on the mode rail to open the Follow APB panel.
3. Press **🛰 Follow APB**.
4. The panel shows **Last APB** — confirm sentences are arriving (it updates as
   they come in). The boat begins steering once it has one.

Set your **speed** on the dock nav bar (knots cruise or % engine power) as with
any guided mode, and use **Pause / Resume / Stop** there too. To change the
route, change it on the external device — the boat follows whatever it broadcasts.

**Settings / options**

- **Speed / throttle** — set on the nav bar (knots cruise control, or % engine
  power). Follow APB has no built-in speed control of its own.
- Cross-track correction strength and limits are fixed internally (gentle
  correction, capped so the boat doesn't slew hard onto the line).
- There are no waypoint, loop or patrol controls here — those belong to the
  external route source.

**Tips & gotchas**

- **Needs a live APB feed.** With no sentence yet, the boat just holds heading
  and idles — it won't move. Check **Last APB** is updating; if it's stuck on
  "—", the feed isn't reaching Vanchor-NG (wiring, output settings, or the app
  isn't actively navigating a route).
- **The external source is in charge of the route.** Arrival, leg advance and
  loop behaviour all happen on that device. Vanchor-NG only steers to the bearing
  and cross-track it's told.
- If you'd rather plan and own the route in Vanchor-NG itself — including
  water-only "take me here" routing, loops and patrols — use
  [Route / Waypoint navigation](route.md) instead.

**Safety** — **Stop always overrides.** The red **■ Stop** button (and the nav
bar Stop) drops to [Manual](manual.md) with the motor at zero regardless of what
the external source is sending. The safety governor still applies its slew
limiting, shallow-water and no-go auto-stop, and (if enabled) the loss-of-fix
failsafe — note that's the failsafe on *Vanchor-NG's own* GPS fix, independent of
the chartplotter's. A stale or absent APB feed leaves the boat idling on heading,
not running blind.

**See also:** [Route / Waypoint navigation](route.md) ·
[Area survey (map mode)](survey.md) · [Manual](manual.md)
