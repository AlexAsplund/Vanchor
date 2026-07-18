"""Task-4 mode-grid and driving shell assertions."""
from vanchor.ui.server import _render_shell


def test_anchor_style_seg():
    html = _render_shell()
    assert 'id="anchor-style-seg"' in html
    assert 'id="anchor-smart"' not in html or 'hidden' in html[html.find('id="anchor-smart"'):html.find('id="anchor-smart"')+40]


def test_troll_on_rail():
    html = _render_shell()
    rail_start = html.find('id="mode-rail"')
    more_start = html.find('id="more-menu"')
    assert rail_start >= 0
    assert more_start > rail_start
    troll_in_rail = html.find('data-mode="trolling"', rail_start, more_start)
    assert troll_in_rail >= 0, "trolling must be in the mode rail"


def test_apb_in_more_menu():
    html = _render_shell()
    more_start = html.find('id="more-menu"')
    assert more_start >= 0
    apb_in_more = html.find('data-mode="follow_apb"', more_start)
    assert apb_in_more >= 0, "follow_apb must be in #more-menu"
    assert 'id="more-apb"' in html


def test_man_overboard_spelled_out():
    html = _render_shell()
    # Should appear at least twice (sheet-mob + view-mob at minimum)
    assert html.count("MAN OVERBOARD") >= 2


def test_wheel_hold_toggle():
    html = _render_shell()
    assert 'id="wheel-hold"' in html


def test_steer_seg_new_labels():
    html = _render_shell()
    assert "Off the bow" in html
    assert 'data-steermode="relative"' in html


def test_nav_stop_removed():
    html = _render_shell()
    assert 'id="nav-stop"' not in html


def test_anchor_go_no_hold_tag():
    html = _render_shell()
    anchor_go_idx = html.find('id="anchor-go"')
    assert anchor_go_idx >= 0
    snippet = html[anchor_go_idx:anchor_go_idx+100]
    assert "hold-tag" not in snippet, "anchor-go must remain single-tap (no HOLD tag)"
