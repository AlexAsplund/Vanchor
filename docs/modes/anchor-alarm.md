# Anchor alarm (passive watch circle)

**Roadmap adoption #10** — Motor-OFF GPS watch circle over the physical
anchor: arm it from the Anchor panel, leave the boat unattended, and
Vanchor alarms you if the real anchor drags.

---

## What it is

The **anchor alarm** is a *passive observer* — it watches a GPS circle
on the server (the Pi on the boat) with the motor completely off.  When
the boat drifts outside the circle, Vanchor fires a high-severity alarm:

- A red banner across the top of the screen ("🚨 ANCHOR ALARM — dragging")
- The alarm sound (severity = high)
- The `anchor_alarm.firing` flag in telemetry (so Task-3 Web Push can forward it)
- The alarm ring on the chart turns red

The motor is **never activated** while the alarm is passive.

---

## How it differs from anchor-hold's drag alarm

| | Anchor alarm (this feature) | Anchor-hold drag alarm |
| --- | --- | --- |
| Motor | **OFF** — real anchor holds the boat | ON — Vanchor is actively station-keeping |
| Trigger | Boat outside the alarm watch circle | Boat > `drag_alarm_factor × radius` from the hold point |
| Purpose | Notify the operator that the real anchor is dragging | Notify during active motorised hold |
| Where | `core/anchor_alarm.py`, 1 Hz supervisor | `controller/safety.py:356-371`, 5 Hz governor |

---

## Server-side watch (phone can sleep)

The watcher runs inside the server's 1 Hz supervisor loop
(`Runtime._supervise_once`), NOT in the browser.  Once you arm it and
pocket your phone, the Pi keeps watching.  Breach events accumulate in
`anchor_alarm.firing`/`breach_count` telemetry; they surface the next
time the phone reconnects.

Task 3 (Web Push) will push breach notifications to your phone even while
the app is in the background.

---

## Arming, clearing, and recovering

**To arm:** open the Anchor panel (bottom rail) → scroll to "Anchor alarm
(motor off)" → set the radius → tap **🔔 Set alarm here**.  The alarm
anchors at the boat's current GPS position (or at a lat/lon you provide
explicitly via the API).

**To clear:** tap **Clear** in the same section, or send
`{"type": "anchor_alarm_clear"}`.

**One-tap recover:** while the alarm is armed (and the motor is
connected), tap **⚓ Recover — hold at alarm point**.  This sends an
`anchor_alarm_recover` command which engages the **normal `anchor_hold`
mode** at the alarm anchor point through the standard command path — device
gating, the safety governor, the reverse interlock, and every other failsafe
apply unchanged.  If `anchor_hold` actually engages, the passive alarm is
automatically disarmed (the governor's drag alarm then supervises the motorised
hold).  If the controller refuses (e.g. motor not connected), the passive
watch stays armed.

---

## Persistence

The armed state is saved to `<data_dir>/anchor_alarm.json` using the same
atomic-write pattern as `safety.json`.  A Pi restart with no phone connected
reloads the armed circle and continues watching immediately.  `firing` and
`breach_count` are NOT persisted — they are recomputed from live GPS after a
restart.

---

## Config keys

Both keys are in the `safety:` block of `vanchor.yaml` (or the embedded
defaults).  They change no behaviour until the operator arms the alarm.

```yaml
safety:
  anchor_alarm_default_radius_m: 30.0  # default radius offered by the UI slider
  anchor_alarm_stale_fix_s: 30.0       # alarm reports "stale GPS" past this fix age
```

---

## Safety statement

While the alarm is passive:

- It emits **zero motor commands**.
- It makes **no mode changes**.
- It has **no reference** to the controller, helm, governor, or motor objects.
- It is a pure observer of `state.position`.

The only code path from the alarm to a motor command is the
`anchor_alarm_recover` command, which is reachable **only from an explicit
operator tap** and routes through `controller.handle_command` with every
failsafe active.

Proven by: `tests/test_anchor_alarm.py::test_passive_watch_never_touches_motor_or_mode`
