# Contour follow

> Hold a depth contour (isobath) from the live sounder — let the boat ride the edge of a drop-off while you fish.

**What it does** — The boat drives forward at your set speed and steers to keep the sounder reading on your **target depth**. It watches the depth *trend* along its track (how the bottom slopes over the last few metres of travel) and curves toward deeper or shallower water to null the error, so it tracks a curving isobath instead of just driving straight. The **side** setting tells it which way to lean to correct — whether you want the deep water to starboard or to port. If the sounder gives no return (no depth), it simply holds its current heading and keeps moving until soundings come back.

**When to use it** — Working a breakline, weed edge, channel edge, or the lip of a drop-off where fish hold along a constant depth. Trolling a contour for suspended fish. Following the 5 m line along a flat where structure runs parallel to depth.

**How to use it**
1. Tap **🎣 More** on the mode rail to open the guided-modes flyout.
2. Pick **🪜 Contour**. The Contour panel opens.
3. Set **Target depth** (the depth you want to hold).
4. Choose which side the deeper water should be on: **Deep side** or **Shallow side**.
5. Set **Speed** (knots).
6. Tap **🪜 Follow contour** to engage.
7. The panel shows live **Current depth**, **Target depth**, and a centred **Error** bar so you can see how well it's holding. Adjusting depth/speed/side while running re-sends and takes effect live.
8. Stop with **■ Stop** in the nav bar (or switch to any other mode).

**Settings**
- **Target depth** — slider 0.5–50 m (panel default 5.0 m). The depth the boat tries to hold.
- **Side (deep / shallow)** — default **Deep side**. Picks which way the boat turns to correct, i.e. which bank you're favouring.
- **Speed** — slider 0–3.5 kn (panel default 2.0 kn). Held via cruise control (speed over ground). At 0 / no speed set, the mode falls back to its built-in cruise throttle (~50%).
- Under the hood the steering correction is proportional to the depth error, capped at 30° off the along-contour heading, so the approach to the line stays gentle. Re-evaluation of the bottom's slope happens every ~4 m of travel.

**Tips & gotchas**
- It needs a working **depth sounder**. With no soundings it just holds heading — it cannot find a contour from nothing.
- It engages from your **current heading** as the baseline, so point the boat roughly *along* the contour (parallel to the depth lines) before you start, not straight at the bank. Starting across the contour means a longer, more aggressive convergence.
- It follows the bottom it actually swims over — a single sounder reading. On a noisy or very irregular bottom the held line will wander. Keep speed modest for cleaner tracking.
- This is a *following* aid, not a depth alarm. To keep the boat off the shallows use the separate **minimum-depth** safety setting.

**Safety** — **STOP always overrides.** Tapping **■ Stop** immediately drops to Manual with zero thrust. The boat-wide safety governor (loss-of-fix failsafe, minimum-depth, no-go zones) still applies in this mode.

See also: [Circle / Orbit](orbit.md) · [Trolling pattern](trolling.md)
