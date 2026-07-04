#!/usr/bin/env python3
"""Bump the project version (single source of truth: ``pyproject.toml``).

Versions are PEP 440 with an alpha suffix while the project is pre-1.0-stable:
``MAJOR.MINOR.PATCHaN`` (e.g. ``1.4.0a0``). The default bump increments the
alpha number; ``minor``/``major``/``patch`` start a fresh ``a0`` on the next
release number; ``final`` graduates out of alpha (drops the suffix).

Prints the NEW version to stdout (last line) so CI can capture it. With
``--changelog`` it also rolls ``CHANGELOG.md``: the top ``## Unreleased`` section
becomes ``## [<version>] — <date>`` and a fresh empty ``## Unreleased`` is
inserted above it.

    python scripts/bump_version.py --part alpha            # 1.4.0a0 -> 1.4.0a1
    python scripts/bump_version.py --part minor --changelog
    python scripts/bump_version.py --part final            # 1.4.0a3 -> 1.4.0
"""
from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"

_VERSION_RE = re.compile(r'(?m)^version\s*=\s*"([^"]+)"')
_PEP440 = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:a(\d+))?$")


def parse(v: str) -> tuple[int, int, int, int | None]:
    m = _PEP440.match(v)
    if not m:
        sys.exit(f"unparseable version {v!r} (want MAJOR.MINOR.PATCH[aN])")
    maj, minor, patch, alpha = m.groups()
    return int(maj), int(minor), int(patch), (int(alpha) if alpha is not None else None)


def bump(current: str, part: str) -> str:
    maj, minor, patch, alpha = parse(current)
    if part == "alpha":
        # a3 -> a4; a fresh alpha of the *next* patch when currently final.
        return f"{maj}.{minor}.{patch}a{alpha + 1}" if alpha is not None \
            else f"{maj}.{minor}.{patch + 1}a0"
    if part == "final":
        if alpha is None:
            sys.exit(f"{current} is already a final release")
        return f"{maj}.{minor}.{patch}"
    if part == "patch":
        return f"{maj}.{minor}.{patch + 1}a0"
    if part == "minor":
        return f"{maj}.{minor + 1}.0a0"
    if part == "major":
        return f"{maj + 1}.0.0a0"
    sys.exit(f"unknown part {part!r}")


def read_current() -> tuple[str, str]:
    text = PYPROJECT.read_text()
    m = _VERSION_RE.search(text)
    if not m:
        sys.exit("could not find a version = \"...\" line in pyproject.toml")
    return m.group(1), text


def roll_changelog(new: str) -> None:
    """Turn the top '## Unreleased' into '## [<new>] — <date>' + a fresh one."""
    if not CHANGELOG.exists():
        return
    text = CHANGELOG.read_text()
    today = datetime.date.today().isoformat()
    marker = "## Unreleased"
    if marker not in text:
        return
    replacement = f"## Unreleased\n\n## [{new}] — {today}"
    CHANGELOG.write_text(text.replace(marker, replacement, 1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--part", default="alpha",
                    choices=["alpha", "patch", "minor", "major", "final"])
    ap.add_argument("--changelog", action="store_true",
                    help="also roll CHANGELOG.md (Unreleased -> the new version)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    current, text = read_current()
    new = bump(current, args.part)
    if not args.dry_run:
        m = _VERSION_RE.search(text)
        PYPROJECT.write_text(text[: m.start(1)] + new + text[m.end(1):])
        if args.changelog:
            roll_changelog(new)
    print(new)


if __name__ == "__main__":
    main()
