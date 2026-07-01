# Testing & workflow

> Part of the `docs/llms/` developer guide. 🔁 **If you change how the project is
> run/tested, the harness timing, or discover a new operational gotcha, update
> this file.** This page is mostly hard-won gotchas — keep it current.

## CI (GitHub Actions)

`.github/workflows/ci.yml` runs on every push and pull request:

- **test** job: `python -m pytest -q` on Python 3.11 and 3.12 (matrix,
  `fail-fast: false`). `pytest-timeout` enforces a **120 s per-test** timeout
  (configured in `pyproject.toml`).
- **lint** job: `ruff check src tests` — baseline `E9` (syntax errors) + `F`
  (pyflakes); `F401`/`F841` suppressed.
- **js-syntax** job: `node --check` on every `static/*.js`.

Run locally with `make lint` (runs both ruff and node --check).

## Running locally

```bash
. .venv/bin/activate                    # use absolute path if cwd may differ
vanchor --host 0.0.0.0 --port 8000 --nmea-tcp --log-level warning
```

Serves the UI at `http://localhost:8000` and a telemetry socket at `/ws`.
Defaults to simulation. CLI flags + YAML map to `core/config.py`.

## Testing

```bash
python -m pytest -q                          # full suite (fast, hardware-free)
python -m pytest tests/test_modes.py -q       # one file
node --check src/vanchor/ui/static/foo.js     # JS syntax gate (no test runner for JS)
```

### The closed-loop harness (`tests/harness.py`)

Wires the **real** navigator + controller + simulator with no asyncio and no
wall clock. Use it for any control/nav assertion.

```python
from harness import Harness, STOCKHOLM
from vanchor.core.models import Environment
h = Harness(model="fossen", environment=Environment())     # calm by default
h.command({"type": "goto", "waypoints": [{"lat": .., "lon": ..}]})
h.run(60)                                                   # 60 s of sim time
```

⚠️ **Timing is load-bearing.** The harness schedules physics ~20 Hz, control
~5 Hz, GPS ~1 Hz, compass ~5 Hz. **Never call `control_tick` every physics
step** — that runs control 4× too fast and manufactures oscillation that doesn't
exist in the real system. Reproductions that don't use the harness loop are
suspect.

### Reproduce-and-measure (the project's debugging culture)

Before changing a control/nav law, *reproduce the symptom in the harness and
measure it* (e.g. count cross-track zero-crossings, peak heading error, turn
rate). Then change one thing and re-measure. Real example: "boat oscillates
following waypoints" was **not** a control bug — isolating it (run with
`h.gps.position_noise_m = 0`) showed a clean fix tracks perfectly, so the root
cause was an unrealistic sim GPS-noise default. Fix root causes, not symptoms.

### Headless browser verification (Playwright)

`node --check` only catches syntax. For UI behaviour/layout, drive a real
browser against the running server:

```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"]); pg = b.new_page()
    errs=[]; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type=="error" else None)
    pg.goto("http://127.0.0.1:8000/", wait_until="networkidle"); pg.wait_for_timeout(1500)
    # assert elements exist, fire interactions, screenshot, assert errs == []
```

Tips: tiles don't load offline (the map background is dark) — to test contrast,
paint a light background in JS; you can fire Leaflet events
(`VA.map.leaflet.fire('click',{latlng})`) but for click-pipeline bugs use **real**
`pg.mouse`/`pg.touchscreen` (touch context for mobile) — synthetic events can
mask real failures.

## Chaos / fault-injection suite (`tests/test_chaos.py`)

`tests/test_chaos.py` contains **24 deterministic fault-injection tests** that
prove the safety floor holds under failure. Coverage includes: tick exception
(supervised loop zeroes motor + continues), compass silence (guided coast),
link-loss while in guided and manual modes (respective failsafes), serial EOF
(reconnect/no-throw), through-zero reversal, NTP clock step, and STOP from all
11 major modes. See `docs/safety-matrix.md` for the 12-failure-mode×layer×test
matrix this suite encodes. (Source: `tests/test_chaos.py` line 1 docstring.)

## `tests/conftest.py` — Host-header allowlist

`tests/conftest.py` sets `os.environ.setdefault("VANCHOR_ALLOWED_HOSTS",
"testserver")` at import time. This allows the FastAPI test client (whose default
host header is `testserver`) to pass the `_HostCheckMiddleware` that guards
against DNS-rebinding attacks. Any new test file that uses `TestClient` against
the real app inherits this automatically (conftest loads before tests). If you
need to test host rejection, unset the env var in the test.

## Operational gotchas (these bite every time)

1. **Working directory ⇒ data directory.** `vanchor_data/` (boat profiles, depth
   map, cached charts) is resolved **relative to the server's cwd**. If your shell
   cd'd elsewhere (e.g. into `static/` for a `node --check`) and you then start
   the server, it reads/writes a *different* `vanchor_data/` — you'll see stale
   `boats.json` / missing presets. **Always start the server from the repo root.**
2. **`boats.json` only seeds presets when absent.** To regenerate the starter
   presets, delete `vanchor_data/boats.json` (from the repo root) and restart.
   Don't delete a user's profiles in production.
3. **Killing the server:** `pkill -f vanchor` may miss the uvicorn child that
   actually holds the port. Use `fuser -k 8000/tcp` (then confirm with
   `ss -ltnp | grep :8000`) before restarting. A lingering old server silently
   serves stale code and rewrites `boats.json`.
4. **Restart pattern:**
   ```bash
   fuser -k 8000/tcp; sleep 2
   # (optional) seed the water cache for routing in tests:
   #   WaterCache("vanchor_data").store(bbox, load_geojson("tests/data/water_sim.geojson"))
   source /abs/path/.venv/bin/activate && vanchor --host 0.0.0.0 --port 8000 --nmea-tcp --log-level warning
   ```
5. **Service worker caching.** It's network-first now, but a user on the *old*
   worker still gets stale assets until they reload a couple of times (or the
   worker version is bumped). After UI changes, tell the user to reload twice /
   reopen the installed app. See [frontend.md](frontend.md).
6. **Python heredocs in bash:** prefer `python - <<'EOF'` with `urllib` over
   `curl` + inline `-c` (escaped quotes break repeatedly).
7. **`TestClient(Runtime(...))` spins when the Runtime carries depth data.**
   `with TestClient(create_app(Runtime(cfg))) as c:` hangs at ~100% CPU on the
   lifespan-portal when the Runtime has imported depth data loaded — the real
   uvicorn server with the same data starts fine, so it's a TestClient quirk,
   not a product bug. **FIX:** test the runtime methods *directly*
   (`Runtime(cfg).depth_grid(...)`, `.import_depth_map(...)`) with no
   TestClient — see the `_rt()` helpers in `tests/test_depth_grid.py` /
   `tests/test_depth_import.py`. Verify the thin HTTP routes live instead.
8. **Test isolation — always point `cfg.data_dir` at `tmp_path`.** A `Runtime`
   built with the default data_dir writes into the repo's `vanchor_data/` and
   corrupts `boats.json` / `devices.json` (the recurring-corruption root cause).
   Worse: a large imported `vanchor_data/depthchart.json` (the static depth
   chart, hundreds of MB) makes non-isolated tests slow enough to **time out the
   whole suite** — a known isolation gap. Isolate *any* test that constructs a
   Runtime.
9. **Don't let a pipe mask the pytest exit code.** `python -m pytest … | tail;
   echo $?` reports `tail`'s exit (0), hiding pytest timeouts/hangs (exit
   124/143). Use `echo ${PIPESTATUS[0]}`, or redirect to a file and capture
   `$?` right after the pytest call. Don't trust a background-task "exit 0" that
   went through a pipe.

## Working in parallel (sub-agents)

When fanning work out to sub-agents, give each **disjoint files**. The shared
hubs that must have a single editor at a time:

- `src/vanchor/ui/static/map.js`, `index.html`, `style.css`, `app.js`
- `src/vanchor/app.py`, `src/vanchor/core/config.py`

Design contracts so feature work *doesn't* touch the hubs: e.g. a new depth-grid
field flows through `Runtime.depth_grid` → `/api/depth/grid` without editing
`app.py`/`server.py`; a new overlay self-registers via `VA.map.addOverlay`
without editing `map.js`; a new front-end module is a new file + one script tag.
After agents land, **integrate + verify** (full suite, restart, headless check)
before marking done.

## The recurring bug classes (check these first)

- **Wrong-way / won't-track steering** → `steer_sign` (bow vs stern mount).
- **Weaving on a leg in calm water** → sensor realism (`gps_noise_m`), not the
  control law.
- **Stale UI / "my change isn't showing"** → service-worker cache (network-first
  + reload), or a missing `sw.js` `SHELL` entry.
- **Missing presets / stale boat config** → server cwd / `boats.json`.
- **Phantom oscillation in a test** → control ticked too fast (use the harness).
