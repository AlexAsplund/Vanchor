"""Assert that every .py file under supervisor/ uses only stdlib imports.

Walks the AST of each module and rejects any top-level import of a package
that is not in sys.stdlib_module_names (Python 3.10+) and is not
``vanchor_supervisor`` itself.

This enforces the zero-dependency constraint from task-5-brief.md §3 (D3):
the supervisor daemon runs on Bookworm system python3 with no venv.
"""
from __future__ import annotations
import ast
import sys
from pathlib import Path

import pytest

SUPERVISOR_ROOT = Path(__file__).parent.parent / "supervisor"

# Extra names that are allowed: the package itself + guard (top-level script).
ALLOWED = frozenset({"vanchor_supervisor", "guard"})


def _collect_py_files():
    """Yield all .py files under supervisor/."""
    return list(SUPERVISOR_ROOT.rglob("*.py"))


def _get_imports(path: Path) -> list[str]:
    """Return all top-level module names imported in the file."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # top-level: "import os.path" -> "os"
                names.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                # absolute import: "from json import loads" -> "json"
                names.append(node.module.split(".")[0])
            # relative imports (level > 0) are always within the package — OK
    return names


@pytest.fixture(scope="module")
def stdlib_names() -> frozenset[str]:
    return frozenset(sys.stdlib_module_names)


@pytest.mark.parametrize("pyfile", _collect_py_files(), ids=lambda p: str(p.relative_to(SUPERVISOR_ROOT)))
def test_no_third_party_imports(pyfile: Path, stdlib_names: frozenset[str]) -> None:
    """Every module under supervisor/ must only import stdlib or itself."""
    imports = _get_imports(pyfile)
    for name in imports:
        if name in ALLOWED:
            continue
        assert name in stdlib_names, (
            f"{pyfile.relative_to(SUPERVISOR_ROOT)}: "
            f"found non-stdlib import {name!r}. "
            "The supervisor package must be stdlib-only."
        )
