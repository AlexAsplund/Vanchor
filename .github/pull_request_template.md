<!-- Keep this short. Delete any section that doesn't apply. -->

## What & why

<!-- One or two lines: what changes, and why. -->

Linking an issue is **optional but encouraged** (never required):
`Closes #123` / `Refs #123`. Roadmap work should reference its `roadmap` issue.

## Tests

<!-- Required to acknowledge. Tests are part of the change: any new behaviour,
     bug fix, or edge case must add or update tests that would fail without it. -->

- [ ] Tests added/updated for this change, **or** not applicable because:
      <!-- e.g. docs-only, comment/typo, pure refactor already covered -->

## Checklist

- [ ] `python -m pytest -q` green locally
- [ ] `ruff check src tests` clean; `node --check` on any JS touched
- [ ] Docs updated (`docs/`, `docs/llms/*`, `CHANGELOG.md`) if behaviour changed
- [ ] Branch is up to date with `main` (the required checks will run on the PR)
