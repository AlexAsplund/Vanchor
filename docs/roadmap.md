# Roadmap

**The roadmap now lives in GitHub issues**, not this file. Planned work is tracked
as issues labelled [`roadmap`](https://github.com/AlexAsplund/Vanchor/issues?q=is%3Aissue+is%3Aopen+label%3Aroadmap),
grouped by area label:

| Area | Label |
|---|---|
| Adoption / onboarding / UX | [`adoption`](https://github.com/AlexAsplund/Vanchor/issues?q=is%3Aissue+is%3Aopen+label%3Aadoption) |
| Control loops, modes, ML anchor | [`control-ml`](https://github.com/AlexAsplund/Vanchor/issues?q=is%3Aissue+is%3Aopen+label%3Acontrol-ml) |
| Safety floor, failsafes, robustness | [`safety`](https://github.com/AlexAsplund/Vanchor/issues?q=is%3Aissue+is%3Aopen+label%3Asafety) |
| Boards, sensors, motors, bench work | [`hardware`](https://github.com/AlexAsplund/Vanchor/issues?q=is%3Aissue+is%3Aopen+label%3Ahardware) |
| Simulator fidelity | [`sim`](https://github.com/AlexAsplund/Vanchor/issues?q=is%3Aissue+is%3Aopen+label%3Asim) |

For what's **done**, see the [CHANGELOG](../CHANGELOG.md) and
[FEATURES.md](FEATURES.md) — the original v1.0-alpha roadmap and the 2026-07
full-project review (Phases 0–7: safety floor, robustness, UI/API, nav/control,
sim depth, hardware, community groundwork) all shipped.

## Working with the roadmap

- **See what's next:** `gh issue list --label roadmap` (add `--label safety`, etc.
  to filter by area).
- **Propose work:** open an issue and label it `roadmap` plus an area label. Keep
  it concrete: what, why, acceptance, and the files it touches.
- **When it ships:** close the issue and add a dated entry to the
  [CHANGELOG](../CHANGELOG.md). Don't re-open a roadmap section here.

Two areas keep their design detail in dedicated docs (the issues link to them):

- **Simulator fidelity** — the known sim-vs-real gaps live in
  [simulator.md](simulator.md).
- **Extensibility / packs** — the plug-in architecture and HACS-style sharing
  design live in [extensibility.md](extensibility.md).
