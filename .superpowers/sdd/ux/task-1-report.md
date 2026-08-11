# Task 1 Report: IA shuffle (zero behavior change)

## Status: DONE

## What Was Implemented

### 1. Simulator motor response — moved from Devices to Simulator panel

- **From**: `src/vanchor/ui/partials/panel-devices.html` (lines 196–208, the `.ctx-sub` block after `#dev-serial`)
- **To**: `src/vanchor/ui/partials/panel-sim.html` (appended before closing `</details>` of `#sim-card`)
- **Exact ids moved** (all preserved verbatim):
  - `dev-simmotor-revdelay` (Reverse delay input)
  - `dev-simmotor-slew` (Thrust slew input)
  - `dev-simmotor-lag` (Thrust lag input)
- The brief listed ids as `dev-sim-reverse-delay` etc. but the actual markup uses `dev-simmotor-*` — verified against the code before moving. devices.js references are unchanged (grep confirmed lines 344–345, 520–522).

### 2. Battery test-SOC section — moved from Safety to Simulator panel

- **From**: `src/vanchor/ui/partials/panel-safety.html` (`#set-batt-test-section` div, lines 51–58)
- **To**: `src/vanchor/ui/partials/panel-sim.html` (appended after motor response block, inside `#sim-card`)
- **Exact ids moved** (all preserved verbatim):
  - `set-batt-test-section` (outer div — safety.js toggles `.hidden` on this by id)
  - `batt-test` (range input)
  - `batt-test-val` (output)
  - `batt-test-send` (button)
- The ctx-sub label was changed from "Test (simulator)" to "Test battery" to match the brief's "own .ctx-sub 'Test battery'" instruction.
- safety.js hidden/visible toggle continues to work because the id is intact.

### 3. Simulator tile hidden when sim is off

- **File**: `src/vanchor/ui/static/settings.js`
- **Location**: `renderSim` function (~line 281)
- Extended the existing `if (on !== simShown)` block to also toggle `document.querySelector('.cm-tile[data-cat="sim"]')` with the `.hidden` class.
- Live toggle: fires every time sim_enabled flips, same as the card toggle.

### 4. menu.js TITLES — added `tools: "Tools"`

- **File**: `src/vanchor/ui/static/menu.js`
- Added `tools: "Tools"` entry to the `TITLES` object (line 34).
- The Tools panel app-bar now shows "Tools" instead of falling back to "Menu".

## Test Results

| Check | Result |
|-------|--------|
| `node --check menu.js` | PASS |
| `node --check settings.js` | PASS |
| `pytest tests/test_shell_partials.py tests/test_shell_manifest.py -q` | 42/42 passed |
| `pytest -q` (full suite) | 2307 passed, 6 skipped, 10 warnings |
| `SMOKE_PORT=8171 python e2e_smoke.py` | 29/29 PASS |

## Files Changed

- `src/vanchor/ui/partials/panel-devices.html` — removed 14 lines (motor response block)
- `src/vanchor/ui/partials/panel-safety.html` — removed 8 lines (batt test section)
- `src/vanchor/ui/partials/panel-sim.html` — added 27 lines (both blocks)
- `src/vanchor/ui/static/menu.js` — added 1 line (`tools: "Tools"`)
- `src/vanchor/ui/static/settings.js` — added 3 lines (sim tile toggle)

## Commit

`e83ca15` — ux: Task 1 IA shuffle — move sim controls, hide sim tile, fix Tools title

## Self-Review Findings

- All element ids are preserved exactly — no renames or deletions.
- The brief said to verify exact ids in panel-devices.html; done. The brief's example ids (`dev-sim-reverse-delay` etc.) differed from the actual ids (`dev-simmotor-*`), which is why the brief instructed verification.
- The `#set-batt-test-section` id is intact so safety.js's `.classList.toggle("hidden", ...)` pattern continues to work from any DOM position.
- The sim tile toggle is inside the `if (on !== simShown)` guard, matching the brief's "live toggle, not one-shot" requirement.
- No new JS dependencies, no CSS changes, no id renames.

## Concerns

None. This is a clean markup relocation with zero behavior change. All tests pass.

## Review fix

**Change**: Added `hidden` class to sim tile `<button>` (line 980, `src/vanchor/ui/static/index.html`).

**Rationale**: The tile's visibility guard in settings.js only fires on sim-state changes; with sim initially disabled, `false===false` means the toggle never fires, leaving the tile visible. The `#sim-card` already starts HTML-hidden; the tile must match for consistency.

**Tests**:
- `pytest tests/test_shell_partials.py tests/test_shell_manifest.py -q` → 42/42 passed
- `SMOKE_PORT=8172 timeout 200 python e2e_smoke.py` → 29/29 checks passed (validates tile appears when sim is enabled)

**Commit**: `bacfe9c` (ux: sim tile starts hidden)
