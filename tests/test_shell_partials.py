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


# ---- Task 2 glanceable truth checks ----------------------------------------

def test_task2_peek_instruments_ids():
    """New peek instrument ids: si-ctx, m-batt-volts, mode-pill."""
    html = _render_shell()
    assert 'id="si-ctx"' in html, "si-ctx tile missing from peek instruments"
    assert 'id="m-batt-volts"' in html, "m-batt-volts sub-label missing from BATT tile"
    assert 'id="mode-pill"' in html, "mode-pill missing from #map-pills"
    assert 'id="map-pills"' in html, "#map-pills container missing"
    # Task-1 ids must still be present (no regression)
    assert 'id="sheet-mob"' in html, "#sheet-mob (Task 1) must still be present"
    assert 'id="sheet-stop"' in html, "#sheet-stop must still be present"


def test_task2_no_emoji_bell():
    """alerts-open must not contain the 🔔 emoji — replaced by inline SVG."""
    html = _render_shell()
    # Find the alerts-open button content
    idx = html.find('id="alerts-open"')
    assert idx >= 0
    # The bell emoji must not appear in the button (look forward ~300 chars)
    snippet = html[idx:idx + 300]
    assert "\U0001F514" not in snippet, "Bell emoji 🔔 still present in alerts-open button"


def test_task2_ctx_sub_ids():
    """Peek ctx cell has label, unit, and sub-label ids."""
    html = _render_shell()
    assert 'id="si-ctx-label"' in html
    assert 'id="si-ctx-unit"' in html
    assert 'id="si-ctx-sub"' in html


def test_task2_si_batt_ids():
    """Peek BATT tile has the id and sub-label."""
    html = _render_shell()
    assert 'id="si-batt"' in html
    assert 'id="m-batt-volts"' in html


def test_task2_rtl_separation():
    """RTL button has rtl-danger styling class and subtitle."""
    html = _render_shell()
    assert "rtl-danger" in html, "rtl-danger class missing from RTL button"
    assert "rtl-sub" in html, "rtl-sub subtitle missing"
    assert "drives the boat home" in html, "RTL subtitle text missing"


def test_task2_jog_labels():
    """Jog pad has bow-relative labels and 1 m per tap caption."""
    html = _render_shell()
    # Check for dpad-lbl spans and caption
    assert 'class="dpad-lbl"' in html, "dpad-lbl spans missing"
    assert "1 m per tap" in html, "'1 m per tap' caption missing"


def test_task2_steer_hint_collapse():
    """Steering hint has expand button and hidden extra paragraph."""
    html = _render_shell()
    assert 'id="steer-hint-expand"' in html, "steer-hint-expand button missing"
    assert 'id="steer-hint-extra"' in html, "steer-hint-extra span missing"


# ---- Task 3 sim honesty + onboarding checks --------------------------------

def test_task3_sim_indicator_id():
    """id=sim-indicator replaces the old id=demo-indicator (renamed in task 3)."""
    html = _render_shell()
    assert 'id="sim-indicator"' in html, "sim-indicator missing"
    assert 'id="demo-indicator"' not in html, "old demo-indicator still present"


def test_task3_firstrun_dialog():
    """First-run dialog and its key elements are present in the shell."""
    html = _render_shell()
    assert 'id="firstrun"' in html, "#firstrun dialog missing"
    assert 'id="firstrun-real"' in html, "#firstrun-real button missing"
    assert 'id="firstrun-sim"' in html, "#firstrun-sim button missing"
    assert "SIMULATION" in html, "SIMULATION text missing from firstrun dialog"
    assert "not your motor" in html, "SIMULATION warning text missing"


def test_task3_get_started_tile():
    """Get-started tile is present as the first tile in the home grid."""
    html = _render_shell()
    assert 'id="cm-get-started"' in html, "#cm-get-started tile missing"
    assert "Get started" in html, "Get started text missing"


def test_task3_onboard_script_last():
    """onboard.js script tag is present and loads after views.js."""
    html = _render_shell()
    assert '/static/onboard.js' in html, "onboard.js script tag missing"
    views_pos = html.find('/static/views.js')
    onboard_pos = html.find('/static/onboard.js')
    assert views_pos < onboard_pos, "onboard.js must load after views.js"


def test_task3_calib_error_card():
    """Calibration error card is present in the wizard step 3."""
    html = _render_shell()
    assert 'id="calib-error"' in html, "#calib-error div missing"
    assert 'id="calib-error-msg"' in html, "#calib-error-msg missing"
    assert 'id="calib-error-raw"' in html, "#calib-error-raw missing"


def test_task3_hwwiz_save_gate():
    """Save-gate hint is present in the hardware wizard finish step."""
    html = _render_shell()
    assert 'id="hwwiz-save-gate"' in html, "#hwwiz-save-gate hint missing"


def test_task3_boat_setup_rename():
    """Wizard header reads 'Boat setup', not 'Init Boat'."""
    html = _render_shell()
    assert "Boat setup" in html, "'Boat setup' wizard title missing"
    assert "Init Boat" not in html, "'Init Boat' text must not appear in UI"


def test_task3_view_switcher_text_labels():
    """#view-switcher must contain text labels and no emoji."""
    html = _render_shell()
    # Slice between view-switcher open and its closing </div>
    start = html.find('id="view-switcher"')
    assert start >= 0, "#view-switcher not found"
    end = html.find("</div>", start)
    snip = html[start:end]
    assert "CHART" in snip, "CHART label missing from view-switcher"
    assert "HELM" in snip, "HELM label missing from view-switcher"
    # None of the four old emoji should appear in the switcher
    for emoji in ["🗺", "🕹", "📊", "🎚"]:
        assert emoji not in snip, f"Emoji {emoji!r} still in view-switcher"
