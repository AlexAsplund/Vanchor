"""Shared test configuration.

The UI server validates the HTTP Host header (DNS-rebinding protection in
``vanchor.ui.server``); Starlette's TestClient sends ``Host: testserver``,
which the production rules would reject.  Allow it suite-wide so any test can
build a TestClient without per-fixture env plumbing.  ``setdefault`` keeps a
caller-provided value (e.g. a test that asserts the strict behaviour) intact.
"""

import os

os.environ.setdefault("VANCHOR_ALLOWED_HOSTS", "testserver")
