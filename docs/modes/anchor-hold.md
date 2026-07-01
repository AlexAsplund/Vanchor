# Anchor hold / Spot-Lock

> Drop a virtual anchor and let the boat hold that GPS spot — the "Spot-Lock" of a real GPS trolling motor.

**What it does** — Anchor hold keeps the boat near a fixed **anchor mark** using
the trolling motor. A PD controller on the GPS distance to the mark pulls the
boat back toward it (`kp`) while braking on the GPS closing speed (`kd`) so it
doesn't overshoot and orbit. Two behaviours matter in practice:

- **Drift anticipation.** Because the `kd` term acts on velocity (from real
  course/speed-over-ground), it counters wind/current *before* the position
  error builds — the boat sits within the radius with little maneuvering. The
  mode also estimates the environmental drift (ground velocity minus its own
  propulsion) and exposes it on the HUD.
- **Idle dead-band & reverse.** Within a small dead-band (~0.8 m) of the mark it
  idles the motor — with no thrust there's no yaw, so the heading just settles
  and the boat isn't worked. When the mark ends up *behind* the boat it backs
  straight up toward it instead of looping all the way around. It only enters
  active "recover" (re-point and drive back) once shoved clearly outside the
  radius, with hysteresis so GPS noise doesn't make it dart around.

A single bow-mounted, thrust-to-steer motor is underactuated — it can't actively
hold an arbitrary heading *while sitting still* (steering needs thrust, which
moves it off station). So this holds **position** and lets the heading settle,
exactly like a real GPS trolling motor. The heading shown is the one captured
when you dropped the anchor.

**Smart (learned) variant — `anchor_ml`.** There's an optional learned
spot-lock: a tiny neural net (~1.6k params, pure numpy) trained offline that adds
a small bounded correction on top of the same PID base — `command = clip(pid_base
+ 0.3·net)`. The robust PID base (dead-band idle, drive-to-mark, reverse-when-
astern) guarantees the worst case is just normal anchor-hold, while the residual
tightens the hold (eval: ~66% → ~80% within the watch circle). It registers only
if its model file loads, and falls back to standard anchor-hold if not. Turn it
on with the **"Smart station-keeping (learned)"** toggle in the Anchor panel,
then drop anchor as usual — the boat shows as **Anchor (Smart)** and the toggle
mirrors the live mode. It shares the same Anchor panel, radius and jog controls.

**When to use it** — Holding over a fishing spot, structure, or drop-off; staying
on a bait school; keeping position against wind/current without dropping a real
anchor; pausing on station mid-task.

**How to use it** — Tap **⚓ Anchor** on the mode rail. The panel has:

- **Anchor radius** slider (1–50 m, default 5).
- **Hold heading while anchored** toggle (on by default — see note below).
- **⚓ Drop anchor here** button — sets the mark at the boat's current position
  and engages the hold.
- **Jog anchor** D-pad (↑ ← → ↓) — nudges the mark boat-relative.

Tap **Drop anchor here** to anchor at your current spot. Changing the radius (or
the hold-heading toggle) while anchored re-applies to the existing mark rather
than re-dropping. Tap **Stop** to release.

**Jog (Spot-Lock Jog).** The D-pad moves the anchor a small step (default 1.5 m)
in a *boat-relative* direction — forward, back, left, right relative to the
current heading — so you can creep the hold point onto the fish without
re-dropping. Jog is ignored if no anchor is set.

**Settings**

- **Anchor radius** (default 5 m) — your watch circle. Inside it the boat
  station-keeps calmly; a tight radius still holds calmly because active recovery
  never triggers below an internal ~3.5 m floor (so a small radius doesn't make
  the boat dart against GPS noise). The radius also sets the **drag alarm**
  threshold (see Safety).
- **Hold heading** toggle — records the heading at drop for display. Note the boat
  holds heading *passively* (by idling, not by actively slewing); a lone bow motor
  can't force a heading while parked.
- **Jog increment** — default 1.5 m per D-pad tap.

**Tips & gotchas**

- It holds *position*, not heading — expect the bow to settle/weather-vane with
  wind; that's normal and intended.
- A very tight radius (1–2 m) sits below the GPS-noise + boat-length floor, so the
  boat idles and drifts gently within a few metres rather than chasing every
  wobble — calmer and easier on the motor.
- In stronger drift it works harder, occasionally backing up to re-point; the
  reverse prop is weaker, so give it room.
- Try **Anchor (Smart)** for a tighter hold; it can only ever do as well as, or
  better than, the standard hold by construction.

**Safety** — **Stop** always overrides and releases to [Manual](manual.md) with
the motor zeroed. An **anchor-drag alarm** trips if the boat drifts beyond
`drag_alarm_factor × radius` (default 2× the anchor radius) from the mark —
warning you the hold is failing (e.g. too much wind for the motor). Shallow-water
/ no-go auto-stop and the loss-of-fix failsafe apply as in every mode; note that
anchor hold relies entirely on GPS, so a lost fix means it can't hold position.
To hold a *moving* line instead, see [Heading hold](heading-hold.md) or
[Drift](drift.md).
