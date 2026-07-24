# AGENTS.md — start here

You are an AI/LLM about to work on **Vanchor-NG**. Before editing anything:

## 1. Read the developer guide

**[`docs/llms/README.md`](docs/llms/README.md)** is the curated, LLM-oriented
guide to the whole project. It orients you in ~5 minutes and links to per-area
guides:

- [`docs/llms/architecture.md`](docs/llms/architecture.md) — data flow, loops, invariants
- [`docs/llms/backend.md`](docs/llms/backend.md) — Python: runtime, control modes, nav, config
- [`docs/llms/simulation.md`](docs/llms/simulation.md) — physics, sensors, boat parameters
- [`docs/llms/frontend.md`](docs/llms/frontend.md) — the web UI (`VA.*`, map, PWA)
- [`docs/llms/api.md`](docs/llms/api.md) — REST + WebSocket contract
- [`docs/llms/testing-and-workflow.md`](docs/llms/testing-and-workflow.md) — running, testing, gotchas

## 2. Golden rules

- **Simulate, don't theorise.** Reproduce + measure control/nav changes in the
  harness (`tests/harness.py`) before claiming they work.
- **Keep new defaults a no-op** so the full suite stays green.
- **Run the server from the repo root** (the data dir is cwd-relative).
- Front end has **no build step**; `node --check` is the only JS gate; the
  service worker is **network-first** (bump its version + `SHELL` list when you
  add/change shell assets).

## 3. 🔁 Keep `docs/llms/*` current — mandatory

Whenever you add, remove, rename, or change behaviour of code, **update the
matching `docs/llms/*` file in the same change**, before considering the task
done. A stale guide misleads the next agent. See the "Keeping these docs
current" section in [`docs/llms/README.md`](docs/llms/README.md).

## 4. Writing docs

For the human docs in `docs/` (the `docs/llms/*` rule above still governs the LLM
guide):

- **Concise and spacious.** A shorter version with the same information is
  better. Prefer tables over prose lists. One idea per paragraph.
- **One home per topic.** Don't duplicate across files — link instead. Adding or
  removing a doc means updating the index in [`docs/README.md`](docs/README.md).
- **Describe current reality, not history.** `roadmap.md` is what's *next*;
  shipped work belongs in the `CHANGELOG`. When you implement a design/research
  doc, fold the still-useful bits into the living docs and **delete the stale
  design doc** — don't leave a "recommend X" doc once X ships. Never rewrite the
  `CHANGELOG`'s dated entries (they're point-in-time).
- **No stale claims, no broken links.** Update wording when behaviour changes;
  keep internal `.md` links valid.
- **`docs/api/` is generated** by `make docs` — don't hand-edit it.

## 5. The roadmap lives in GitHub issues

Planned/future work is tracked as **GitHub issues labelled `roadmap`** (plus an
area label: `adoption`, `control-ml`, `safety`, `hardware`, `sim`), not in a
markdown file. [`docs/roadmap.md`](docs/roadmap.md) is only a pointer.

- **See what's next:** `gh issue list --label roadmap` (filter by area, e.g.
  `--label safety`).
- **Propose work:** `gh issue create` and label it `roadmap` + an area label.
  State what, why, acceptance, and the files it touches — concretely.
- **When it ships:** close the issue and add a dated [`CHANGELOG`](CHANGELOG.md)
  entry. Don't reintroduce a roadmap list in the docs.

Design detail for two big areas still lives in docs the issues link to:
[`docs/simulator.md`](docs/simulator.md) (sim-vs-real gaps) and
[`docs/extension-packs.md`](docs/extension-packs.md) (the pack system).

## 6. Branching, PRs & merging (`main` is protected)

**Never commit to `main` directly.** Branch off `main`, push, open a PR, let CI
go green, then merge. `main` is protected — force-push and deletion are blocked,
and **8 status checks are required** before a PR can merge:

| Required check | Reproduce locally before pushing |
|---|---|
| `Test (Python 3.11)` / `Test (Python 3.12)` | `python -m pytest -q` |
| `Lint` | `ruff check src tests` |
| `JS syntax` | `node --check` on each `.js` you touched |
| `Browser E2E` | `python e2e_smoke.py` **and** `pytest -m e2e tests/test_e2e_playwright.py -q` |
| `Sim regression gate` | `python scripts/regression_check.py --verbose` **and** `pytest scripts/test_ci_regression.py -q` |
| `NMEA parser fuzz (Hypothesis)` | `python -m pytest tests/test_nmea_fuzz.py -q` |
| `Firmware command-parser host test` | `make -C firmware/steering/tests` |

Protection also has **strict** mode on: a PR must be **up to date with `main`**
before it merges (rebase/merge `main` in if it moved). No reviews are required
(solo maintainer).

**Flow:**
```bash
git checkout -b feat/<slug>          # or fix/… , docs/…
# …work, committing with the required trailers…
git push -u origin feat/<slug>
gh pr create --base main --fill      # or --title/--body
gh pr merge <n> --squash --auto --delete-branch
```
`--auto` now **waits for the required checks** and merges only once they pass —
that is the point of the protection (before it existed, `--auto` merged
instantly). Prefer **squash** merges to keep `main` linear.

Admin can bypass in a genuine emergency (`enforce_admins` is off), but **don't
merge red or pending CI** — reproduce the failing check locally, fix, push.

To change the required set or strictness later:
`gh api --method PUT repos/AlexAsplund/Vanchor/branches/main/protection --input <file>`
(GET the same path to see the current config).

## 7. Verify before done

Before opening/merging a PR, get the required checks (§6) green **locally** — at
minimum `python -m pytest -q`, `ruff check src tests`, `node --check` on any JS
you touched, and for UI work `python e2e_smoke.py` (no console errors). Pushing
red CI just burns a round-trip.
