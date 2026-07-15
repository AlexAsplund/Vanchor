"""index.html is assembled from partials at serve time (no build step)."""
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.ui.server import _render_shell, create_app


def test_shell_assembles_with_no_unresolved_includes():
    html = _render_shell()
    assert "#include" not in html  # every marker was resolved
    # all nine settings panels are inlined
    for cat in ("boat", "display", "feedback", "map", "fishing", "safety",
                "devices", "data", "sim"):
        assert f'data-cat="{cat}"' in html


def test_index_routes_serve_the_assembled_shell():
    c = TestClient(create_app(Runtime(load(None))))
    for path in ("/", "/index.html", "/view/helm"):
        r = c.get(path)
        assert r.status_code == 200
        assert "#include" not in r.text
        assert 'data-cat="devices"' in r.text
