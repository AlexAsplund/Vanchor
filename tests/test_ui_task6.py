"""Static code audits for UX Evolution+ Task 6 (WP5/WP11/WP13).

These tests verify structural invariants that cannot be caught by the shell
partial tests alone:
  * no bare confirm() calls in supervisor.js (replaced by #sup-confirm modal)
  * VA.toast is exported in appcore.js (used by offline link-restored toast)
  * install.js appears in both index.html script list and sw.js SHELL array
  * advanced spec fields are inside #boat-advanced, not top-level grid
  * push card summary uses the new human-readable name
"""

from pathlib import Path

STATIC = Path(__file__).parent.parent / "src" / "vanchor" / "ui" / "static"
PARTIALS = Path(__file__).parent.parent / "src" / "vanchor" / "ui" / "partials"


def test_no_bare_confirm_in_supervisor_js():
    """supervisor.js must not call bare confirm() — all confirmations use #sup-confirm."""
    import re
    text = (STATIC / "supervisor.js").read_text()
    # Strip single-line comments so comment mentions of confirm() don't fire.
    text_no_comments = re.sub(r"//[^\n]*", "", text)
    # Remove the modal-helper name so we can check for residual bare confirm(
    stripped = re.sub(r"\bsupConfirm\b", "", text_no_comments)
    assert "confirm(" not in stripped, (
        "supervisor.js still contains a bare confirm() call — replace with supConfirm()"
    )


def test_va_toast_exported_in_appcore():
    """appcore.js must export VA.toast so the offline link-restored toast works."""
    text = (STATIC / "appcore.js").read_text()
    assert "VA.toast" in text, "VA.toast not exported in appcore.js"


def test_install_js_in_index_html():
    """index.html must include a <script src="/static/install.js"> tag."""
    text = (STATIC / "index.html").read_text()
    assert "/static/install.js" in text, "install.js script tag missing from index.html"


def test_install_js_in_sw_shell():
    """sw.js SHELL array must contain /static/install.js."""
    text = (STATIC / "sw.js").read_text()
    assert '"/static/install.js"' in text, "/static/install.js missing from sw.js SHELL array"


def test_advanced_specs_inside_boat_advanced():
    """The 7 advanced spec fields must live inside #boat-advanced, not top-level."""
    text = (PARTIALS / "panel-boat.html").read_text()
    advanced_start = text.find('id="boat-advanced"')
    assert advanced_start >= 0, "#boat-advanced element missing from panel-boat.html"
    advanced_section = text[advanced_start:]

    advanced_fields = [
        "reverse_efficiency", "max_steer_angle_deg", "autopilot_steer_deg",
        "shaft_dia_mm", "steer_range_deg", "steer_reduction", "sonar_cone_deg",
    ]
    for field in advanced_fields:
        assert f'data-field="{field}"' in advanced_section, (
            f"{field!r} spec not found inside #boat-advanced"
        )


def test_push_card_summary_renamed():
    """push-card summary must use human-readable name (item 33 rename)."""
    text = (PARTIALS / "panel-feedback.html").read_text()
    assert "Phone notifications (push)" in text, (
        "push-card summary not renamed to 'Phone notifications (push)'"
    )


def test_tune_card_summary_renamed():
    """tune-card summary must mention 'Auto-tune the autopilot'."""
    text = (PARTIALS / "panel-boat.html").read_text()
    assert "Auto-tune the autopilot" in text, (
        "tune-card summary not renamed to 'Auto-tune the autopilot'"
    )
