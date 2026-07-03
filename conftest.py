"""Root pytest configuration.

This lives at the repository root (alongside ``pyproject.toml``) so its hooks
apply to the whole suite. It exists for roadmap #46: a **hardware-in-the-loop
(HIL)** test marker.

The ``hil`` marker tags tests that need a real bench -- an Arduino motor
controller, a live GPS, a physical trolling motor on a stand -- wired to the
machine running the tests. Those tests obviously cannot run in CI, so they are
**skipped by default** and only execute when the operator explicitly opts in by
setting ``VANCHOR_HIL=1`` in the environment (i.e. "yes, a bench is connected").

The marker is registered here via :func:`pytest_configure` /
``config.addinivalue_line`` (rather than in ``pyproject.toml``) so that pytest
does not emit a ``PytestUnknownMarkWarning`` and the default suite stays green.
See ``tests/hil/`` for a documented example of what a bench test looks like.
"""

from __future__ import annotations

import os

import pytest

# Set this to a truthy value ("1", "yes", "true", ...) when a physical bench is
# connected and you want the ``hil``-marked tests to actually run.
HIL_ENV_VAR = "VANCHOR_HIL"

# Enable pytest's own ``pytester`` fixture so ``tests/test_hil_scaffold.py`` can
# spin up throwaway sub-pytests to prove the skip/run gating actually works.
pytest_plugins = ["pytester"]


def bench_available() -> bool:
    """True iff the operator has declared a HIL bench is connected.

    Gated on the :data:`HIL_ENV_VAR` environment variable. Empty / ``0`` /
    ``false`` (any case) count as "no bench"; anything else means "bench
    present, run the hardware tests". Kept as a tiny pure function so tests (and
    the ``tests/hil`` example) can import and exercise the gate directly.
    """
    return os.environ.get(HIL_ENV_VAR, "").strip().lower() not in ("", "0", "false", "no", "off")


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``hil`` marker so it is a known marker (no warning)."""
    config.addinivalue_line(
        "markers",
        "hil: hardware-in-the-loop bench test (real Arduino/motor/GPS); "
        f"skipped unless {HIL_ENV_VAR}=1 declares a bench is connected",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip every ``hil``-marked test unless a bench is declared present.

    This keeps ``pytest`` (and CI) green out of the box: the HIL tests are
    collected -- so ``pytest --collect-only`` and coverage still see them -- but
    are marked skipped with an actionable reason. Set ``VANCHOR_HIL=1`` on a
    bench-connected machine to actually run them.
    """
    if bench_available():
        return
    skip_hil = pytest.mark.skip(
        reason=(
            f"HIL bench test: no bench declared. Set {HIL_ENV_VAR}=1 (with a real "
            "Arduino/motor/GPS bench connected) to run."
        )
    )
    for item in items:
        if "hil" in item.keywords:
            item.add_marker(skip_hil)
