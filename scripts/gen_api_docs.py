#!/usr/bin/env python3
"""Generate the Vanchor API reference as Markdown into ``docs/api/``.

One file per top-level subpackage (``core.md``, ``nav.md``, …) plus a
``README.md`` index -- browsable straight on GitHub. Built from the code's
docstrings with pydoc-markdown (``pip install -e '.[docs]'``); run via ``make
docs``. Modules that fail to import (e.g. an optional dependency isn't
installed) are skipped with a note rather than aborting the whole build.
"""

from __future__ import annotations

import importlib
import pkgutil
import subprocess
import sys
from pathlib import Path

OUT = Path("docs/api")
# Top-level groups -> one markdown file each. "app" is a single module.
GROUPS = ["app", "core", "controller", "nav", "sim", "hardware", "ui", "analysis"]


def submodules(pkg: str) -> list[str]:
    mod = importlib.import_module(pkg)
    names = [pkg]
    if hasattr(mod, "__path__"):
        for m in pkgutil.walk_packages(mod.__path__, pkg + "."):
            names.append(m.name)
    return sorted(set(names))


def render(module: str) -> str | None:
    """Render one module to markdown; None if it can't be loaded."""
    r = subprocess.run(["pydoc-markdown", "-m", module],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    index = ["# Vanchor API reference", "",
             "Auto-generated from the package docstrings with "
             "[pydoc-markdown]; regenerate with `make docs`.", ""]
    for group in GROUPS:
        pkg = f"vanchor.{group}"
        try:
            mods = submodules(pkg)
        except Exception as exc:  # noqa: BLE001
            print(f"skip {pkg}: {exc}", file=sys.stderr)
            continue
        parts, ok, skipped = [f"# `{pkg}`\n"], 0, []
        for m in mods:
            md = render(m)
            if md is None:
                skipped.append(m)
                continue
            parts.append(md)
            ok += 1
        if skipped:
            parts.insert(1, "> Skipped (import failed — optional dep?): "
                         + ", ".join(f"`{s}`" for s in skipped) + "\n")
        (OUT / f"{group}.md").write_text("\n".join(parts), encoding="utf-8")
        index.append(f"- [`{pkg}`]({group}.md) — {ok} modules"
                     + (f" ({len(skipped)} skipped)" if skipped else ""))
        print(f"{group}.md: {ok} modules"
              + (f", {len(skipped)} skipped" if skipped else ""))
    (OUT / "README.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    print(f"API reference -> {OUT}/README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
