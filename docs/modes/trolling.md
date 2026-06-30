# Trolling pattern

> A lazy-S weave around your heading at a held speed — cover more water and vary the lure action while trolling.

**What it does** — The boat drives forward at your set speed and adds a smooth side-to-side **sinusoidal heading offset** around a base heading — a continuous S-curve. The weave is `amplitude × sin(2π·t / period)`, so **amplitude** sets how far off course each swing goes and **period** sets how long one full left-right-left cycle takes. The base heading is captured as your **current heading** when you engage, and the boat weaves around that straight line. This is the classic trolling-motor S-pattern: it sweeps lures across a wider swath than a straight pull and repeatedly speeds up / slows down the lure on the outside vs. inside of each turn, which can trigger following fish.

**When to use it** — Trolling open water or a flat where you want to cover a wider lane than a straight line. Searching for active fish across a band of water. Adding action to crankbaits or spoons by varying their speed and direction through the S. Working back and forth over a known depth band without a fixed contour to follow.

**How to use it**
1. Point the boat in the direction you want to troll — this heading becomes the centreline of the weave.
2. Tap **🎣 More** to open the guided-modes flyout.
3. Pick **〰 Troll**. The Trolling panel opens.
4. Set **Amplitude** (how wide the weave).
5. Set **Period** (how slow/lazy each S is).
6. Set **Speed** (knots).
7. Tap **〰 Start trolling** to engage. A live weave indicator shows the current swing.
8. Changing amplitude, period or speed while running re-sends and takes effect live.
9. Stop with **■ Stop** in the nav bar.

**Settings**
- **Amplitude** — slider 5–60° (panel default 20°; backend default 20° if unspecified). Peak heading offset to each side of the base course.
- **Period** — slider 5–120 s (panel default 30 s; backend default 20 s if unspecified). Time for one complete left-right-left cycle. Longer = lazier, wider-spaced S.
- **Speed** — slider 0–3.5 kn (panel default 2.0 kn). Held via cruise control; with no speed set the mode falls back to its built-in cruise throttle (~40%).
- **Base heading** — taken from the boat's current heading at the moment you press start (the UI sends none, so the backend uses live heading).

**Tips & gotchas**
- The boat **does not navigate toward anywhere** — it just weaves around the heading you engaged on and keeps going. It will not turn back or follow a shoreline on its own; watch your water and stop / re-aim before you run out of room.
- Because the base heading is fixed at engage time, to change the overall direction you stop, re-point the boat, and start again. (Amplitude/period/speed are adjustable live; the centreline is not.)
- Big amplitude + short period = a tight, aggressive zig-zag that bleeds speed-over-ground; for a smoother troll keep amplitude moderate and the period long.
- It does not actively compensate for wind/current drift, so on a windy day the centreline may walk downwind over time.

**Safety** — **STOP always overrides.** Tapping **■ Stop** immediately drops to Manual with zero thrust. The safety governor (loss-of-fix failsafe, minimum-depth, no-go zones) still applies.

See also: [Contour follow](contour-follow.md) · [Circle / Orbit](orbit.md)
