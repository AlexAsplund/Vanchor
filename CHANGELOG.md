# Changelog

All notable changes to Vanchor-NG. Dates are ISO-8601.

## [1.1-alpha] — 2026-07-02

Phase 3 (UI/API maturity) and Phase 4 (nav & control quality) from the roadmap —
items 21–35. 46 files changed (+6,637 / −339), 876 tests. STOP-always-works
safety floor verified intact by a whole-branch review.

### UI / API (Phase 3)

- **Versioned WS envelope + command acks (#21)** — server→client messages carry
  `type`/`v`; telemetry frames add `seq`/`ts` (backward compatible). Seq-carrying
  commands get `{ack}`/`{nack}`; STOP confirms on the ack **or** the next
  telemetry frame, with a red banner only if neither arrives in ~1.5 s.
- **Screen Wake Lock (#22)** — `wakelock.js` keeps the screen awake while a motor
  mode is active. Requires a secure context, so it is a no-op over plain-HTTP LAN
  (needs HTTPS on the Pi to actually hold the screen).
- **Server-persisted safety geometry + prefs (#23)** — no-go zones / min-depth /
  fix-failsafe persist to `safety.json` and apply at Runtime init (survive a
  restart with no client). Generic `GET`/`PUT /api/prefs`; the browser adopts
  server geometry as truth with an echo guard.
- **Multi-client helm/observer roles (#24)** — first client is helm, later ones
  observers; helm disconnect auto-promotes; cooperative `take_helm`. Observer
  boat-commands are `role_denied`, but `stop`/`take_helm`/`ping` are always
  allowed (STOP is the safety floor). Broadcast frame serialized once per tick.
- **Playwright e2e + uitest repair (#25)** — STOP-integrity and reconnect/
  staleness browser tests behind an opt-in `e2e` marker + a `browser-e2e` CI job;
  `uitest.py` repaired (self-launching, 21/21).
- **Command audit log + offline-first queue (#26)** — bounded server audit ring at
  `GET /api/audit`; client command queue state machine (queued → sent →
  confirmed/failed) with an in-app panel. STOP is never queued; stale queued
  commands expire and never auto-replay.

### Nav & control (Phase 4)

- **Wind/current estimator service (#27)** — persistent Controller-owned estimator
  (never reset on mode change) drives waypoint crab feedforward (mean |XTE|
  10.7 m → 0.47 m on a beam set), spot-lock preload, and the Drift-mode axis.
- **Depth-aware routing (#30)** — `DepthMap.shallow_polygons()` (contours +
  soundings) hard-subtracted from navigable water with a soft-penalty band and a
  trap-safe fallback; on by default when `min_depth_m > 0`.
- **Adaptive gains + per-boat profiles (#31)** — SOG-keyed helm gain scheduling
  (more gain when slow; neutral default); per-boat saved gains in
  `boat_gains.json`; tuner can persist tuned gains.
- **Ground-track trolling (#32)** — the S-curve is a bounded rolling corridor of
  virtual waypoints, so the swath stays constant over ground under beam current.
- **Visibility-graph speedup (#33)** — lazy A* + reflex-vertex filtering: ~8×
  fewer visibility tests (216k → 27k on a near-cap basin), routes provably
  identical to an independent eager oracle.
- **ML anchor v2 (#34)** — mount/steer-sign correctness, a runtime residual-decay
  guardrail (never worse than the PID base), a spot-lock quality metric
  (RMS error / % in radius) in telemetry, and an offline `finetune.py`.
- **Vectored/azimuth station-keeping (#35, opt-in)** — vectors thrust against the
  set using the full rotation instead of the ±35° band; beam-set RMS 3.29 m →
  1.29 m. Default off reproduces the baseline hold bit-for-bit.
- Verified already complete from earlier work: signed drift-mode speed (#28);
  survey water-clip, concave-leg boundary routing, and waypoint passed-the-
  perpendicular arrival (#29).

### Fixes (from the whole-branch review)

- **Safety:** the offline-queue resend could re-engage the motor with a stale
  `manual` command on reconnect (after the link-loss failsafe had stopped the
  boat). Motor-engaging commands are now never resent, and any resend older than
  5 s is dropped.
- Trolling UI relabeled to metres; observer-dim CSS excludes all STOP controls;
  role sends get a 2 s per-client timeout; `safety_geometry` stripped from
  decimated telemetry frames.

### Known limitations

- Screen Wake Lock (#22) is inert over plain HTTP — needs HTTPS on the Pi.
- Stern-mount ML anchor (#34): the corrected training env shows the shipped
  residual is slightly negative for stern boats; the guardrail floors it to the
  PID baseline, but a retrain on the fixed env is a follow-up to gain on stern.

## [1.0-alpha] — 2026-07-01

Ground-up software-first rewrite that supersedes the original Vanchor (0.1-alpha):
Fossen 3-DOF physics, serial HAL, safety governor, observability, the full Tier
1–3 GPS trolling-motor feature set (spot-lock jog, cruise, track replay, drift,
chart-tap goto, contour follow) plus Work Area survey and a learned ML anchor.
Subsequently hardened by a full-project review (supervised control loop, motor
lifecycle, sensor staleness, link/fix failsafes, CI, columnar depth-chart store
that cut RSS from ~1.8 GB to ~180 MB and fits a 512 MB device). See `RELEASE.md`.
