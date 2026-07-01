# Vanchor-NG — LLM Developer Guide

> **You are an LLM about to modify this codebase. Read this file first.** It
> orients you in ~5 minutes and points you at the per-area guide you need.
>
> 🔁 **These docs are living. If you change code, you MUST update the matching
> `docs/llms/*` file in the same change.** See [Keeping these docs current](#-keeping-these-docs-current-mandatory).

## What this project is

Vanchor-NG is a **software-first GPS autopilot / anchor system for a cheap
trolling-motor boat**. A Raspberry Pi (or any machine, or pure simulation) runs
a Python `asyncio` backend that reads GPS/compass/depth (real NMEA or a built-in
physics simulator), runs guidance + control loops, and drives a motor (real
serial/Arduino or the simulator). A vanilla-JS + Leaflet web UI (also a PWA)
talks to it over a WebSocket + REST.

- Language/stack: **Python ≥ 3.11** (`asyncio` + FastAPI + uvicorn, numpy for
  the physics), **vanilla JS + Leaflet** front end (no build step, no framework).
- Entry point: the `vanchor` console script → `vanchor.app:main` (see
  `pyproject.toml`).
- It runs fully **headless in simulation** — you almost never need hardware.

## Run it

```bash
# one-time: editable install with dev + routing extras
pip install -e ".[dev,routing]"        # or use the repo's .venv

# run the server (simulation by default; serves the UI at http://localhost:8000)
vanchor --host 0.0.0.0 --port 8000 --nmea-tcp --log-level warning
```

Config is YAML (see `vanchor.example.yaml`); fields map to `core/config.py`
dataclasses. Runtime data (boat profiles, depth map, cached charts) lives in
`vanchor_data/` **relative to the process working directory** — see the cwd trap
in [testing-and-workflow.md](testing-and-workflow.md).

## Test it

```bash
. .venv/bin/activate && python -m pytest -q     # full suite, fast, no hardware
node --check src/vanchor/ui/static/<file>.js    # syntax-check any JS you touch
```

The closed-loop integration harness (`tests/harness.py`) wires the real
navigator + controller + simulator together with **no asyncio and no wall
clock**. Its sensor/control timing is load-bearing — see
[testing-and-workflow.md](testing-and-workflow.md) before writing control tests.

## Repository map

| Path | What lives there | Guide |
|------|------------------|-------|
| `src/vanchor/app.py` | `Runtime` (owns sim/nav/controller, the async loops) + `main()` | [backend.md](backend.md) |
| `src/vanchor/controller/` | `controller.py` (helm), `modes.py` (steering behaviours), `calibration.py`, `safety.py` | [backend.md](backend.md) |
| `src/vanchor/core/` | `config.py`, `state.py`, `models.py`, `geo.py`, `pid.py`, `boat_profiles.py`, events, debug recorder | [backend.md](backend.md) |
| `src/vanchor/nav/` | `navigator.py`, `routing.py`, `water.py`, `depth.py`, `survey.py`, `track.py`, `trip.py`, `nmea*.py`, `guard.py` | [backend.md](backend.md) |
| `src/vanchor/sim/` | `simulator.py`, `fossen.py` (3-DOF physics), `boat.py` (simple model), `devices.py` (sensors+noise), `bathymetry.py`, `battery.py`, `weather.py`, `gust.py` | [simulation.md](simulation.md) |
| `src/vanchor/hardware/` | real serial/NMEA devices + motor; `registry.py` + `drivers/` = pluggable device drivers | [device-drivers.md](device-drivers.md) |
| `src/vanchor/ui/server.py` | FastAPI app: REST endpoints + the `/ws` telemetry socket | [api.md](api.md) |
| `src/vanchor/ui/static/*.js` | the web UI (one module per feature; `map.js` + `app.js` are the hubs) | [frontend.md](frontend.md) |
| `src/vanchor/analysis/` | offline scenario runner + auto-tuner (text/CSV/plot reports) | [backend.md](backend.md) |
| `tests/` | pytest suite + `harness.py` | [testing-and-workflow.md](testing-and-workflow.md) |

## The guides

1. **[architecture.md](architecture.md)** — the big picture: data flow, the loops, the core invariants. Read this second.
2. **[backend.md](backend.md)** — developing the Python core: the runtime, control modes, calibration, navigation, config + boat profiles. How-to recipes.
3. **[simulation.md](simulation.md)** — the physics simulator + simulated sensors: Fossen model, boat parameters (`hull_tracking`, thruster geometry), sensor noise, presets, teleport.
4. **[frontend.md](frontend.md)** — the web UI: the `VA.*` global contract, `map.js` patterns (overlays, click consumers, waypoints), adding a feature, the PWA service worker.
5. **[api.md](api.md)** — the REST + WebSocket contract: endpoints, command types, telemetry shape.
6. **[testing-and-workflow.md](testing-and-workflow.md)** — running, testing (harness timing!), headless browser verification, and the operational gotchas that bite every time.

There are also older, narrower design docs in `docs/` (`architecture.md`,
`nav-control-api.md`, `ui-contract.md`, `routing-weather-api.md`,
`simulator-options.md`, `FEATURES.md`). They are reference material; the
`docs/llms/` set is the curated developer guide and takes precedence when they
disagree (fix the older doc if you spot the drift).

## Golden rules (read once, internalise)

1. **Simulate, don't theorise.** This codebase has a deterministic closed-loop
   harness. Before claiming a control/nav change works, *reproduce and measure*
   it (see [testing-and-workflow.md](testing-and-workflow.md) → "Reproduce with
   the harness"). The recurring lesson this project keeps re-learning: an
   oscillation/“it won’t turn” bug is usually a wrong sign, a mistuned gain, or
   an unrealistic sim parameter — find the root cause, don’t paper over it.
2. **Keep the default a no-op.** When you add a tunable (a boat parameter, a
   gain, a noise level), pick the default so existing behaviour is byte-identical
   and the whole suite still passes. New knobs earn their keep at non-default
   values.
3. **Respect file ownership when working in parallel.** If you fan work out to
   sub-agents, give each disjoint files; `map.js`, `index.html`, `style.css`,
   `app.py` are shared hubs — never have two agents edit one concurrently.
4. **The front end has no build step.** Edit the `.js`/`.css`/`.html` directly;
   `node --check` is your only compile gate. The service worker is **network-first**
   (see [frontend.md](frontend.md)) — bump its cache version when you change the
   shell.
5. **🔁 Update these docs in the same change.** Non-negotiable. See below.

## 🔁 Keeping these docs current (mandatory)

`docs/llms/` is the map other LLMs navigate by. A stale map is worse than none.

**Whenever you add, remove, rename, or change behaviour of code, update the
matching `docs/llms/*` file in the SAME change — before you consider the task
done.** Concretely:

- Added/renamed a module, control mode, REST endpoint, command type, boat
  parameter, telemetry field, `VA.*` API, or JS feature module? → update the
  relevant guide's lists/recipes and, if needed, this README's repo map.
- Changed an invariant (loop rates, the harness timing, a default value, the
  service-worker strategy, the cwd/data-dir behaviour)? → update the doc that
  states it.
- Learned a non-obvious gotcha while debugging? → add it to the relevant guide
  so the next LLM doesn't rediscover it.

Keep edits tight and high-signal. If a guide section no longer matches the code,
fix the doc; don't leave both. When in doubt, over-document invariants and
gotchas, under-document things that are obvious from the code.
