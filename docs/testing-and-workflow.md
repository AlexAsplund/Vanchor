# Testing and workflow

## Unit and integration tests

Run the standard pytest suite with:

```
pytest -q
```

The test harness (`tests/harness.py`) wires the real navigator + controller + simulator in lockstep — no asyncio, no wall-clock time — so tests are fast and deterministic.

## Browser end-to-end tests

Two layers of browser testing live in the repo:

| Script | What it does |
|--------|-------------|
| `e2e_smoke.py` | 18-check smoke: backend API + live control loop + Playwright UI checks. Self-contained; starts its own isolated sim server. |
| `uitest.py` | Step-by-step happy-path click-through: mode panels, Go buttons, STOP, settings, wizard, remote. Self-contained; starts its own server. |
| `tests/test_e2e_playwright.py` | Pytest-runnable regression tests: **STOP integrity** (engage a mode → click STOP → assert motor stopped and no "STOP NOT CONFIRMED" banner) and **Reconnect** (go offline → assert disconnected chip + DATA STALE banner → go online → assert telemetry resumes and banner clears). |

### Running locally

```bash
# One-time browser install (if not already done):
playwright install --with-deps chromium

# Full smoke:
python e2e_smoke.py

# Interactive click-through:
python uitest.py

# Playwright regression suite:
pytest tests/test_e2e_playwright.py -v
```

All three tools start their own isolated server on an ephemeral port and clean up on exit, so they can run concurrently without port conflicts.

### CI job

The `browser-e2e` job in `.github/workflows/ci.yml` runs on `ubuntu-latest` alongside (but independent of) the main `test` matrix. It installs Chromium via `playwright install --with-deps chromium`, then runs both `python e2e_smoke.py` and `pytest tests/test_e2e_playwright.py -q`. A failure here does not block the unit-test or lint jobs.

If Playwright or Chromium is absent from the local environment, `tests/test_e2e_playwright.py` skips gracefully via `pytest.importorskip` and a launch check at import time.
