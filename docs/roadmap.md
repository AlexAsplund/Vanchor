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

1. **(safety floor)** Wire `motor.start()/stop()` into Runtime
   start/stop/reload + a runtime-level serial-motor test — today the serial
   motor transport is never opened and real-hardware motor mode is dead on
   arrival.
2. **(safety floor)** Supervise the control loop: try/except per tick (zero
   motor + alarm on repeated failure), done-callbacks on all runtime tasks,
   `controller_heartbeat_age_s` in telemetry with a UI red-banner.
3. **(safety floor)** Kill the boot-time `manual` command sent by slider
   binding; gate motor-engaging rail taps behind the existing per-panel Go
   buttons.
4. **(safety floor)** Manual deadman: treat manual-with-thrust as underway
   (link failsafe → stop), plus an app-level WS heartbeat.
5. **(safety floor)** Fix both through-zero reverse-interlock bypasses
   (governor + serial driver); stop resetting the governor on same-mode
   commands; seed slew anchors from the last applied command.
6. Fix the X9C digipot INC/CS sequencing in `engine.ino` (NVM wear-out on
   every throttle change).
7. **(safety floor)** Sensor staleness: timestamps on fixes/heading/depth/IMU;
   stale sensor → forced idle + alarm; ship `fix_failsafe_enabled: true`.
8. Sign-preserving cruise; fold ANCHOR_ML and Work-Area holds into the drag
   alarm; move `auto_rtl` planning to an executor (it currently blocks the
   event loop on a 60 s network fetch).
9. Sanitize the debug-recorder session name (path-traversal write); Host
   validation + optional PIN on control/restart/restore endpoints.
10. **Safety matrix doc**: failure mode (Pi crash / GPS loss / link loss /
    serial loss / UI crash) × which layer cuts the motor × the test that
    proves it — then encode it as a chaos-test suite (serial EOF mid-run,
    sensor silence, mode exception, clock step ⇒ assert boat ends motionless
    with an alarm).

### Phase 1 — Project infrastructure

11. GitHub Actions CI: pytest + `e2e_smoke.py` + `node --check`, Python
    3.11/3.12, with `pytest-timeout`.
12. LICENSE file; adopt ruff (lint+format) and mypy on `core/` +
    `controller/`; pre-commit.
13. `requirements.lock` for the Pi; cut the `1.0-alpha` tag; `__version__` +
    `/api/version`.
14. `docs/deploy-pi.md` (systemd unit, install script, health-checked update
    with rollback); CONTRIBUTING.md; fix stale doc counts; reconcile
    `docs/ui-contract.md` with code and add a schema-drift test.

### Phase 2 — Robustness & health

15. Supervised driver base class: backoff reconnect on EOF/error, `healthy`
    flag, `last_data_monotonic` — enforced for all drivers.
16. A dedicated 1 Hz safety supervisor task (link failsafe, RTL recommend,
    sensor ages, task liveness) — out of `telemetry()`, immune to replay,
    independent of clients; make `GET /api/state` a pure read.
17. `health` telemetry block + degraded modes: GPS-lost → coast + alarm;
    compass-lost → COG-derived heading fallback when making way.
18. Firmware heartbeat round-trip (sequence number echoed in the `A`
    feedback line) so the Pi detects one-way serial failure; parse the
    currently ignored `E` lines.
19. Measured `dt` + monotonic clocks everywhere; non-blocking telemetry
    broadcast; move depth-map/debug-recorder writes off the control thread;
    harden `handle_command` parsing.
20. Always-on low-rate black-box ring recording with pre-trigger dump on any
    alarm; record applied-vs-desired motor commands.

### Phase 3 — UI/API maturity

21. Versioned WS envelope (`{v, type, seq, ts}`) with server acks; dual-path
    (WS+POST) STOP that verifies the next telemetry frame and escalates
    visually if unconfirmed within ~1 s.
22. Telemetry-age watchdog overlay ("data N s old"); Screen Wake Lock while
    a motor mode is active.
23. Server-persisted safety geometry (no-go zones, min-depth) and UI prefs —
    the browser as cache, not the source of truth.
24. Multi-client model: helm vs observer roles, "another helm is connected",
    no boot-time disruption.
25. Playwright reconnect/STOP regression in CI; repair `uitest.py`.
26. Command audit log surfaced in-app; offline-first command queue with
    queued/sent/confirmed states.

### Phase 4 — Nav & control quality

27. Shared wind/current estimator as a persistent service (promoted out of
    AnchorHoldMode, fed continuously incl. IMU) → crab-angle feedforward on
    waypoint legs, a real drift axis for Drift mode, spot-lock that engages
    already knowing the environment.
28. Drift mode on signed along-axis speed; dt-scaled estimator alphas.
29. Water-clip survey routes + concave-cell decomposition; use the clipped
    island ring; waypoint passed-the-perpendicular arrival check.
30. Depth-aware routing: cost the visibility graph with the depth grid /
    imported contours — proactive shallow avoidance instead of reactive stop.
31. Adaptive helm gain scheduling keyed on SOG; per-boat saved gain
    profiles.
32. Ground-track trolling (S-curve as a corridor of virtual waypoints, fixed
    swath under current).
33. Visibility-graph speedup (tangent-vertex filtering or lazy A*) for
    Pi-class planning.
34. ML anchor v2: stern-mount/steer-sign training variants, runtime
    residual-decay guardrail when underperforming the PID base, offline
    fine-tuning from recorded real-water sessions; spot-lock quality metric
    (RMS error, % in radius) in telemetry.
35. Vectored/azimuth station-keeping exploiting the motor's full rotation.

### Phase 5 — Simulation & testing depth

36. Sim actuation parity: reverse dead-time, soft-start, first-order prop
    lag in `SimMotorController`.
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
