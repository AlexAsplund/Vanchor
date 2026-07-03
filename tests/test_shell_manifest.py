"""Tests for the app-shell manifest drift guard (roadmap #51).

``index.html`` (the ``<script src>`` tags) and ``sw.js`` (the ``SHELL`` precache
array) each list the front-end app scripts by hand, with no build step to keep
them in sync. ``scripts/check_shell_manifest.py`` cross-checks the two so a
script added to one list but not the other fails CI instead of silently breaking
the offline shell. These tests verify the check passes on the current tree and
detects a simulated drift in either direction.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts" / "check_shell_manifest.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_shell_manifest", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


CHK = _load_checker()


def test_current_tree_is_in_sync():
    """The real index.html and sw.js agree on the app-script set today."""
    problems = CHK.check()
    assert problems == [], "unexpected shell drift:\n" + "\n".join(problems)


def test_lists_are_non_empty():
    """Guard against a parser that silently returns nothing (false green)."""
    idx = CHK.index_scripts()
    shell = CHK.sw_shell_scripts()
    assert len(idx) > 20
    assert idx == shell


def test_main_exit_zero_on_current_tree():
    assert CHK.main([]) == 0


def test_vendor_scripts_excluded():
    """Vendored libs under /static/vendor/ are not counted as app scripts."""
    assert not CHK._is_app_script("/static/vendor/leaflet/leaflet.js")
    assert not CHK._is_app_script("/static/vendor/uplot/uPlot.iife.min.js")
    assert CHK._is_app_script("/static/views.js")


def test_comment_star_glob_not_parsed_as_entry():
    """The `/static/*.js` token in sw.js's comment must not become an entry."""
    shell = CHK.sw_shell_scripts()
    assert "/static/*.js" not in shell


# --- Simulated drift: a script present in one list but not the other -------

_MINIMAL_HTML = """<html><head></head><body>
  <script src="/static/vendor/leaflet/leaflet.js"></script>
  <script src="/static/core.js"></script>
  <script src="/static/hud.js"></script>
  <script src="/static/views.js"></script>
</body></html>"""

_MINIMAL_SW = """"use strict";
const VERSION = "x";
const SHELL = [
  "/",
  "/static/vendor/leaflet/leaflet.js",
  "/static/core.js",
  "/static/hud.js",
  "/static/views.js",
];
"""


def test_helpers_parse_minimal_fixtures():
    assert CHK.index_scripts(_MINIMAL_HTML) == {
        "/static/core.js",
        "/static/hud.js",
        "/static/views.js",
    }
    assert CHK.sw_shell_scripts(_MINIMAL_SW) == {
        "/static/core.js",
        "/static/hud.js",
        "/static/views.js",
    }


def test_drift_script_only_in_index_detected():
    """A new script added to index.html but not sw.js is caught."""
    html = _MINIMAL_HTML.replace(
        '<script src="/static/views.js"></script>',
        '<script src="/static/newmod.js"></script>\n'
        '  <script src="/static/views.js"></script>',
    )
    idx = CHK.index_scripts(html)
    shell = CHK.sw_shell_scripts(_MINIMAL_SW)
    assert "/static/newmod.js" in idx
    assert "/static/newmod.js" not in shell
    assert (idx - shell) == {"/static/newmod.js"}


def test_drift_script_only_in_sw_detected():
    """A script left in sw.js's SHELL but removed from index.html is caught."""
    sw = _MINIMAL_SW.replace(
        '  "/static/views.js",',
        '  "/static/views.js",\n  "/static/orphan.js",',
    )
    idx = CHK.index_scripts(_MINIMAL_HTML)
    shell = CHK.sw_shell_scripts(sw)
    assert "/static/orphan.js" in shell
    assert (shell - idx) == {"/static/orphan.js"}


def test_manifest_optional_returns_none_when_absent(tmp_path, monkeypatch):
    """manifest_scripts() is a no-op when no manifest is provided."""
    # No text and (by default) no file -> None, so the check stays two-way.
    if not CHK.MANIFEST_JSON.exists():
        assert CHK.manifest_scripts() is None
    # Explicit JSON forms parse correctly.
    assert CHK.manifest_scripts('["/static/core.js"]') == {"/static/core.js"}
    assert CHK.manifest_scripts('{"scripts": ["/static/hud.js"]}') == {
        "/static/hud.js"
    }
