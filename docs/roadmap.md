# Roadmap

## Done (v1.0-alpha)

The entire original roadmap shipped: Fossen 3-DOF physics, the serial HAL,
NMEA-over-TCP, FollowAPB, GPX routes, the safety governor, observability,
typed config, the rewritten anchor hold, the analysis/tuning framework, and
all Tier 1–3 candidate features from the GPS trolling-motor research
(spot-lock jog, cruise control, track replay/BackTrack, drift mode,
chart-tap goto, contour follow) plus bonus modes (Work Area survey, learned
ML anchor). See git history and `docs/FEATURES.md` for the full inventory.

## Full-project review — 2026-07-01

A five-area review (core runtime/safety, nav/control, hardware/sim, UI/API,
tests/tooling) produced the phased roadmap below. Phases are ordered by
priority; each is shippable on its own. Items marked **(safety floor)**
relate directly to the non-negotiable invariants: motor deadman, isolation,
STOP always works.

### Phase 0 — Safety floor repairs

1. ✅ **(safety floor)** Wire `motor.start()/stop()` into Runtime
   start/stop/reload + a runtime-level serial-motor test.
2. ✅ **(safety floor)** Supervise the control loop: try/except per tick (zero
   motor + alarm on repeated failure), done-callbacks on all runtime tasks,
   `controller_fault` / `controller_tick_age_s` in telemetry with a UI red-banner.
3. ✅ **(safety floor)** Kill the boot-time `manual` command sent by slider
   binding; gate motor-engaging rail taps behind the existing per-panel Go
   buttons.
4. ✅ **(safety floor)** Manual deadman: treat manual-with-thrust as underway
   (link failsafe → stop), plus an app-level WS heartbeat.
5. ✅ **(safety floor)** Fix both through-zero reverse-interlock bypasses
   (governor + serial driver); stop resetting the governor on same-mode
   commands; seed slew anchors from the last applied command.
6. ✅ Fix the X9C digipot INC/CS sequencing in `engine.ino` (NVM wear-out on
   every throttle change).
7. ✅ **(safety floor)** Sensor staleness: monotonic timestamps on
   fixes/heading/depth/IMU; stale compass in guided mode → coast + alarm;
   stale depth → treated as unknown; `fix_failsafe_enabled: true` by default.
8. ✅ Sign-preserving cruise; fold ANCHOR_ML and Work-Area holds into the drag
   alarm; move `auto_rtl` planning to an executor.
9. ✅ Sanitize the debug-recorder session name (path-traversal write); Host
   validation (`_HostCheckMiddleware`, `VANCHOR_ALLOWED_HOSTS`) on all endpoints.
10. ✅ **Safety matrix doc** (`docs/safety-matrix.md`): 12 failure modes × detecting
    layer × behaviour × proving test; **chaos test suite** (`tests/test_chaos.py`,
    24 deterministic fault-injection tests).

### Phase 1 — Project infrastructure

11. ✅ GitHub Actions CI: pytest on Python 3.11/3.12, `node --check`, ruff,
    `pytest-timeout` (120 s).
12. (partial) LICENSE file; ✅ ruff `E9+F` baseline adopted; mypy on `core/` +
    `controller/` and pre-commit not yet.
13. `requirements.lock` for the Pi; cut the `1.0-alpha` tag; `__version__` +
    `/api/version`.
14. (partial) ✅ `docs/ui-contract.md` reconciled with code (~22 commands
    documented); ✅ stale doc counts fixed; `docs/deploy-pi.md` and
    CONTRIBUTING.md not yet.

### Phase 2 — Robustness & health

15. ✅ Supervised driver base class: exponential-backoff reconnect on EOF/error,
    `healthy` flag, `last_data_monotonic` pollable — implemented in
    `serial_link.py`; `motor.flush()` no longer raises while the link is down.
16. ✅ Dedicated ~1 Hz safety supervisor task (`_run_supervisor` in `app.py`):
    link-failsafe evaluation, RTL recommend, launch capture, trip update,
    depth-map checkpoint — exception-proof, immune to replay, independent of
    connected clients; `telemetry()` / `GET /api/state` are pure reads.
17. (partial) ✅ `health` telemetry block: per-sensor ages, `controller_fault`,
    `controller_tick_age_s`, staleness flags, per-device `healthy`/`data_age_s`;
    ✅ health UI banners (`health.js`); COG-derived heading fallback when
    compass-lost not yet implemented.
18. Firmware heartbeat round-trip (sequence number echoed in the `A`
    feedback line) so the Pi detects one-way serial failure; parse the
    currently ignored `E` lines.
19. (mostly done) ✅ Monotonic clocks everywhere (injectable `mono_fn`); ✅
    non-blocking telemetry broadcast with per-client bounded queues; ✅ depth-map
    saves and debug-recorder gzip moved off event loop (`asyncio.to_thread`); ✅
    `handle_command` hardened against malformed payloads.
20. Always-on low-rate black-box ring recording with pre-trigger dump on any
    alarm; record applied-vs-desired motor commands.

### Phase 3 — UI/API maturity ✅ (complete)

21. ✅ Versioned WS envelope (`{v, type, seq, ts}`) with server `{ack}`/`{nack}`;
    dual-path (WS+POST) STOP that confirms on the ack OR the next telemetry
    frame and shows a red banner if neither arrives in ~1.5 s.
22. ✅ Telemetry-age watchdog overlay ("DATA STALE") + Screen Wake Lock while a
    motor mode is active (`wakelock.js`; no-op without a secure context, i.e.
    plain-HTTP LAN — needs HTTPS on the Pi to actually hold the screen).
23. ✅ Server-persisted safety geometry (`safety.json`: no-go zones / min-depth /
    fix-failsafe, applied at Runtime init) + generic `/api/prefs` store; the
    browser adopts server geometry as truth with an echo guard.
24. ✅ Multi-client helm/observer roles, auto-promote on helm disconnect,
    cooperative `take_helm`; observer commands `role_denied` but STOP always
    works; no boot-time disruption. (Broadcast frame now serialized once.)
25. ✅ Playwright reconnect/STOP regression (opt-in `e2e` marker + `browser-e2e`
    CI job); repaired `uitest.py` (21/21, self-launching).
26. ✅ In-app command audit log (`/api/audit`, helm/observer/rest source) +
    offline-first command queue (queued/sent/confirmed/failed; STOP never
    queued; stale queued commands expire, never auto-replay).

### Phase 4 — Nav & control quality ✅ (27–35 complete)

27. ✅ Shared `WindCurrentEstimator` promoted to a persistent Controller service
    (fed every tick, never reset on mode change) → waypoint crab feedforward
    (mean |XTE| 10.7 m → 0.47 m on a beam set), spot-lock preloaded with the
    drift, Drift mode drift axis.
28. ✅ Drift mode on signed along-axis speed; dt-scaled estimator alphas.
29. ✅ Water-clip survey routes + concave-leg boundary routing (pragmatic vs full
    cell decomposition); clipped island ring; waypoint passed-the-perpendicular
    arrival check.
30. ✅ Depth-aware routing: `DepthMap.shallow_polygons()` (contours+soundings)
    hard-subtracted from navigable water with a soft-penalty band and a
    trap-safe fallback — proactive shoal avoidance, on by default.
31. ✅ Adaptive helm gain scheduling keyed on SOG (more gain when slow); per-boat
    saved gain profiles (`boat_gains.json`); tuner can persist tuned gains.
32. ✅ Ground-track trolling (bounded rolling corridor of virtual waypoints;
    ~constant swath under beam current instead of shearing).
33. ✅ Visibility-graph speedup: lazy A* + reflex-vertex filtering, ~8× fewer
    `covers()` tests (216k → 27k), routes provably identical to an eager oracle.
34. ✅ ML anchor v2: mount/steer-sign correctness, runtime residual-decay
    guardrail (never worse than the PID base), spot-lock quality metric
    (RMS error / % in radius) in telemetry, offline fine-tune script.
35. ✅ Vectored/azimuth station-keeping (opt-in): pushes against the set using
    the full rotation; beam-set RMS 3.29 m → 1.29 m; default off = baseline.

**Since 1.3-alpha (beyond 34/35):** the learned station-keeper was pushed well
past the roadmap. The default **Smart** mode is now a full-azimuth hybrid
(PID + learned residual, trained at a 120° swing, rescaled to the boat's range),
which strictly dominates PID and the old ±35° hybrid — 90.6% in-radius vs PID's
82.4% (≤6 m/s), 100% on both bow and stern, with the safety floor intact. A pure
experimental mode, **Leffe 🍺**, is also selectable. Full writeup + held-out
numbers + training recipe in [`docs/anchor-ml.md`](anchor-ml.md).

### Phase 5 — Simulation & testing depth

36. (partial) ✅ `SimMotorController` opt-in actuation shaping implemented
    (`reverse_delay_s`, `thrust_slew_per_s`, `thrust_lag_tau_s`, `step(dt)`);
    not yet wired to the config YAML or device-config API.
37. Fault injection as first-class sim knobs + API triggers (GPS
    dropout/glitch, compass freeze, serial EOF, NMEA garbage,
    baud-saturation latency) — wired into CI safety scenarios.
38. Sea-state model (1-DOF roll/pitch/heave driving the sim IMU) — exercises
    the AHRS/IMU pipeline, enables wave-aware station-keeping later.
39. Sim-based regression gates in CI: run key `vanchor.analysis` scenarios
    and fail on metric regressions vs committed baselines.
40. Nightly soak job (multi-hour sim, mode churn, link drops); NMEA
    property-based/fuzz tests (hypothesis); host-compiled test for
    `vanchorParseCmd`.

### Phase 6 — Hardware expansion & the pack framework

41. Interactive magnetometer calibration (the stubbed `calibrate_mag`
    rotate-through-360° hard/soft-iron fit); persist the learned compass
    offset.
42. Battery monitor driver (INA226/Victron shunt) as the first
    registry-driven non-compass device kind.
43. Registry-route all four device kinds (gps/depth/motor are hard-coded);
    versioned driver API with a narrow capability object instead of
    `runtime: Any`; entry-point discovery so packs are pip-installable —
    unblocks the paused HACS-style pack framework.
44. Hardware watchdog chain: Pi heartbeat GPIO → external relay on the motor
    supply (covers Pi-hard-hang, which the firmware watchdog doesn't).
45. Sonar/fishfinder ingest (NMEA2000 gateway / Deeper) merged with cmapper
    chart import — live bathymetry-vs-chart divergence alerts.
46. HIL pytest marker suite (bench-connected Arduino) + recorded-truth
    Fossen auto-calibration from real runs.
47. Heading semantics done right: honor M/T reference, central declination,
    emit HDT.

### Phase 7 — Field & community

48. Opt-in "upload last session on WiFi" — real-water incidents become
    replayable test scenarios.
49. Low-battery ladder (staged thrust derating before RTL fires).
50. Safety-floor config lockout: a config section that hot-reload / profiles
    / backup-restore can never weaken.
51. Frontend manifest/loader so `index.html` and `sw.js` shell lists can't
    drift (still no build step).
52. Community pack registry + docs, once Phase 6's driver API is versioned.
    Design sketch (packs, safety floor, registry, phasing) in
    [`docs/community-plan.md`](community-plan.md).

## Engineering debt (carried over, still open)

- Per-boat saved gain profiles; persist applied tuner gains back to config
  (→ item 31).
- The "Hold heading while anchored" UI checkbox is a passive no-op — remove
  or repurpose.
- No hardware-in-the-loop tests (→ item 46).
- Interactive magnetometer calibration is stubbed (→ item 41).
- Learned compass offset not persisted across restarts (→ item 41).
- COG/declination stubbed (magnetic == true) for non-HWT901B sources
  (→ item 47).
