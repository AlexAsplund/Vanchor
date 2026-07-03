"""Tests for the HIL marker + skip scaffold (roadmap #46).

Two things are proven:

1. The real repo-root ``conftest.py`` registers the ``hil`` marker (so pytest
   emits no ``PytestUnknownMarkWarning``), and its ``bench_available()`` gate
   reads the ``VANCHOR_HIL`` env var correctly.
2. The gating *mechanism* -- ``hil``-marked tests skip by default and run when
   the env var is set -- works end to end, exercised with pytest's own
   ``pytester`` in an isolated throwaway project.
"""

from __future__ import annotations

import conftest as root_conftest  # the repo-root conftest.py under test


# --------------------------------------------------------------------------- #
# The real conftest: marker registration + env gate
# --------------------------------------------------------------------------- #
def test_hil_marker_is_registered(pytestconfig):
    """The ``hil`` marker is a known marker -> no unknown-marker warning."""
    markers = pytestconfig.getini("markers")
    assert any(m.startswith("hil:") for m in markers), markers


def test_bench_available_reads_env(monkeypatch):
    monkeypatch.delenv(root_conftest.HIL_ENV_VAR, raising=False)
    assert root_conftest.bench_available() is False

    for truthy in ("1", "yes", "true", "on", "YES"):
        monkeypatch.setenv(root_conftest.HIL_ENV_VAR, truthy)
        assert root_conftest.bench_available() is True

    for falsy in ("", "0", "false", "no", "off"):
        monkeypatch.setenv(root_conftest.HIL_ENV_VAR, falsy)
        assert root_conftest.bench_available() is False


# --------------------------------------------------------------------------- #
# The gating mechanism, exercised in an isolated sub-pytest via ``pytester``.
# The conftest below mirrors the repo-root hooks so we test the real behaviour
# without depending on the outer session's environment.
# --------------------------------------------------------------------------- #
_CONFTEST = '''
import os
import pytest

HIL_ENV_VAR = "VANCHOR_HIL"

def bench_available():
    return os.environ.get(HIL_ENV_VAR, "").strip().lower() not in (
        "", "0", "false", "no", "off"
    )

def pytest_configure(config):
    config.addinivalue_line("markers", "hil: hardware-in-the-loop bench test")

def pytest_collection_modifyitems(config, items):
    if bench_available():
        return
    skip = pytest.mark.skip(reason="HIL bench test: set VANCHOR_HIL=1 to run")
    for item in items:
        if "hil" in item.keywords:
            item.add_marker(skip)
'''

_TEST = '''
import pytest

@pytest.mark.hil
def test_needs_bench():
    assert True

def test_plain():
    assert True
'''


def _make_project(pytester):
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(_TEST)


def test_hil_skips_without_env(pytester, monkeypatch):
    monkeypatch.delenv("VANCHOR_HIL", raising=False)
    _make_project(pytester)
    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1, skipped=1)
    # No unknown-marker warning must appear.
    assert "PytestUnknownMarkWarning" not in result.stdout.str()


def test_hil_runs_with_env(pytester, monkeypatch):
    monkeypatch.setenv("VANCHOR_HIL", "1")
    _make_project(pytester)
    result = pytester.runpytest("-v")
    # Now both tests run; nothing is skipped.
    result.assert_outcomes(passed=2, skipped=0)


def test_no_unknown_marker_warning(pytester, monkeypatch):
    """Registering the marker suppresses PytestUnknownMarkWarning even under -W error."""
    monkeypatch.delenv("VANCHOR_HIL", raising=False)
    _make_project(pytester)
    # If the marker were unregistered, -W error on the warning would error the run.
    result = pytester.runpytest("-W", "error::pytest.PytestUnknownMarkWarning")
    result.assert_outcomes(passed=1, skipped=1)
