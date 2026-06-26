# Vanchor-NG documentation

Human-friendly docs for Vanchor-NG. (Working on the code as an AI/LLM? See the
**[`docs/llms/`](llms/)** developer guide instead — it's curated per-subsystem
for LLMs and takes precedence on implementation detail.)

## Guides

- **[architecture.md](architecture.md)** — the design: layers, the closed control
  loop, why it's testable, and the seam where real hardware plugs in.
- **[FEATURES.md](FEATURES.md)** — the full feature tour (GUI, control modes,
  navigation, sensing, safety, simulation).
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
- **[roadmap.md](roadmap.md)** — what's implemented and what's planned next.
- **[assumptions.md](assumptions.md)** — the deliberate simplifications taken to
  reach a working baseline.

## AI / LLM developer guide

The **[`docs/llms/`](llms/)** directory is the curated developer guide for LLMs
working on the codebase — architecture, backend, simulation, frontend, the API
contract, and testing/workflow gotchas. Start at
[`docs/llms/README.md`](llms/README.md) (also linked from
[`AGENTS.md`](../AGENTS.md)).
