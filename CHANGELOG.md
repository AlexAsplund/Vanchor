# Changelog

All notable changes to Vanchor-NG. Dates are ISO-8601.

## Unreleased

- **Fix: sim boat wouldn't move (100% thrust into a NullMotor)** — the Devices
  panel re-submits every field on save, so switching any source (e.g. GPS back
  to Auto) also persisted the Advanced split-channel selects as
  steering/thrust = none/none, which the link planner took as "motor
  disconnected" and built a NullMotor over the live sim motor. Channel
  none+none now only means motor-off when `motor_source` is ALSO none;
  otherwise it means "no split configured" and the combined motor builds as
  selected. Regression-tested both ways.

- **New logo + icon set**: an anchor whose crown is a GPS reticle ring, in the
  app's cyan/off-white on dark navy. Hand-authored SVGs (`icons/logo.svg`,
  `icons/logo-wordmark.svg`, `favicon.svg`) with all PWA PNGs regenerated from
  them; the topbar brand mark and README header now use the real mark instead
  of the placeholder colored square.

- **Phone GPS: server-side fix reissue** — first field RUM recording showed the
  real cause of "regularly loses GPS fix": phone browsers deliver
  `watchPosition` only on change and coalesce timers, so the stream is sparse
  (~6 s) and bursty, tripping the 5 s loss-of-fix failsafe. The PhoneGps device
  now re-publishes the last fix at 1 Hz while the stream is quiet, hard-capped
  at 15 s (past that the silence is real and the failsafe fires) and only while
  the feeder is still connected. End-to-end test pins the recorded field
  cadence against the failsafe.

- **Client RUM into the Debug recorder**: the UI now streams browser-side
  telemetry to the boat (`POST /api/client-log`, batched, bounded) — JS errors
  + unhandled rejections with stacks, WS open/close/error, tab visibility
  changes, wake-lock transitions, phone-sensor feeder changes, geolocation
  errors and fix gaps, plus a per-page-load hello (UA/secure-context/viewport).
  Entries land in the `vanchor.client` logger (visible in the Debug panel) and
  as a structured `client` stream inside active debug recordings — so a phone
  problem in the field is troubleshootable from the same recording as the boat
  data. Other modules can add breadcrumbs via `VA.rum(event, msg, level)`.
- **Phone GPS fix-loss hardening**: browsers only fire `watchPosition` on
  change, so a stationary phone starved the boat into "fix lost" — the client
  now re-sends the last fix every 2 s (up to 15 s, marked `cached`), and the
  phone device's stale window widened 5 s → 10 s to absorb uneven browser
  cadence. The remaining loss cases (tab hidden, screen lock) now show up
  explicitly in RUM as `visibility`/`geo_gap` breadcrumbs.

## [1.5.0a3] — 2026-07-07

- **Phone-as-sensor devices**: select source **Phone (this device)** for GPS
  and/or compass, and a connected phone streams its browser geolocation (with
  per-fix accuracy riding into the fusion) and magnetic compass heading to the
  boat over the existing WebSocket ("Share this phone's GPS & compass" in
  Settings → Devices). **Disclaimer: crude, varies wildly between devices —
  experimentation, not navigation**; GPS sharing needs the https:// address.
  Strict single-feeder arbitration: one client feeds each sensor kind; another
  takes over automatically ONLY when the feeder disconnects — taking the helm
  never reassigns the feeder.

- **HTTPS listener** on a second port (default **8443**, `server.https_port`, 0
  disables): the same app served over TLS so secure-context browser APIs work on
  the boat — the real Screen Wake Lock API and full PWA/service-worker installs.
  Bring your own cert (`server.ssl_certfile`/`ssl_keyfile`) or a self-signed one
  (CN=vanchor.local, SANs vanchor.local/localhost/127.0.0.1) is auto-generated
  once into `<data_dir>/tls/` and reused, so a device trusts it once. Best-effort:
  busy port or no openssl -> warning, plain HTTP unaffected. mDNS TXT advertises
  `https_port` when active.

- **Independent Steering + Thrust channels** (`docs/custom-hardware.md`): the motor
  device is split into two logical channels that can be sourced, configured
  (own port/baud/framing), health-checked and debugged independently — for custom
  rigs like a modified trolling-motor head with its own steering servo + separate
  thrust ESC. Channels resolving to one physical link (the default single-MCU rig)
  build the exact combined controller as before; STOP zeroes both channels through
  the single control seam in every topology; a failing channel never takes down
  the other and gates only the modes that need it, by name. Configured in
  Settings → Devices → Motor → "Advanced: split channels".
- **Depth in the map long-press menu**: press-and-hold the chart and the menu
  shows the best-known depth at that point (nearest sounding within ~100 m, else
  the nearest imported contour) above "Place marker here".


- **Connector framework** (`src/vanchor/connectors/`, `docs/connectors.md`): pluggable
  external integrations under a permission-manifest trust model — default-deny
  allowlists, plain-language **user consent** in Settings → Devices → Connectors
  (re-consent on any manifest change), control-as-capability routed through the same
  governed command path as the app (STOP always flows, granted or not). Ships four
  connectors: **nmea-tcp** (the retrofitted bridge), **metrics** (offline-first
  store-and-forward telemetry export for a boat without internet), **nmea2000**
  (PGN codec + CAN seam; position/COG-SOG/heading/depth ingress, position egress —
  bench-verify flagged), and **rf-remote** (the control-grant reference: governed
  thrust/steer with an active-driver deadman that neutralizes via STOP and never
  disturbs an autonomous anchor hold on radio loss). Built subagent-driven with
  per-task adversarial reviews + a whole-branch safety review.
  The **nmea2000** connector also speaks the N2K **thruster** family (PGN 128006/128008):
  it always broadcasts the motor's own thruster status, and — opt-in via the
  `thruster_control` setting, which changes the manifest hash and forces re-consent —
  accepts 128006 thruster commands through the governed path with an rf-remote-style
  Command-Timeout deadman, an OFF→thrust-0 mapping, and a self-frame loopback guard.
  **Connector settings** (Task 8): every connector exposes a typed settings schema
  editable live via `POST /api/connectors/{name}/settings` and a ⚙ Settings inline
  form in the Connectors card. Saves are live-applied (stop → rebuild → start); secrets
  masked as `"•••"` in responses (stored plain-text); the thruster-control
  manifest-change exception surfaces `needs_reconsent` and re-arms the consent flow.
  The nmea-tcp boot host/port re-sync is suppressed after a user-explicit settings save
  (`user_edited` guard).

## [1.5.0a2] — 2026-07-05

- **Per-device Debug view** (Settings → Devices). Every device class now has a
  `debug()` that reports its most recent RAW data in human-readable form, surfaced
  by a 🐞 Debug button per device that live-streams it (polls every 0.5 s). The
  u-blox view shows `fix_type` vs `gnssFixOK`, the NED velocity vector and the
  accuracy estimates, so a marginal/no-fix state is diagnosable at a glance.
- **Fix:** `test_device_gating` no longer persists to the repo's
  `vanchor_data/devices.json` (its `set_device_config` calls could clobber a live
  device config).

## [1.5.0a1] — 2026-07-05

- **"Boat shown on map" setting** (Settings → Simulator). Choose which boat the
  map draws when a real GPS *and* the simulator are both active: **Auto** (the
  default — shows the real GPS whenever the GPS source is real hardware, else the
  sim boat), **Simulator**, or **Real GPS**. Fixes a real GPS + sim-actuation
  setup showing the boat at the sim start instead of your actual position.

## [1.5.0a0] — 2026-07-05

- **GNSS/INS sensor fusion (u-blox M9N UBX + HWT901B IMU).** A new UBX GPS driver
  (`gps_source: ublox`) surfaces the NED ground-velocity vector + per-fix accuracy
  that NMEA can't, and a loosely-coupled complementary filter fuses it with the
  IMU into a smooth heading, real yaw rate, clean low-speed velocity, crab/leeway,
  and dead-reckoning through GPS gaps. Fully **additive/non-blocking** — it fills
  new `state.fusion` telemetry only; heading/position/control are unchanged, so
  every existing hardware combo behaves exactly as before.
- **Capability-driven activation.** The richer path keys off what a `GpsFix`
  carries (`has_velocity`/`has_3d_velocity`/`has_accuracy`), not which driver made
  it — any velocity source (UBX, a future GNSS, a bridge, or the sim via
  `sensors.gps_velocity`) lights up the same behaviour.
- **Sensor-calibration wizard** (separate from boat setup): guided *still* (gyro
  bias + noise → tuned fusion gains), *align* (drive-straight → compass/IMU
  mounting offset) and *interference* (thrust×steer sweep) captures, plus a
  "Calibrate all" sequence. Interference reports a **0–100 quality score**,
  escalating mitigation **recommendations**, and an **experimental** software
  remedy that compensates the heading for both motor thrust **and** servo angle.
- **UBX config bench-verified** on a real M9N; configures both UART1 and USB so it
  works however the receiver is wired. See `docs/ublox-m9n-fusion.md`.

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
- **"Leif"** — an experimental *pure* full-azimuth learned anchor mode (no
  PID base) selectable from the Anchor panel (with an info-icon tooltip). It
  holds a stern mount exceptionally tight (~98% in-radius) but runs the motor
  hot and has no PID fallback — a fun research mode. The boat mode badge now
  distinguishes the keeper (Anchor / Anchor · Smart / Anchor · Leif).
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
  10.7 m → 0.47 m on a beam set), anchor hold preload, and the Drift-mode axis.
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
  guardrail (never worse than the PID base), a hold quality metric
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
1–3 GPS trolling-motor feature set (anchor jog, cruise, track replay, drift,
chart-tap goto, contour follow) plus Work Area survey and a learned ML anchor.
Subsequently hardened by a full-project review (supervised control loop, motor
lifecycle, sensor staleness, link/fix failsafes, CI, columnar depth-chart store
that cut RSS from ~1.8 GB to ~180 MB and fits a 512 MB device). See `RELEASE.md`.
