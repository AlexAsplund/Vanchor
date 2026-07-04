# Changelog

All notable changes to Vanchor-NG. Dates are ISO-8601.

## Unreleased

## [1.4.0a1] — 2026-07-04

## [1.4.0a0] — 2026-07-04

- **Smart station-keeper upgraded to a full-azimuth hybrid.** The learned
  residual is now trained with a wide (120°) steering swing, so it *vectors*
  the motor through its full rotation on top of the PID base (rescaled to the
  boat's mechanical range at deploy). It strictly dominates the previous ±35°
  hybrid and plain PID on the held-out set — **90.6% time-in-radius vs PID's
  82.4%** (≤6 m/s), **90.4% vs 70.2%** on the full 0–12 m/s regime, **100% on
  both bow and stern** (PID: bow 99.8%, stern 79.5%), tighter mean distance,
  without thrashing the motor — and the residual-decay guardrail still floors
  it to the PID base if it ever underperforms.
- **"Leffe 🍺"** — an experimental *pure* full-azimuth learned anchor mode (no
  PID base) selectable from the Anchor panel (with an info-icon tooltip). It
  holds a stern mount exceptionally tight (~98% in-radius) but runs the motor
  hot and has no PID fallback — a fun research mode. The boat mode badge now
  distinguishes the keeper (Anchor / Anchor · Smart / Anchor · Leffe 🍺).
- Anchor training tooling gained `--pure`, `--steer-range`, and condition-cap
  flags (the recipe behind the above).

## [1.3-alpha] — 2026-07-02

- **ML anchor retrained** on the sign-faithful env (fixing the #34 follow-up):
  the shipped policy was trained on the old steering-sign-flipped env and was
  actually *worse* than PID (71.8% vs 75.6% time-in-radius) at 3× the motor
  energy, with a broken stern mount (61.5%). The retrained `anchor_policy.json`
  is at parity with PID (75.0%), holds a tighter mean distance, uses **3–4×
  less motor energy**, and recovers the stern mount (61.5% → 74.1%).
- **Vectored/azimuth station-keeping** validated on the stern mount (a clear
  win, not just bow) and exposed as a "Vectored thrust (full rotation)" toggle
  in the Anchor panel; the analysis runner can now score it.
- Service-worker cache version is now a **content hash of the static shell**,
  injected into `sw.js` at serve time — the PWA auto-refreshes exactly when
  assets change, with no manual `VERSION` bump and no needless re-download on a
  no-op restart.
- **Mobile mode sheet**: selecting a mode expands the bottom sheet to full and
  scrolls the mode rail out of view, so the mode's options are immediately
  reachable; any "tap the map" action drops the sheet / switches to the chart.
- **README**: a Hardware section + mermaid overview diagram, pointing at the
  companion open-hardware carrier board (vanchor-pcb) as an easy optional build.

## [1.2-alpha] — 2026-07-02

UI rehaul for on-the-water reachability (design spec: `docs/ui-redesign.md`).

- **Command menu** replaces the cramped right-side settings drawer: the ☰
  button opens a centered modal with 8 large category tiles → big stacked
  sub-panels. Full-bleed sheet on phone, centered panel on landscape; ≥56 px
  touch targets. All existing card ids/handlers preserved.
- **Information architecture fixed**: the time-series "Charts" card (uPlot
  graphs) renamed "Time-series graphs" and moved out of "Map & charts" (a
  nautical-chart name collision) into "Data & system"; calibration + auto-tune
  consolidated under "Boat & tuning".
- **Specialised URL-addressable views** at `/view/<name>`, composed from the
  existing live widgets (chart-optional): `helm` (big mode grid + quick actions
  + dominant STOP), `instruments` (large glance HUD), `manual` (big thrust/
  steering), and `chart` (default full UI). Topbar + menu switchers; last view
  and per-view widget toggles persist via `/api/prefs`; offline via the SW.
- **Daylight theme**: an opt-in high-contrast palette (Appearance card,
  persisted, applied pre-paint) for direct sun; dark stays default. Raised
  secondary-text contrast.
- **Mobile mode sheet**: selecting a mode now slides the bottom sheet up and
  scrolls the mode rail out of view so the mode's options are immediately
  reachable (no manual drag). Any "tap the map" action (drop marker, add
  waypoints, go-to, orbit centre, work-area spots, GPS-adjust, offline-area,
  teleport) now auto-shows the chart view, closes the menu, and drops the
  sheet so the map is reachable.
- Mobile and landscape are both first-class throughout; STOP stays present and
  unmissable in every view and every sheet state.

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
