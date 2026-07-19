# Vanchor-NG documentation

Human-friendly docs for Vanchor-NG. (Working on the code as an AI/LLM? See the
**[`docs/llms/`](llms/)** developer guide instead — it's curated per-subsystem
for LLMs and takes precedence on implementation detail.)

## Guides

- **[architecture.md](architecture.md)** — the design: layers, the closed control
  loop, why it's testable, and the seam where real hardware plugs in.
- **[FEATURES.md](FEATURES.md)** — the full feature tour (GUI, control modes,
  navigation, sensing, safety, simulation).
- **[modes/](modes/)** — a user guide for **every autopilot mode** (manual,
  heading-hold, anchor hold, route + smart routing, Work Area, drift, and the
  fishing patterns): what each does, when to use it, and its on-screen controls.
- **[nav-control-api.md](nav-control-api.md)** — control & navigation backend
  contract: modes, calibration, cross-track, drift feed-forward, GPS offset.
- **[routing-weather-api.md](routing-weather-api.md)** — the smart "take me here"
  water router and the variable-weather (wind/current/gust) model.
- **[ui-contract.md](ui-contract.md)** — the WebSocket telemetry + REST contract
  the front end builds against.
- **[simulator-options.md](simulator-options.md)** — the physics-simulator design
  and the reuse-vs-build investigation behind it.
- **[firmware.md](firmware.md)** — how the Arduino firmware in `firmware/` plugs
  into the Python app (the Pi ↔ Arduino software contract).
- **[analysis.md](analysis.md)** — the headless, deterministic scenario runner +
  auto-tuner for measuring control changes.
- **[pid-tuning.md](pid-tuning.md)** — every control loop and gain (helm,
  anchor, cruise, drift, XTE, firmware steering head): units, shipped values,
  what raising/lowering each term does, mis-tune symptoms, interactions.
- **[push-notifications.md](push-notifications.md)** — Web Push alarms to the
  phone with the app closed: setup, platform constraints, config reference.
- **[anchor-ml.md](anchor-ml.md)** — the three station-keepers (PID anchor_hold, Smart anchor_ml, Leif anchor_leif): how the learned models work and when to use each.
- **[ublox-m9n-fusion.md](ublox-m9n-fusion.md)** — GNSS/INS fusion path (u-blox M9N + HWT901B IMU): setup, calibration, and the `fusion` telemetry fields.
- **[setup-wizard.md](setup-wizard.md)** — hardware setup wizard: identify + wire GPS / compass / motor in ~2 minutes.
- **[adding-a-device.md](adding-a-device.md)** — how to add a new sensor or motor driver to the device registry.
- **[custom-hardware.md](custom-hardware.md)** — independent steering + thrust channels (split-motor configs, off-centre mounts).
- **[connectors.md](connectors.md)** — the consent-gated connector framework: NMEA-TCP, external integrations, building new connectors.
- **[deploy-pi.md](deploy-pi.md)** — boat-ready Raspberry Pi deployment (SD image, systemd, supervisor, OTA updates).
- **[image-testing.md](image-testing.md)** — first-flash bench-test checklist for a new SD-card image (hardware-verified items).
- **[sim-vs-real.md](sim-vs-real.md)** — systematic fidelity audit of the simulator vs real hardware (2026-07-15).
- **[testing-and-workflow.md](testing-and-workflow.md)** — unit/integration test guide, CI workflow, debug-recording workflow.
- **[ui-redesign.md](ui-redesign.md)** — UX rehaul notes: two-row peek, alarm strips, mode names, sim pill, view routing.
- **[roadmap.md](roadmap.md)** — what's implemented and what's planned next.
- **[safety-matrix.md](safety-matrix.md)** — 13 failure modes × detecting layer ×
  boat behaviour × the test that proves it; companion to `tests/test_chaos.py`.
  Also covers the demo/readonly posture.
- **[assumptions.md](assumptions.md)** — the deliberate simplifications taken to
  reach a working baseline.
- **[community-plan.md](community-plan.md)** — design notes for a future HACS-style extension-pack system (not yet implemented).

## Design documents

Internal design notes and reviews, not yet linked from FEATURES.md:

- **[design/pack-framework.md](design/pack-framework.md)** — extension-pack framework draft.
- **[design/ux-review-2026-07.md](design/ux-review-2026-07.md)** — UX expert review synthesis (July 2026).
- **[design/ux-revamp-concepts-2026-07.md](design/ux-revamp-concepts-2026-07.md)** — UX concept round (16 rendered screens).
- **[design/prior-art-lessons-2026-07.md](design/prior-art-lessons-2026-07.md)** — ArduPilot / pypilot prior-art lessons.

## AI / LLM developer guide

The **[`docs/llms/`](llms/)** directory is the curated developer guide for LLMs
working on the codebase — architecture, backend, simulation, frontend, the API
contract, and testing/workflow gotchas. Start at
[`docs/llms/README.md`](llms/README.md) (also linked from
[`AGENTS.md`](../AGENTS.md)).
