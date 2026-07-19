# PID & gain reference — every control loop, every knob

Every closed loop in vanchor, what signal it consumes and produces (with
units), the shipped values, **what increasing/decreasing each gain does to
the boat**, and the mis-tune symptoms to recognise. Config keys live under
`control:` in `vanchor.yaml` unless noted. The learned anchor modes (Smart /
Leif) have **no gains** — they are neural policies (see
[anchor-ml.md](anchor-ml.md)); Smart's PID *base* is the anchor-hold law
below.

## The loops at a glance

| Loop | Error → output (units) | Kp | Ki | Kd | Where |
|---|---|---|---|---|---|
| Helm (heading) | heading err (°) → steering (norm −1…1) | 0.035 | 0 | 0.012 | `control.heading_*` |
| Anchor hold | distance (m) & closing speed (m/s) → thrust (norm) | 0.12 /m | — | 0.6 /(m/s) | `control.anchor_kp/kd` |
| Cruise (SOG) | speed err (kn) → thrust (0…1) | 0.64 | 0.25 | 0 | `control.cruise_kp/ki` |
| Drift (speed) | drift-speed err (kn) → thrust (−1…1) | 0.5 | 0.25 | 0 | `control.drift_kp/ki` |
| Waypoint XTE | cross-track (m) → bearing correction (°) | 2.0 °/m | — | — | `control.waypoint_xte_gain` |
| Manual-course XTE | cross-track (m) → bearing correction (°, ±45 cap) | 2.0 °/m | — | — | code constant (`modes.py`) |
| Steering head (firmware) | head angle err (°) → motor PWM | 6.0 | 0.8 | 0.6 | `firmware/steering/steering.ino` |

---

## 1. Helm heading PID — the steering loop every guided mode shares

`controller.py` `Helm`. Error = **shortest** angular difference between the
commanded bearing and the compass heading (degrees, ±180); output = steering
command in −1…1. Every bearing-tracking behaviour steers through this one
loop: waypoint/route legs, orbit, contour, trolling, work-area, Follow-APB,
RTL, and manual **absolute/course** modes (the server holds the bearing).

Shipped: `heading_kp: 0.035`, `heading_ki: 0.0`, `heading_kd: 0.012`
(auto-tuned compromise — see §8). With kp 0.035/°, full steering is reached
at ~29° of heading error.

- **Kp ↑** — snappier turn-in, tighter course capture. Too high: the boat
  S-curves (weaves) down a leg, worse the faster you go — steering authority
  scales with water speed, so a gain that is calm at 0.5 kn oscillates at
  2 kn (that is what the gain schedule, §7, is for). Symptom: wake looks
  like a sine wave; steering telemetry ping-pongs sign.
- **Kp ↓** — lazy, wide arcs; slow to recover from gusts; persistent XTE on
  legs (the XTE correction, §5, can only ask for a bearing — a weak helm
  won't hold it). Symptom: boat "crabs" downwind of every leg and takes
  many boat-lengths to settle after a turn.
- **Kd ↑** — damps overshoot when capturing a new bearing (anticipates by
  reacting to error *rate*). Too high: the derivative amplifies the ~1°
  compass noise at 5 Hz into constant small steering reversals — the head
  chatters (the `steer_tau` low-pass, §7, hides some of this but adds lag).
  Symptom: steering telemetry is fuzzy/noisy while the heading error is
  basically constant.
- **Kd ↓ / 0** — overshoot on every capture: the bow swings through the
  target bearing and hunts a few times before settling.
- **Ki is 0 on purpose.** A steady heading offset (weather helm from wind /
  current) *would* be the classic Ki use-case, but underway the XTE loops
  (§5) already trim the *track*, which is what actually matters, and an
  integrator here winds up during long commanded turns and then overshoots.
  If you enable it, keep it tiny (≤ 0.005) and expect slow oscillation if
  you overdo it. Symptom of too much Ki: a slow, deliberate weave with a
  period of tens of seconds (integrator charging/discharging).

## 2. Anchor hold (`anchor_hold`) — P-on-distance, D-on-closing-speed

`modes.py` `AnchorHoldMode`. Not a textbook PID: **thrust = clip(kp·distance
− kd·closing_speed)**, i.e. P on how far you are from the mark, D on how
fast you're approaching it (GPS closing speed) so the boat brakes instead of
overshooting. This same law is Smart's PID base/safety floor.

Steering points at the mark through the Helm loop (§1); the opt-in
`station_keep_azimuth_deg` lets the azimuth sweep wider than the autopilot
band to push directly against the set.

Shipped: `anchor_kp: 0.12` (thrust per metre — full thrust at ~8 m),
`anchor_kd: 0.6` (braking thrust per m/s of closing speed; negative values
of the sum command reverse).

- **Kp ↑** — harder pull back to the mark, tighter circle in strong set.
  Too high: arrives hot, overshoots the mark, and orbits/figure-eights
  around it (with kd fixed), burning battery. Symptom: distance telemetry
  oscillates instead of decaying; thrust rails at max.
- **Kp ↓** — bigger settled offset downwind of the mark (needs more error
  to raise enough thrust); slow recovery after a gust. Symptom: boat parks
  persistently on one side of the circle in any wind.
- **Kd ↑** — earlier, stronger braking on approach; kills overshoot; the
  *anticipation* that makes the hold calm. Too high: the boat gets timid —
  it brakes so early it never quite reaches the mark, and GPS
  velocity noise (~0.1 m/s, more under multipath) feeds straight into
  thrust twitches. Symptom: hovers short of the mark; thrust flutters at
  anchor with the boat basically still.
- **Kd ↓ / 0** — surges through the mark and hunts back and forth along the
  wind axis. Symptom: distance sawtooths.
- **`idle_deadband_m` (0.8, `AnchorConfig`)** — inside this distance the
  motor IDLES entirely: with ~1.5 m GPS noise, chasing the last metre means
  perpetual hunting; idling instead lets the boat sit (and passively hold
  heading, since a stopped motor doesn't yaw it). Wider: calmer + quieter,
  but more real drift before correction. Narrower/0: the classic
  small-radius overcorrection dance returns. Halved settling time and
  oscillation in the measured A/B (docs/analysis.md).
- `AnchorConfig.max_thrust` (1.0) clamps the law's output; lowering it
  softens everything (and lengthens gust recovery) without retuning kp/kd.

## 3. Cruise Control (constant SOG)

`controller.py` `cruise_pid`: error in **knots**, output thrust 0…1 (never
reverse). Shipped `cruise_kp: 0.64`, `cruise_ki: 0.25`, kd 0 (auto-tuned).

- **Kp ↑** — faster reaction to speed drops (weed, chop, turns). Too high:
  throttle pumping — SOG estimate noise (± ~0.1 kn) is multiplied straight
  into thrust. Symptom: audible motor surging on flat water.
- **Kp ↓** — sluggish; big speed sag entering wind/waves, slow recovery.
- **Ki ↑** — how fast the steady-state error is trimmed out (P alone always
  needs *some* error to hold thrust against drag). Too high: slow
  overshoot/undershoot cycles around the setpoint (integrator windup —
  bounded by the PID's anti-windup clamp, but still oscillatory inside the
  clamp). Symptom: speed slowly porpoises ±0.3 kn with a period of ~30 s.
- **Ki ↓ / 0** — never quite reaches the set speed (parks ~0.2–0.5 kn low,
  worse into wind).
- **Kd** — intentionally 0: SOG is too noisy at 5 Hz for a useful
  derivative; adding it mostly amplifies noise.

## 4. Drift mode (controlled drift speed)

`modes.py` `DriftMode`: same structure as Cruise but output −1…1 — it can
**reverse to brake** a too-fast drift. Shipped `drift_kp: 0.5`,
`drift_ki: 0.25`. Tuning behaves exactly like Cruise (§3); the extra
failure mode is **Kp too high → forward/reverse chatter** around the target
drift speed (reverse engages the firmware's 1 s reverse dead-time each flip,
so chatter feels especially clunky and is easy to hear). If you see
alternating sign thrust at near-constant drift speed, lower kp first.

## 5. Cross-track (XTE) corrections — P-only by design

Two places convert "metres off the line" into a **bearing correction**
consumed by the Helm loop (§1) — nested P loops, no I/D:

- **Waypoint/route legs**: `waypoint_xte_gain: 2.0` °/m.
- **Manual course mode** (steer a track line): `COURSE_XTE_GAIN = 2.0` °/m,
  capped at `COURSE_MAX_CORRECTION_DEG = 45` (code constants in
  `modes.py` — change there if ever needed).

Effects: **gain ↑** = snappier return to the line; too high and the *outer*
loop oscillates — the boat weaves across the line even though the Helm
itself is well-tuned (classic cascaded-loop interaction: the outer loop
must stay slower than the inner one). **Gain ↓** = long, lazy converge and
persistent downwind offset in crosswind. The 45° cap bounds how hard course
mode will cut back to the line regardless of gain; the same cap idea keeps
route legs from doubling back when far off-line.

## 6. Steering-head position PID (firmware — the only *hardware* PID)

`firmware/steering/steering.ino`: error = head angle − target (degrees, from
the AS5600), output = signed motor PWM. **These are firmware constants, not
config** — retune requires reflash + bench (BENCH-VERIFY discipline).

Shipped: `KP 6.0` PWM/°, `KI 0.8`, `KD 0.6`, `INTEGRAL_LIMIT 120` PWM,
`DEADBAND_DEG 1.2`.

- **KP ↑** — faster head slew to target. Too high: overshoot + hunting
  around the target (the worm gear's static friction makes limit-cycling
  easy). Symptom: audible buzz/oscillation at the head after each command.
- **KI ↑** — pulls out the last degree of steady error the deadband would
  otherwise leave (worm friction bias). Too high: slow creep-overshoot
  cycles; the `INTEGRAL_LIMIT` clamp bounds the worst of it, and the
  deadband handler bleeds the integrator when settled.
- **KD ↑** — damps the head's inertia at arrival (the "soft landing"). Too
  high: amplifies AS5600 quantisation into PWM ripple mid-travel.
- **DEADBAND_DEG** — inside it the bridge stops and the **self-locking worm
  holds** (zero power, zero hunt). Wider: less hunting and less power, but
  the runtime sees up to that much steady angle error (1.2° ≈ the STEERD
  wire resolution ×12; the autopilot tolerates it fine). Narrower: crisper
  tracking, risk of perpetual micro-hunting the worm was chosen to avoid.
- **Runtime interaction:** the Pi-side stack treats the head as ideal — the
  sim does not model this loop's settle/deadband yet (documented gap in
  [sim-vs-real.md](sim-vs-real.md)), which is one reason sim-tuned helm
  gains can need a small nudge down on the real boat.

## 7. Things that *interact* with the PIDs (tune these first!)

Symptoms that look like bad PID gains are often these instead:

- **`steer_tau` (0.6 s low-pass on helm output)**: hides compass-noise
  chatter; raising it adds control lag that *mimics* low-kp sluggishness;
  0 disables.
- **Adaptive helm gain schedule** (`steer_gain_*`, default neutral): scales
  the *effective* heading kp with SOG (more gain when slow/weak authority,
  less when fast). If the boat is sluggish at trolling speed AND weaving at
  cruise, set the schedule (e.g. `mult_lo 1.5, mult_hi 0.7`) instead of
  compromising the base kp.
- **`autopilot_steer_deg` (boat profile)**: guided steering is rescaled so
  ±1 from the Helm is this many physical degrees (35 by default) regardless
  of the mechanical range — changing the boat's mechanical swing does NOT
  change autopilot authority, so don't retune the helm after a hardware
  steering change.
- **Safety governor slew limits** (thrust 2.0/s, steering ~95°/s-equival.):
  bound how fast any PID's output physically moves — very aggressive gains
  end up slew-limited (looks like lag), and derivative action is partially
  masked.
- **`steer_sign` (mount polarity)**: bow vs stern flips the steering lever
  arm. If every heading loop diverges immediately, it's this, not the
  gains.
- **Nav fusion gains** (`GAIN_KEYS`: `heading_gain`, `vel_tau_s`, …): filter
  time-constants for the GPS/compass fusion, calibrated per-boat by the
  fusion calibration — they change the *quality/lag of the inputs* the PIDs
  see. Retune fusion before touching control gains if headings lag turns.
- **`AnchorMLMode.thrust_tau_s` (0.7 s, first-order low-pass on Smart/Leif
  OUTPUT thrust)**: the ES training env models steering slew but not the
  motor's 1 s forward↔reverse dead-time, so trained policies can flip thrust
  sign several times a second. Without smoothing, the governor and firmware
  block ~45% of those reversals, zeroing the braking the policy wanted and
  leaving it hunting. The low-pass turns sub-second sign oscillations into a
  command the motor can execute (measured: reverse-block events drop from 45%
  to 16% of ticks). Tuning interaction: *too high* mutes real braking thrust
  on a fast approach; *too low* lets the raw oscillating signal through
  again. Set to 0 to restore the raw policy output (useful when benchmarking
  the policy itself). This is the `control.anchor_ml_thrust_tau_s` knob if
  ever broken out into config; currently a constructor default.
- **Reverse dead-time and the governor advisory**:
  - The **firmware / sim motor** apply a 1.0 s reverse dead-time
    (`SimMotorConfig.reverse_delay_s: 1.0`) — thrust is held at zero for 1 s
    after a sign flip. This is the dead-time the learned anchor policies
    were trained *without*, hence the `thrust_tau_s` mitigation above.
  - The **safety governor** has a separate Pi-side interlock
    (`SafetyConfig.reverse_delay_s: 0.5`) that fires earlier; the stricter
    of the two governs in practice.
  - The UI governor advisory banner has a **4 s dwell** (`GOV_DWELL_MS =
    4000` in `safety.js`): the "Reverse blocked" notice only appears after
    the governor has been *continuously* blocking for 4 s, so routine
    sub-second interlock flicker during Smart/Leif station-keeping never
    surfaces the banner. If you see the banner during anchor hold, the policy
    is genuinely stuck in a reverse-demand loop — the `thrust_tau_s` value is
    too low or zero.

## 8. Auto-tuning — where the shipped numbers come from

`analysis/tuning.py` (`python -m vanchor.analysis.tuning`-style jobs; see
[analysis.md](analysis.md)): coordinate-descent over recorded/sim scenarios.

- **`heading` job**: tunes `heading_kp` ∈ [0.005, 0.08] and `heading_kd` ∈
  [0, 0.06] against settle-time + overshoot + effort metrics; baseline
  (0.035, 0.012) is the shipped compromise (faster settle while staying
  anchor-safe).
- **`cruise` job**: tunes `cruise_kp`/`cruise_ki` the same way.
- Anchor kp/kd, drift, and XTE gains are currently hand-tuned (values above
  document the reasoning); the firmware head PID was bench-tuned.
- The CI regression gate (`scripts/regression_check.py`) pins
  settle/overshoot metrics for the shipped gains — if you retune, re-baseline
  deliberately (see [testing-and-workflow.md](testing-and-workflow.md)).

## Changing values

Runtime gains: edit `vanchor.yaml` (`control:` keys above) and restart — or
run the auto-tune jobs and adopt their output. Firmware gains: edit the
constants in `firmware/steering/steering.ino`, reflash, bench-verify (watch
for hunting at several target angles under load) before water use. After
any gain change, sanity-run the regression scenarios and a sim mission; for
anchor gains, watch one gust cycle at anchor in sim (`weather_preset`).
