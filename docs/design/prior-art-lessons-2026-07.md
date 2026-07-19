# Prior-Art Lessons for Vanchor-NG (ArduPilot + pypilot)

Synthesis date: 2026-07-19
Sources: `.superpowers/research/ardupilot-lessons.md`, `.superpowers/research/pypilot-lessons.md`
Lens: a software-first GPS virtual-anchor for **cheap trolling motors**, user = a
**non-technical lake fisherman**, running on a Pi/Orange Pi. Not a marine MFD, not a
fixed-wing/copter drone. Vanchor already has a strong safety floor and an ES-trained tiny-MLP
station-keeper — so this is deliberately skeptical about cargo-culting drone/sailboat habits.

---

## 1. Executive summary — the handful that would move the needle

Vanchor is much closer to these two projects than a naive reading of the notes suggests:
it independently arrived at the split-brain (MCU-owns-motor) architecture, thin-UI-over-a-
value-server, deadband anchor-hold, PID+residual "feed-forward-first" control, safety-floor
lockout, and GPS-course-vs-compass auto-alignment. Most notes land on **already done**. The
lessons that are genuinely *new leverage*, ranked for the fisherman-on-a-lake persona:

1. **A machine-readable parameter registry that the PWA renders from, with tiered
   Basic/Advanced exposure** (ArduPilot `apm.pdef.xml`). Vanchor's config is plain dataclasses
   with inline comments (`core/config.py`); the tuning UI is hand-built. As the PID+MLP knob
   count grows this is the cheapest way to keep it from becoming a wall of unlabeled sliders,
   and it's the single highest leverage / lowest risk borrow. **P0.**

2. **A guaranteed analytic anchor-hold fallback under *every* MLP policy, including a
   NaN/missing-model guard.** Smart (PID+residual) has a base; **Leif is pure with "no PID
   fallback" by its own docstring** (`controller/anchor_ml.py:314`). pypilot's most-proven
   lesson is that even its shipped neural pilot falls back to PID the instant a model won't
   load or while history fills. Make "no valid policy / NaN output" a non-event. **P0.**

3. **A `sqrt`-shaped position term for anchor-hold** (pypilot's `PR`). Vanchor's hold is a
   *linear* deadbanded spring (`controller/modes.py:473`). A `sign(e)·sqrt(|e|)` law gives
   firmer authority when a gust starts dragging you and gentler-than-linear growth on big
   excursions — exactly the "crisp near the mark without porpoising" behaviour a cheap motor
   at ~0 kt needs. Cheap to prototype in the Fossen sim against the current PD. **P1.**

4. **Min-effective-thrust / motor deadband compensation** (ArduPilot `MOT_THR_MIN`). Both
   projects hammer this for cheap drives. The anchor law's tiny corrections must actually
   overcome the trolling motor's throttle deadband or the boat sits dead while the controller
   "commands." A per-boat min-effective-thrust param is directly relevant. **P1.**

5. **A designed brownout story + PWA-heartbeat failsafe.** A 12 V trolling rail sags hard on
   motor inrush; a Pi browning-out mid-hold is a real anchor-alarm scenario. The most likely
   real failure for a lake user is the *phone* going away (screen lock, out of Wi-Fi), so the
   deadman must key on **PWA silence**, defaulting to HOLD, not just serial-link loss. **P1.**

Everything else is either already done, a "verify" nicety, or genuinely not applicable (see
§4). Do **not** adopt: on-device ML training, per-sea-state model zoos, full MAVLink/param
protocol, mission-scripting, a Lua VM, RTIMULib, gain-scheduling-by-speed, or MFD/charting.

---

## 2. Lessons by theme

### A. Failsafe & safety

- **Fail toward a stationary, human-recoverable state; the human can always take over in one
  action.** (ArduPilot, pypilot both.) **VERDICT: Already done** — dual-path STOP, loss-of-fix
  failsafe forces thrust to zero (`controller/safety.py:546`), land/shallow/nogo guard,
  external hardware watchdog (`hardware/watchdog.py`), firmware watchdog HOLD.

- **Safety values ratchet tighter, never weaker.** **VERDICT: Already done, arguably better
  than either source** — `SafetyFloor` / `SAFETY_FLOOR_KEYS` (`core/config.py:398`) locks
  `fix_failsafe_enabled` + `min_depth_m` so no hot-reload/profile/restore can weaken them.
  Neither ArduPilot nor pypilot has this crisp a lockout; keep and extend it.

- **Uniform failsafe-action enum** — every failsafe resolves to the same small end-state set;
  the user answers one question per trigger: STOP / HOLD / COME-BACK (ArduPilot `FS_ACTION`).
  **VERDICT: Adopt (M).** Vanchor's failsafes today mostly coast-to-STOP individually. For a
  trolling motor the useful set is `{STOP, HOLD-here, RETURN-to-mark}`. How: add a
  `FailsafeAction` enum in `controller/safety.py`, one config field per trigger
  (link-loss, fix-loss, low-battery), default **HOLD** for link/fix loss so the virtual anchor
  stays rather than drifting, and route all triggers through one resolver. Keep the STOP path
  above it untouched.

- **PWA/phone-heartbeat failsafe with a short default timeout** (ArduPilot GCS-loss 5 s). The
  PWA *is* the GCS; losing the phone is the single most likely lake failure. **VERDICT: Adopt
  / verify (S).** `link_failsafe` already exists (`obs/blackbox.py:57`); confirm the deadman
  keys on **PWA silence** specifically (not just serial-link loss), default action HOLD.

- **Log *why* a failsafe fired and surface it in the alarm/push.** **VERDICT: Adopt (S)** —
  blackbox records the alarm bits but the anchor-alarm/push text should say "phone lost — now
  HOLDING," not just "alarm." Wire the trigger reason into `core/anchor_alarm.py` / `push.py`.

- **Firmware independently caps current/stall and enforces limits regardless of the brain**
  (pypilot). **VERDICT: Already done** — `firmware/steering/steering.ino` has stall detection
  (`STALL_ERR_DEG`), optional BTS7960 current-sense, integrator clamp, watchdog-HOLD. Verify
  `STALL_CURRENT_ADC` gets set per-build (ships at 0 = disabled).

### B. Control & tuning

- **Feed-forward / base-model first, feedback trims** — validates "Smart = PID + residual."
  **VERDICT: Already done & validated.** Both projects independently confirm this is the
  field-proven shape (`controller/anchor_ml.py`). Lead with Smart, keep residual scale small
  (currently 0.3) and gains conservative.

- **`sqrt` proportional term for anchor-hold** (pypilot `PR`). **VERDICT: Adopt (S)** — see
  Exec §3. Prototype `sign(e)·sqrt(|e|)` (or `e·sqrt(|e|)`) against the linear spring at
  `controller/modes.py:473`, evaluated in the Fossen sim at real-time scale.

- **Question every integral term; prefer a bounded/learned bias over raw I** (pypilot ships
  its default pilot with I OFF). **VERDICT: Mostly already done** — helm-heading PID runs
  `ki=0.0` (`controller/controller.py:234`); anchor-helm uses `ki=0.25`
  (`controller/modes.py:157`) with projected anti-windup (`modes.py:195`) and conditional
  integration (`core/pid.py`). **Adopt (S):** audit whether the anchor-helm I is truly needed
  vs a bounded wind/current bias estimator, and make sure it can't wind up during link-loss /
  motor saturation.

- **Feed the controller a *low-passed heading rate*, never a raw finite difference**
  (pypilot). **VERDICT: Likely already done / verify (S)** — HWT901B provides fused rate;
  confirm the helm PID's D term consumes a smoothed rate, not on-the-fly differencing.

- **On-water one-button calibration** ("drive a slow circle 60 s and learn your boat" —
  ArduPilot QuikTune) and **"gains scale with actuator slew time"** (pypilot's 16 s-hard-over
  rule). **VERDICT: Partially already done, Adopt the rest (M).** Vanchor already has
  `still/align/interference` captures (`nav/calibration.py`) and `TUNING_JOBS` +
  `apply_tuned_gains` (`ui/server.py:571`). Missing: an on-water *circle* capture that measures
  the boat's steer slew + turn response and scales helm gains from it — far more realistic for
  a fisherman than PID sliders, and it matches the ES-offline-then-personalize story.

- **Min-effective-thrust / motor deadband compensation** (`MOT_THR_MIN`). **VERDICT: Adopt
  (S)** — add a per-boat `min_effective_thrust_n` in `BoatConfig` (`core/config.py`) applied so
  small anchor corrections clear the trolling motor's deadband. Distinct from the *idle*
  deadband (which is intentional).

- **No gain-scheduling by speed** (pypilot). **VERDICT: Skip / not applicable** — vanchor spans
  ~0 kt hold to cruise where response changes character; the existing **mode split**
  (anchor PD / cruise PID / drift) is the correct analogue. Borrow only the instinct: one smart
  nonlinear law per mode beats many scheduled linear gain sets.

### C. Sensor fusion & calibration

- **On-sensor AHRS instead of a realtime Pi fusion loop.** **VERDICT: Already done, better** —
  HWT901B fuses on the MCU, sidestepping pypilot's single biggest reliability chore (keeping
  RTIMU realtime on a loaded Pi). Do **not** port RTIMULib.

- **GPS-course vs compass-heading reconciliation as a free auto-cal.** **VERDICT: Already
  done** — `nav/calibration.py` `align` mode learns the compass/IMU mounting yaw offset from
  the steady heading-vs-COG difference on a straight run (`calibration.py:270`).

- **One-tap "boat is level" mounting-orientation capture** (pypilot's highest-leverage cal).
  **VERDICT: Adopt / verify (S)** — confirm the setup wizard has a single "sit still & level,
  tap to set" step capturing the mounting orientation; `still` capture exists but the *level/
  align* one-tap UX should be explicit in the wizard (`docs/setup-wizard.md`).

- **Continuous background gyro-bias learning, persisted across reboot** (pypilot re-persists
  every 60 s, reloads on boot). **VERDICT: Adopt (M)** — vanchor learns gyro bias only during a
  `still` *capture ritual* (`calibration.py:244`). Add a slow background bias estimate that
  persists and reloads at boot so the pilot starts with a good bias instead of drifting.

- **Passive, self-improving compass cal with a visible quality meter** (not a "do 3 circles"
  ritual). **VERDICT: Adopt (M)** — vanchor has the ellipsoid fit machinery
  (`controller/calibration.py:631`); surface a "compass health: good / turn a few circles"
  indicator and accumulate the fit during normal driving instead of gating it behind a ritual.

- **"Fusion loop missed its deadline" is a logged, monitored condition** (pypilot). **VERDICT:
  Adopt (S)** — add an AHRS sample-gap watchdog/telemetry counter so silent fusion stalls are a
  visible health signal, mirroring pypilot's "failed to keep time."

- **Accel/box cal is optional; minimize mandatory disassembly.** **VERDICT: Already aligned** —
  keep the only *required* step the one-tap level; mark everything else optional/advanced.

### D. Simulation & validation

- **A regression harness that runs the *real* control stack and asserts behaviour**
  (ArduPilot SITL + 300-test AutoTest, incl. failsafe & mode-transition tests, on CI).
  **VERDICT: Adopt — the big one (L, incremental).** Vanchor has the Fossen sim and a large
  test suite (`tests/test_anchor_*`, `test_azimuth_stationkeep`, `test_chaos`, etc.) but should
  codify SITL-style assertions over the shipped code: "anchor holds within X m over 5 min in
  0.3 m/s current," "link-loss → HOLD within N s," "STOP always stops," each run at **real-time
  scale** (MEMORY: sim time_scale distorts control) and avoiding the TestClient+Runtime hang
  (MEMORY). Prioritize **failsafe-path** tests — the least-exercised, most-critical code.

- **Never trust sim-optimal gains/policies blindly; validate on real water.** **VERDICT:
  Adopt / ongoing** — `docs/sim-vs-real.md` already flags this; pair every ES-in-sim policy
  with a real-water validation + the on-water calibration above before it's a default.

- **Deterministic replay: feed a recorded session's GPS/AHRS back through the real control
  code** (ArduPilot `LOG_REPLAY`). **VERDICT: Adopt (M)** — compounds with logging (§F); lets a
  field "the anchor drifted" report be reproduced on the bench.

### E. Params / config UX

- **Machine-readable parameter metadata (name, description, range, unit, default, advanced?)
  as a single registry the UI renders from.** **VERDICT: Adopt — highest leverage (M).** See
  Exec §1. Vanchor's `core/config.py` is plain dataclasses; add a registry (Python/JSON) and
  have the PWA build its tuning UI from it.

- **Tiered Basic/Advanced exposure** — 95% of users see ~3 controls (hold-tightness, max
  motor power, alarm radius); the rest behind an "Advanced" toggle. **VERDICT: Adopt (S, rides
  on the registry)** — see `docs/ux-revamp-concepts-2026-07.md`.

- **Named default parameter sets** ("kayak 30 lb", "16 ft aluminum 55 lb", "pontoon").
  **VERDICT: Already done** — `core/boat_profiles.py` (`BoatProfileStore`) is exactly
  ArduPilot's vehicle-default dropdown. Consider *shipping curated presets* rather than only
  user-created ones.

- **Split "underway UI" from "tuning UI"** — big buttons only on the water; graphs/gains/sim
  in a separate advanced area (pypilot's 5-control head vs OpenCPN plots). **VERDICT: Adopt /
  in progress** — tracked in `docs/ui-redesign.md` / `ux-review-2026-07.md`; the on-water screen
  should be glanceable at arm's length, wet hands, sunlight: state + 2–3 huge actions.

- **Full MAVLink param protocol / RebootRequired semantics.** **VERDICT: Skip** — vanchor
  restarts cheaply, isn't multi-GCS.

### F. Extensibility

- **A safe extension surface that *cannot bypass the safety floor*** (ArduPilot sandboxed Lua;
  extensions can't crash the core loop or override STOP). **VERDICT: Adopt the pattern + rule,
  not a VM (deferred).** Vanchor's paused "Pack framework" (`docs/design/pack-framework.md`,
  MEMORY) is this idea; bake in that any pack is *incapable* of overriding STOP or the deadman,
  exactly as ArduPilot isolates the core loop from Lua.

- **Applet + paired-`.md`, no-edit distribution** for non-technical users. **VERDICT: Adopt
  (pattern)** — curated presets/behaviours a user installs, not code they write.

- **Ship a general Lua/scripting VM.** **VERDICT: Skip (for now)** — more than the lake product
  needs; the Python plugin surface already exists.

### G. Logging / replay

- **The log is the product's nervous system; "post your recording" is the support model.**
  **VERDICT: Already done (recently)** — chunked crash-safe gzip debug recorder + app-log
  capture (recent commits), plus `obs/blackbox.py`, `obs/session_upload.py`.

- **Log control-loop *internals* (PID target vs achieved, MLP inputs/outputs, mode + failsafe
  transitions with reasons), not just raw telemetry** — that's what makes a log *replayable/
  diagnosable*. **VERDICT: Adopt (S/M)** — blackbox currently records mode + alarm bits
  (`obs/blackbox.py`); add the controller *decision* (PID terms, MLP obs/out, residual, active
  policy, transition reasons) so logs answer "why did it do that."

- **One-tap "send us your recording" from the PWA.** **VERDICT: Already done / verify** —
  `session_upload.py` exists; ensure it's one tap and opt-in/local-first.

### H. Deployment / reliability

- **Split-brain: the MCU owns the motor so a Linux hiccup can't run the drive open-loop.**
  **VERDICT: Already done, stronger than pypilot** — I2C tunnel to Pico + serial ASCII,
  firmware steering PID, dual-path STOP.

- **Designed brownout story on a sagging 12 V rail.** **VERDICT: Adopt (M, part electrical).**
  See Exec §5. Ensure a Pi reset triggers the MCU link-loss failsafe (not open-throttle);
  recommend a holdup cap / separate regulator so motor inrush can't reset the brain. Cross-check
  the watchdog/failsafe covers "brain browned out and rebooted while holding."

- **Local-first, no cloud dependence for core hold** (pypilot). **VERDICT: Already done /
  guard** — keep web-push + health-gated docker updates optional and local-first; core
  anchor-hold must never need connectivity (a lake may have none).

- **Health-gated updates / supervisor.** **VERDICT: Already done, ahead of both** — docker +
  supervisor with health gating beats pypilot's bare systemd.

---

## 3. Prioritized action-item backlog (Adopt items only)

### P0

- **P0-1 — Machine-readable param registry + PWA renders tuning UI from it; Basic/Advanced
  tiers.** Size **M**. Files: `src/vanchor/core/config.py` (add a registry: name, description,
  unit, range, default, `advanced` flag per tunable), `src/vanchor/ui/server.py` (expose it),
  PWA tuning view. Ship ~3 Basic controls: hold-tightness, max motor power, anchor-alarm radius.

- **P0-2 — Guaranteed analytic fallback under every MLP policy + NaN/missing-model guard.**
  Size **S/M**. Files: `src/vanchor/controller/anchor_ml.py`. On model load failure, NaN
  output, or while the history window fills, fall back to the deadbanded `pid_base`
  (`anchor_ml.py:36`) — including for **Leif** (currently "no PID fallback",
  `anchor_ml.py:314`). Make "no valid policy" a silent non-event; log it to blackbox.

### P1

- **P1-1 — `sqrt`-shaped anchor-hold position term.** Size **S**. Files:
  `src/vanchor/controller/modes.py:473` (position spring), evaluate in
  `src/vanchor/analysis/` + Fossen sim at real-time scale vs the current linear PD.

- **P1-2 — Min-effective-thrust / motor deadband compensation.** Size **S**. Files:
  `src/vanchor/core/config.py` (`BoatConfig.min_effective_thrust_n`),
  `src/vanchor/controller/modes.py` / `anchor_ml.py` (apply to small corrections). Distinct from
  the intentional idle deadband.

- **P1-3 — PWA-heartbeat failsafe defaulting to HOLD + trigger-reason in alarm/push.** Size
  **S**. Files: `src/vanchor/controller/safety.py`, `src/vanchor/core/anchor_alarm.py`,
  `src/vanchor/push.py`. Confirm the deadman keys on PWA silence, not just serial-link loss.

- **P1-4 — Uniform `FailsafeAction` enum `{STOP, HOLD, RETURN}`, one per trigger, default HOLD
  for link/fix loss.** Size **M**. Files: `src/vanchor/controller/safety.py`,
  `src/vanchor/core/config.py`. STOP path stays above it.

- **P1-5 — SITL-style regression assertions running the shipped control stack at real-time
  scale, failsafe-paths first.** Size **L (incremental)**. Files: `tests/` (new
  `test_failsafe_regression.py` etc.), `src/vanchor/sim/`. Codify: anchor-hold radius over 5 min
  in current; link-loss → HOLD within N s; STOP always stops. Honour the MEMORY gotchas
  (time_scale distortion; TestClient+Runtime hang).

- **P1-6 — Log control-loop internals to the blackbox** (PID terms target-vs-achieved, MLP
  obs/out, residual, active policy, transitions + reasons). Size **S/M**. Files:
  `src/vanchor/obs/blackbox.py`, `src/vanchor/controller/controller.py`.

- **P1-7 — Continuous background gyro-bias learning, persisted + reloaded at boot.** Size
  **M**. Files: `src/vanchor/nav/calibration.py`, `src/vanchor/nav/fusion.py`,
  `src/vanchor/core/prefs.py` (persist).

- **P1-8 — Brownout story: Pi reset → MCU link-loss failsafe (never open-throttle); document
  holdup-cap/regulator recommendation; watchdog covers "browned out while holding".** Size
  **M** (part electrical/docs). Files: `firmware/steering/steering.ino`,
  `src/vanchor/hardware/watchdog.py`, `docs/deploy-pi.md`.

### P2

- **P2-1 — On-water one-button circle calibration** (measure steer slew + turn response, scale
  helm gains). Size **M**. Files: `src/vanchor/nav/calibration.py`,
  `src/vanchor/analysis/tuning.py`, `src/vanchor/ui/server.py`.

- **P2-2 — Passive self-improving compass cal + "compass health" quality meter.** Size **M**.
  Files: `src/vanchor/controller/calibration.py`, PWA status.

- **P2-3 — Deterministic session replay through the real control stack** (recorded GPS/AHRS
  in). Size **M**. Files: `src/vanchor/obs/blackbox.py`, `src/vanchor/sim/`, `tests/`.

- **P2-4 — AHRS sample-gap watchdog / "fusion missed deadline" health counter.** Size **S**.
  Files: `src/vanchor/nav/fusion.py`, telemetry/state.

- **P2-5 — One-tap "boat is level & align" wizard step (verify/expose).** Size **S**. Files:
  `src/vanchor/nav/calibration.py`, `docs/setup-wizard.md`, PWA.

- **P2-6 — Audit anchor-helm integral vs a bounded bias estimator.** Size **S**. Files:
  `src/vanchor/controller/modes.py`, `src/vanchor/core/pid.py`.

- **P2-7 — Bake the "packs cannot override STOP/deadman" rule into the paused Pack framework.**
  Size **S (design)**. Files: `docs/design/pack-framework.md`.

---

## 4. Things vanchor already does as well or better (don't cargo-cult)

- **Split-brain MCU-owns-motor** with dual-path STOP + firmware steering PID + stall/current
  cutout — **stronger than pypilot's** (which lacks a formal deadman) and matches ArduPilot's
  core-loop independence.
- **Safety-floor lockout** (`SAFETY_FLOOR_KEYS` / `SafetyFloor`, ratchet-tighter-never-weaker)
  — a crisper, more testable guarantee than either project ships.
- **On-sensor AHRS (HWT901B)** — avoids pypilot's #1 reliability chore (realtime RTIMU on a
  loaded Pi). Do not port RTIMULib.
- **GPS-course vs compass mounting-offset auto-alignment** already implemented
  (`nav/calibration.py align`) — a lesson both notes list as "worth adopting."
- **Deadbanded, point-bow-then-drive anchor hold** already implemented
  (`controller/modes.py`) — matches ArduPilot boat-Loiter's core design.
- **PID+residual "feed-forward-first" control** — the field-proven shape; vanchor is *ahead*
  of pypilot, whose neural pilot is still `disabled=True` after years.
- **Named boat profiles** (`core/boat_profiles.py`) = ArduPilot vehicle-default sets.
- **Crash-safe chunked blackbox + app-log capture + session upload** — the ArduPilot "log is
  the nervous system" culture, already built.
- **Health-gated docker + supervisor** — ahead of pypilot's bare systemd.

## Traps the two projects learned the hard way (heed, don't repeat)

- **Integral windup on a wave/gust-yawed boat** — pypilot ships its default pilot with I OFF;
  bound/replace raw integral with a learned bias (P2-6).
- **Shipping the learned pilot as the sole path** — pypilot's neural pilot has been
  `disabled=True` for years with automatic PID fallback. Ship Smart (PID+residual) as default;
  gate pure-Leif behind explicit opt-in + fallback (P0-2).
- **Cheap ESC/motor deadband silently swallowing small corrections** — both projects flag it;
  it's *the* thing that makes a cheap-hardware anchor law "command but not move" (P1-2).
- **Untested failsafe paths** — the least-exercised, most-critical code; ArduPilot tests them
  in SITL specifically (P1-5).
- **Sim-optimal gains trusted blindly** — ArduPilot QuikTunes on the real vehicle; pair every
  ES-in-sim policy with real-water validation.
- **Scope-creep into a chartplotter/MFD** — pypilot deliberately stays a steering brain and
  lets OpenCPN own charts. Keep vanchor to anchor-hold + simple go-to; weather/routing stay
  thin optional add-ons.
- **Cloud dependence** — core anchor-hold must never need connectivity; keep push/updates
  optional and local-first.

### Where their lessons genuinely DON'T apply to vanchor

- **On-device ML training / MPC over a learned forward model** (pypilot `learning.py`/
  `intellect.py`) — never shipped; vanchor's offline-ES → fixed-policy inference is the
  safer/cheaper Pi choice. Don't chase it.
- **Gain-scheduling-by-speed avoidance** (pypilot) — vanchor's wider speed range makes its
  mode split the right analogue; don't collapse to one gain set.
- **Aggressive rate-loop AUTOTUNE step inputs** (Copter/Plane) — a slow trolling motor can't
  and shouldn't be perturbed that way; the QuikTune slow-circle is the right analog.
- **Full MAVLink param protocol, mission/DO_ scripting, per-sea-state model zoo, camera CNN,
  distributed compute** — all overkill or pure aspiration for a lake trolling-motor product.
