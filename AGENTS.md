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

## 5. Verify before done

`python -m pytest -q` green, `node --check` any JS you touched, and for UI work a
headless Playwright pass (no console errors). See the testing guide.
