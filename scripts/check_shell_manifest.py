#!/usr/bin/env python3
"""Guard against drift between the two app-shell script lists (roadmap #51).

The set of front-end app scripts is written down in *two* places, with no build
step to keep them in sync:

  * ``index.html`` -- the ``<script src="/static/*.js">`` tags the browser loads.
  * ``sw.js``      -- the ``SHELL`` precache array the service worker caches so
    the app boots fully offline (#82).

If a new module is added to one list but not the other it silently drifts: a
script listed only in ``index.html`` is never precached (so it 404s / serves
stale offline), and one listed only in ``sw.js`` is cached but never actually
loaded. Both failure modes are invisible until you're on the water with no
signal -- exactly when the offline shell has to work.

A build step (bundling / templating both lists from one manifest) is out of
scope: the app is deliberately buildless (plain files served straight off the
Pi). So instead of a single generated source, this script makes the two hand-
maintained lists *provably equivalent* by cross-checking them in CI. Run it from
a pytest test (``tests/test_shell_manifest.py``) and from CI; it exits non-zero
and prints the exact offending filenames when the lists disagree.

Optionally, if ``manifest.shell.json`` exists next to the static files, it is
treated as an additional authoritative list and cross-checked against both --
letting a maintainer keep an explicit single source of truth if they want one
(still no build step; it's just a third list held equal to the other two).

Usage::

    python scripts/check_shell_manifest.py        # exit 0 if in sync, 1 if not
    python scripts/check_shell_manifest.py -v      # also print the shared list
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_STATIC = _REPO / "src" / "vanchor" / "ui" / "static"
INDEX_HTML = _STATIC / "index.html"
SW_JS = _STATIC / "sw.js"
MANIFEST_JSON = _STATIC / "manifest.shell.json"

# An *app* script is a top-level ``/static/<name>.js`` -- no sub-directory. This
# deliberately excludes vendored libraries under ``/static/vendor/...`` (Leaflet,
# uPlot), which are third-party and listed/loaded separately in both files.
_APP_SCRIPT_RE = re.compile(r"^/static/[^/]+\.js$")


def _is_app_script(path: str) -> bool:
    return bool(_APP_SCRIPT_RE.match(path))


def index_scripts(html: str | None = None) -> set[str]:
    """App-script srcs from ``index.html``'s ``<script src=...>`` tags."""
    if html is None:
        html = INDEX_HTML.read_text(encoding="utf-8")
    srcs = re.findall(r"""<script[^>]*\bsrc=["']([^"']+)["']""", html)
    return {s for s in srcs if _is_app_script(s)}


def _shell_array_slice(js: str) -> str:
    """The text between ``const SHELL = [`` and its closing ``];``.

    Isolating the array literal keeps stray ``/static/*.js`` tokens that appear
    in *comments* elsewhere in ``sw.js`` (e.g. the doc note "every
    <script src=\"/static/*.js\">") from being mistaken for real entries.
    """
    m = re.search(r"const\s+SHELL\s*=\s*\[(.*?)\]\s*;", js, re.DOTALL)
    if not m:
        raise ValueError("could not find `const SHELL = [ ... ];` array in sw.js")
    return m.group(1)


def sw_shell_scripts(js: str | None = None) -> set[str]:
    """App scripts from the ``SHELL`` precache array in ``sw.js``.

    Full-line ``//`` comments inside the array are dropped first, so a
    ``"/static/*.js"`` token appearing in a comment (as it does in the doc note
    above the app-scripts block) isn't mistaken for a real precache entry.
    """
    if js is None:
        js = SW_JS.read_text(encoding="utf-8")
    body = _shell_array_slice(js)
    lines = [ln for ln in body.splitlines() if not ln.lstrip().startswith("//")]
    entries = re.findall(r"""["']([^"']+)["']""", "\n".join(lines))
    return {e for e in entries if _is_app_script(e)}


def manifest_scripts(text: str | None = None) -> set[str] | None:
    """App scripts from the optional ``manifest.shell.json`` (``None`` if absent).

    The manifest may be either a bare JSON array of paths, or an object with a
    ``"scripts"`` (or ``"shell"``) key holding the array.
    """
    if text is None:
        if not MANIFEST_JSON.exists():
            return None
        text = MANIFEST_JSON.read_text(encoding="utf-8")
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("scripts") or data.get("shell") or []
    if not isinstance(data, list):
        raise ValueError("manifest.shell.json must be a list or {scripts|shell: [...]}")
    return {p for p in data if isinstance(p, str) and _is_app_script(p)}


def check() -> list[str]:
    """Return a list of human-readable drift problems (empty == in sync)."""
    idx = index_scripts()
    shell = sw_shell_scripts()
    problems: list[str] = []

    only_index = sorted(idx - shell)
    only_shell = sorted(shell - idx)
    if only_index:
        problems.append(
            "In index.html <script> tags but MISSING from sw.js SHELL precache "
            "(won't be cached for offline): " + ", ".join(only_index)
        )
    if only_shell:
        problems.append(
            "In sw.js SHELL precache but MISSING from index.html <script> tags "
            "(cached but never loaded): " + ", ".join(only_shell)
        )

    manifest = manifest_scripts()
    if manifest is not None:
        only_manifest = sorted(manifest - idx)
        missing_from_manifest = sorted(idx - manifest)
        if only_manifest:
            problems.append(
                "In manifest.shell.json but not loaded by index.html: "
                + ", ".join(only_manifest)
            )
        if missing_from_manifest:
            problems.append(
                "Loaded by index.html but missing from manifest.shell.json: "
                + ", ".join(missing_from_manifest)
            )

    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    problems = check()
    if problems:
        print("Shell manifest DRIFT detected between index.html and sw.js:\n")
        for p in problems:
            print("  - " + p)
        print(
            "\nFix: add/remove the script in BOTH src/vanchor/ui/static/index.html"
            " and src/vanchor/ui/static/sw.js (SHELL array)."
        )
        return 1

    shared = sorted(index_scripts())
    print(f"OK: index.html and sw.js agree on {len(shared)} app scripts.")
    if args.verbose:
        for s in shared:
            print("  " + s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
