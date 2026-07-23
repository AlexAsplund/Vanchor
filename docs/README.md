# Vanchor-NG documentation

Working on the code as an AI/LLM? Start at **[`llms/`](llms/)** — a developer
guide curated per-subsystem, which takes precedence on implementation detail.

## Start here

- **[getting-started.md](getting-started.md)** — zero to a running sim, no coding.
- **[FEATURES.md](FEATURES.md)** — the full feature tour.
- **[architecture.md](architecture.md)** — the layers, the closed control loop,
  and the seam where real hardware plugs in.

## Modes & control

- **[modes/](modes/)** — a guide to every autopilot mode: what it does, when to
  use it, its on-screen controls.
- **[anchor-ml.md](anchor-ml.md)** — the three station-keepers (PID, Smart, Leif):
  how the learned models work and when to use each.
- **[pid-tuning.md](pid-tuning.md)** — every control loop and gain: units, shipped
  values, what raising/lowering does, mis-tune symptoms.

## Backend contracts

The interfaces the front end and integrations build against.

- **[ui-contract.md](ui-contract.md)** — WebSocket telemetry + commands.
- **[api-contract.md](api-contract.md)** — the runtime-discoverable `/api/contract`
  and how it can't silently drift.
- **[nav-control-api.md](nav-control-api.md)** — calibration, guided-mode control,
  routing/survey, pattern modes, safety/power.
- **[routing-weather-api.md](routing-weather-api.md)** — the "take me here" water
  router and the wind/current/gust model.
- **[safety-matrix.md](safety-matrix.md)** — failure modes × detecting layer ×
  behaviour × the test that proves it.
- **[connectors.md](connectors.md)** — the consent-gated connector framework
  (NMEA-TCP, N2K, rf-remote) and how to build one.

## Simulator & testing

- **[simulator.md](simulator.md)** — the physics models, sim-vs-real fidelity, and
  demo mode.
- **[analysis.md](analysis.md)** — the headless scenario runner + auto-tuner.
- **[testing-and-workflow.md](testing-and-workflow.md)** — tests, CI, and the
  debug-recording workflow.

## Hardware & deployment

- **[deploy-pi.md](deploy-pi.md)** — boat-ready Raspberry Pi deploy: SD image,
  supervisor, OTA updates, first-boot verification.
- **[setup-wizard.md](setup-wizard.md)** — identify + wire GPS / compass / motor
  in ~2 minutes.
- **[adding-a-device.md](adding-a-device.md)** — add a sensor or motor driver to
  the registry.
- **[custom-hardware.md](custom-hardware.md)** — split steering + thrust channels,
  off-centre mounts.
- **[firmware.md](firmware.md)** — the Pi ↔ Arduino software contract.
- **[ublox-m9n-fusion.md](ublox-m9n-fusion.md)** — GNSS/INS fusion (M9N + HWT901B).
- **[push-notifications.md](push-notifications.md)** — Web Push alarms to a locked
  phone.

## Project

- **[roadmap.md](roadmap.md)** — what's next (done work lives in the CHANGELOG).
- **[assumptions.md](assumptions.md)** — the deliberate simplifications taken.
- **[extension-packs.md](extension-packs.md)** — design notes for a future
  HACS-style pack system (not yet built).

## Reference

- **[api/](api/)** — the API reference, auto-generated from docstrings
  (`make docs`).
- **[llms/](llms/)** — the curated developer guide for LLMs (also linked from
  [`AGENTS.md`](../AGENTS.md)).
