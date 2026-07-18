"""Task 5 — Map + Daylight: static checks (node --check, DOM, CSS, manifest)."""
import subprocess
import pathlib

ROOT = pathlib.Path(__file__).parent.parent
STATIC = ROOT / "src/vanchor/ui/static"
INDEX = STATIC / "index.html"
SW = STATIC / "sw.js"
CSS = STATIC / "style.css"


def _txt(p):
    return p.read_text(encoding="utf-8")


# ---- node --check ----
def test_node_check_pinpopup():
    r = subprocess.run(["node", "--check", str(STATIC / "pinpopup.js")], capture_output=True)
    assert r.returncode == 0, r.stderr.decode()


def test_node_check_armbar():
    r = subprocess.run(["node", "--check", str(STATIC / "armbar.js")], capture_output=True)
    assert r.returncode == 0, r.stderr.decode()


def test_node_check_themectl():
    r = subprocess.run(["node", "--check", str(STATIC / "themectl.js")], capture_output=True)
    assert r.returncode == 0, r.stderr.decode()


# ---- manifest (sw.js SHELL includes new files) ----
def test_shell_includes_pinpopup():
    assert "/static/pinpopup.js" in _txt(SW)


def test_shell_includes_armbar():
    assert "/static/armbar.js" in _txt(SW)


def test_shell_includes_themectl():
    assert "/static/themectl.js" in _txt(SW)


# ---- index.html contains new DOM + scripts ----
def test_index_arm_banner():
    html = _txt(INDEX)
    assert 'id="arm-banner"' in html
    assert 'id="arm-banner-done"' in html
    assert 'id="arm-banner-cancel"' in html


def test_index_script_pinpopup():
    assert 'src="/static/pinpopup.js"' in _txt(INDEX)


def test_index_script_armbar():
    assert 'src="/static/armbar.js"' in _txt(INDEX)


def test_index_script_themectl():
    assert 'src="/static/themectl.js"' in _txt(INDEX)


# ---- route.js now has pinpopup fallback (no bare return on idle tap) ----
def test_route_js_pinpopup_wired():
    txt = _txt(STATIC / "route.js")
    # The old bare-return branch `if (!wpArmed) return;` must be gone
    assert "if (!wpArmed) return;" not in txt
    # The new branch must reference pinpopup
    assert "VA.pinPopup" in txt


# ---- style.css: leaflet controls upgraded to >=44px ----
def test_css_leaflet_bar_no_32px():
    css = _txt(CSS)
    # The old 32px width on .leaflet-bar a must be gone
    assert "width: 32px; height: 32px; line-height: 32px" not in css


# ---- daylight carve-out deleted ----
def test_css_daylight_carveout_deleted():
    css = _txt(CSS)
    # The carve-out scoped dark-palette variables inside .topbar/.hud for daylight.
    # Its selector was 'html[data-theme="daylight"] .topbar,' — that block must be gone.
    assert 'html[data-theme="daylight"] .topbar,' not in css


# ---- VA.theme exported ----
def test_settings_exports_va_theme():
    txt = _txt(STATIC / "settings.js")
    assert "VA.theme" in txt
    assert "VA.theme.set" in txt or "set: setTheme" in txt


# ---- VA.anchorCtl exported ----
def test_controls_exports_va_anchorctl():
    txt = _txt(STATIC / "controls.js")
    assert "VA.anchorCtl" in txt
    assert "engageAt" in txt


# ---- VA.routeEditor.gotoTo exported ----
def test_route_exports_gototo():
    txt = _txt(STATIC / "route.js")
    assert "VA.routeEditor" in txt
    assert "gotoTo" in txt


# ---- VA.geo.haversineM exported ----
def test_route_exports_geo_haversine():
    txt = _txt(STATIC / "route.js")
    assert "VA.geo" in txt
    assert "haversineM" in txt


# ---- VA.markers.create exported ----
def test_markers_exports_create():
    txt = _txt(STATIC / "markers.js")
    assert "VA.markers" in txt
    assert "create" in txt and "createMarker" in txt


# ---- mocks exist ----
def test_mocks_phone_chart_exists():
    p = ROOT / ".superpowers/sdd/ux/t5-mocks/phone-chart.html"
    assert p.exists(), "Mock phone-chart.html missing"


def test_mocks_phone_alarm_exists():
    p = ROOT / ".superpowers/sdd/ux/t5-mocks/phone-alarm.html"
    assert p.exists(), "Mock phone-alarm.html missing"
