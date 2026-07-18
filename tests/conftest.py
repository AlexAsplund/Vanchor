"""Shared test configuration.

The UI server validates the HTTP Host header (DNS-rebinding protection in
``vanchor.ui.server``); Starlette's TestClient sends ``Host: testserver``,
which the production rules would reject.  Allow it suite-wide so any test can
build a TestClient without per-fixture env plumbing.  ``setdefault`` keeps a
caller-provided value (e.g. a test that asserts the strict behaviour) intact.
"""

import os
from pathlib import Path

import pytest

os.environ.setdefault("VANCHOR_ALLOWED_HOSTS", "testserver")

# The repo's live data dir holds REAL device config (e.g. a bench GPS wired via
# devices.json). A test that builds a Runtime without isolating data_dir and then
# persists config would clobber it — this has happened twice. Belt-and-braces:
# snapshot the persisted config files before the session and restore them after,
# so no test run can permanently alter the developer's live setup.
_GUARDED = ("devices.json", "connectors.json", "fusion_cal.json", "alerts.json")


@pytest.fixture(autouse=True, scope="session")
def _preserve_repo_data_dir():
    data_dir = Path(__file__).resolve().parent.parent / "vanchor_data"
    saved = {n: (data_dir / n).read_bytes() for n in _GUARDED if (data_dir / n).exists()}
    missing = [n for n in _GUARDED if n not in saved]
    yield
    for name, blob in saved.items():
        try:
            (data_dir / name).write_bytes(blob)
        except OSError:  # pragma: no cover - best-effort restore
            pass
    for name in missing:  # a test created it in the repo dir -> remove
        try:
            (data_dir / name).unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass
