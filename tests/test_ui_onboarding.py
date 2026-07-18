"""Server-side onboarding surface tests (WP10, task 3).

Tests the /api/prefs round-trip for the onboarding.wizard_done key and
verifies that the shell includes the onboard.js script.
"""
import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.ui.server import _render_shell, create_app


@pytest.fixture()
def client(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    return TestClient(create_app(Runtime(cfg)))


def test_onboard_prefs_round_trip(client):
    """PUT onboarding.wizard_done=true, then GET confirms it."""
    r = client.put("/api/prefs", json={"onboarding": {"wizard_done": True}})
    assert r.status_code == 200

    r2 = client.get("/api/prefs")
    assert r2.status_code == 200
    data = r2.json()
    assert data.get("onboarding", {}).get("wizard_done") is True


def test_onboard_prefs_shallow_merge_sibling(client):
    """Prefs shallow-merge must not clobber a sibling 'views' key."""
    # First set a views pref
    r = client.put("/api/prefs", json={"views": {"view": "helm", "widgets": {}}})
    assert r.status_code == 200

    # Now PUT onboarding — should NOT erase views.
    r2 = client.put("/api/prefs", json={"onboarding": {"wizard_done": True}})
    assert r2.status_code == 200

    r3 = client.get("/api/prefs")
    assert r3.status_code == 200
    data = r3.json()
    assert data.get("onboarding", {}).get("wizard_done") is True, \
        "wizard_done was not persisted"
    assert "views" in data, "views key was clobbered by onboarding PUT"
    assert data["views"].get("view") == "helm", \
        "views content was clobbered"


def test_onboard_script_tag_in_shell():
    """The shell contains the onboard.js script tag."""
    html = _render_shell()
    assert '/static/onboard.js' in html, "onboard.js script missing from shell"


def test_onboard_script_loads_after_views():
    """onboard.js must appear after views.js in the shell script list."""
    html = _render_shell()
    views_pos   = html.find('/static/views.js')
    onboard_pos = html.find('/static/onboard.js')
    assert views_pos >= 0,   "views.js missing from shell"
    assert onboard_pos >= 0, "onboard.js missing from shell"
    assert views_pos < onboard_pos, \
        "onboard.js must load after views.js (it depends on VA.openWizard etc.)"
