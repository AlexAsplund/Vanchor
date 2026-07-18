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
    # hardware setup wizard elements are present
    assert 'id="hwwiz"' in html, "hw-wizard.html not inlined (hwwiz modal missing)"
    assert 'id="hwwiz-open"' in html, "hwwiz-open button missing from panel-devices.html"


def test_index_routes_serve_the_assembled_shell():
    c = TestClient(create_app(Runtime(load(None))))
    for path in ("/", "/index.html", "/view/helm"):
        r = c.get(path)
        assert r.status_code == 200
        assert "#include" not in r.text
        assert 'data-cat="devices"' in r.text


# ---- Task 1 safety chrome checks ------------------------------------------

def test_safety_banners_no_old_banner():
    """The retired #banner element must not be present."""
    html = _render_shell()
    assert 'id="banner"' not in html, "#banner element was NOT retired"


def test_safety_banners_present():
    """#safety-banners container and required strips are in the shell."""
    html = _render_shell()
    assert 'id="safety-banners"' in html
    assert 'id="mob-banner"' in html
    assert "MAN OVERBOARD" in html
    assert 'id="anchor-alarm-banner"' in html
    assert 'id="batt-warn-banner"' in html
    assert 'id="batt-crit-banner"' in html
    assert 'id="rtl-banner"' in html
    assert 'id="shallow-banner"' in html
    assert 'id="link-banner"' in html


def test_peek_bar_layout():
    """Peekbar has STOP, mode button, and MOB."""
    html = _render_shell()
    assert 'id="sheet-stop"' in html
    assert 'id="sheet-mode"' in html
    assert 'id="sheet-mob"' in html
    # sheet-mode must be a <button>, not a <span>.
    idx = html.find('id="sheet-mode"')
    assert idx >= 0
    tag_start = html.rfind('<', 0, idx)
    assert html[tag_start:tag_start + 7] == '<button', \
        "#sheet-mode must be a <button>, not a <span>"


def test_dock_stop_bar_present():
    """#dock-stop-bar with #dock-stop and #dock-mob inside #dock."""
    html = _render_shell()
    assert 'id="dock-stop-bar"' in html
    assert 'id="dock-stop"' in html
    assert 'id="dock-mob"' in html
    assert html.index('id="dock-stop-bar"') < html.index('</nav>')


def test_cm_stop_in_command_menu():
    """#cm-stop pill must be inside the command menu appbar."""
    html = _render_shell()
    assert 'id="cm-stop"' in html
    cm_pos = html.find('class="cm-appbar"')
    stop_pos = html.find('id="cm-stop"')
    close_pos = html.find('id="settings-close"')
    assert cm_pos < stop_pos < close_pos, "#cm-stop is not in .cm-appbar"


def test_anchor_engaged_block_present():
    """#anchor-engaged block must be in the anchor panel."""
    html = _render_shell()
    assert 'id="anchor-engaged"' in html
    assert 'id="ae-status"' in html
    assert 'id="ae-release"' in html
    assert 'id="ae-redrop"' in html


def test_no_emoji_in_safety_banner_messages():
    """Safety banner .sb-msg text must be emoji-free (text-first labels)."""
    import re
    html = _render_shell()
    snip = html[html.find('id="safety-banners"'):]
    snip = snip[:snip.find('id="settings-scrim"')]
    msgs = re.findall(r'class="sb-msg"[^>]*>(.*?)</span>', snip, re.DOTALL)
    EMOJI_RE = re.compile("[\U0001F000-\U0001FFFF\U00002600-\U000027BF]")
    for m in msgs:
        assert not EMOJI_RE.search(m.strip()), \
            f"Emoji found in safety banner message: {m!r}"


def test_api_alerts_endpoint():
    """GET /api/alerts returns a list."""
    c = TestClient(create_app(Runtime(load(None))))
    r = c.get("/api/alerts")
    assert r.status_code == 200
    data = r.json()
    assert "alerts" in data
    assert isinstance(data["alerts"], list)


def test_api_alerts_clear_endpoint():
    """POST /api/alerts/clear returns ok."""
    c = TestClient(create_app(Runtime(load(None))))
    r = c.post("/api/alerts/clear")
    assert r.status_code == 200
    assert r.json().get("ok") is True
