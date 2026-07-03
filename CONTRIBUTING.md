# Contributing to Vanchor-NG

Thanks for helping build Vanchor-NG — a software-first GPS anchoring / autopilot
for cheap trolling motors. This guide covers dev setup, the test/CI gates your
change must pass, the **non-negotiable safety floor**, and where things live.

Before touching code, skim [`AGENTS.md`](AGENTS.md) and the LLM-oriented
developer guide in [`docs/llms/`](docs/llms/README.md) — they orient you in a few
minutes.

---

## 1. Dev setup

Requires **Python ≥ 3.11**. Everything runs in **full simulation** — no
hardware needed to develop or test.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev,routing]"
```

The `dev` extra pulls in pytest, pytest-asyncio, httpx, and hypothesis; `routing`
adds shapely/networkx/pyproj/requests for the water-routing feature. Other
optional extras: `serial` (real hardware), `analysis` (matplotlib plots),
`hwt901b` (compass driver), `docs` (API-reference generation).

Run the server from the **repo root** (the `vanchor_data/` directory is resolved
relative to the process cwd):

```bash
vanchor --host 0.0.0.0 --port 8000     # then open http://localhost:8000
```

> The Raspberry Pi deployment uses the pinned [`requirements.lock`](requirements.lock)
> for reproducible installs; see [`docs/deploy-pi.md`](docs/deploy-pi.md).
> For local development install the editable package with extras as above.

---

## 2. Tests & quality gates

**All of these must be green before you open a PR.** They mirror the CI jobs in
[`.github/workflows/`](.github/workflows/).

### Unit + integration tests (`ci.yml` → `test`)

```bash
python -m pytest -q
```

The suite (~1100+ tests) wires the real navigator + controller + simulator in
lockstep via `tests/harness.py` — no asyncio, no wall-clock, seeded sensor noise
— so tests are fast and deterministic. **Simulate, don't theorise:** reproduce
and measure any control/nav change in the harness before claiming it works. Two
HIL tests skip without hardware; that is expected.

Write **real tests and run them** for every change, and keep **new defaults a
no-op** so the existing suite stays green.

### Lint — ruff + JS syntax (`ci.yml` → `lint`, `js-syntax`)

```bash
ruff check src tests
for f in src/vanchor/ui/static/*.js; do node --check "$f"; done
# or simply:  make lint
```

Ruff must be clean (config in `pyproject.toml`: `E9` + pyflakes `F`, line length
100). The front end has **no build step** — `node --check` is the only JS gate.
If you add or change a shell asset, bump the service-worker version and its
`SHELL` list (it is **network-first**); `scripts/check_shell_manifest.py` guards
this.

### Browser end-to-end (`ci.yml` → `browser-e2e`)

```bash
playwright install --with-deps chromium   # one-time
python e2e_smoke.py                        # self-contained 18-check smoke
pytest -m e2e tests/test_e2e_playwright.py -q
```

`e2e_smoke.py` starts its own isolated sim server and checks the API + live
control loop + UI. The Playwright regression tests assert **STOP integrity**
(engage a mode → STOP → motor stopped, no "STOP NOT CONFIRMED" banner) and
reconnect behaviour. E2E tests are opt-in (`-m e2e`) and skip gracefully if
Chromium/Playwright is absent.

### Sim regression gate (`regression.yml`)

```bash
python scripts/regression_check.py --verbose
```

Runs fixed analysis scenarios and fails if control-quality metrics (settling
time, overshoot, station-keeping RMS, cross-track, …) drift outside tolerance
against the committed baseline
(`src/vanchor/analysis/baselines/regression.json`). If an **intended**
control/sim change moves the numbers, regenerate and commit the baseline:

```bash
python scripts/regression_check.py --update
```

### Also in CI

- **Fuzz** (`fuzz.yml`) — Hypothesis property tests of the NMEA parser
  (`tests/test_nmea_fuzz.py`) + a host-compiled test of the shared firmware
  command parser.
- **Soak** (`soak.yml`) — nightly headless sim soak (`scripts/soak.py`):
  continuous mode churn + injected link drops, asserting no crash, no stuck
  motor, bounded memory.

There is currently **no pre-commit hook**; run the gates above manually (or
`make test` / `make lint`) before pushing.

---

## 3. The safety floor — non-negotiable

Vanchor-NG steers a real motor on real water. Some invariants **must never be
weakened**, softened, or bypassed — not "temporarily", not behind a flag:

- **STOP always works** — from any client, in any mode, regardless of role
  (helm or observer). It is not access-controlled.
- **The deadman / watchdog** — the firmware ramps the motor to neutral if
  Vanchor-NG stops talking (800 ms), so STOP survives a crash or USB unplug. The
  Pi-side safety governor (`controller/safety.py`) mirrors it: thrust slew
  limiting, reverse dead-time, loss-of-fix failsafe.
- **Failsafes** — loss-of-fix coast, link-loss hold, shallow-water / geofence
  auto-stop, feedback-loss steering hold. Do not remove or defeat them.

If a change touches control, safety, modes, or the motor path, add or extend a
**fault-injection test** in `tests/test_chaos.py` (see
[`docs/safety-matrix.md`](docs/safety-matrix.md) for the failure-mode × layer ×
test matrix). A PR that could weaken any of the above will not be accepted.

---

## 4. Keep the docs current

When you add, remove, rename, or change the behaviour of code, **update the
matching [`docs/llms/*`](docs/llms/) file in the same change** — a stale guide
misleads the next contributor (human or AI). Log notable control/ML experiments
and shipped changes in [`CHANGELOG.md`](CHANGELOG.md). The Markdown API reference
under `docs/api/` is generated from docstrings with `make docs`.

---

## 5. Commit & PR conventions

- **Branch** off `main` for your work; keep the default branch clean.
- **Commits:** short imperative summary line (e.g. `Surface IMU from the AHRS
  into state + telemetry`), with a body explaining *why* when it isn't obvious.
  Group related edits; keep unrelated changes in separate commits.
- **Scope your PR** and describe: what changed, why, and how you verified it
  (which gates you ran). Reference the roadmap item (`docs/roadmap.md`) or issue
  where relevant.
- **Before opening a PR**, confirm: `pytest -q` green, `ruff check src tests`
  clean, `node --check` on any JS you touched, `e2e_smoke.py` passing, and the
  regression gate green (or an intended-baseline update committed).

---

## 6. Where things live

```
src/vanchor/
  app.py         config-driven Runtime wiring + CLI entrypoint (vanchor)
  core/          events, models, geo, pid, state, config, boat profiles, backup
  nav/           nmea, navigator, routing/water, depth, survey, track, trip
  sim/           fossen (3-DOF) + simple physics, devices, bathymetry, weather, battery
  hardware/      real serial / NMEA devices + motor drivers (mirror the sim devices)
  controller/    controller (+ Helm), modes, calibration, safety governor
  ui/            server.py (FastAPI WS + REST) + static/ (Leaflet PWA, no build step)
  analysis/      headless scenario runner + auto-tuner + regression baselines
  obs/           observability (debug recorder, session upload)

tests/           pytest suite + deterministic harness (harness.py) + chaos suite
firmware/        Arduino sketches (engine/steering) + shared protocol header
scripts/         regression_check.py, soak.py, gen_api_docs.py, shell-manifest check
docs/            human docs; docs/llms/ AI developer guide; docs/deploy-pi.md
```

Start reading at [`docs/llms/README.md`](docs/llms/README.md) (architecture,
backend, simulation, frontend, API, testing).

---

## License

Vanchor-NG is **MIT** licensed (see [`LICENSE`](LICENSE)). By contributing you
agree your contributions are licensed under the same terms. It is a clean-room
rewrite — do not copy code from the original Vanchor or other incompatibly
licensed sources.
