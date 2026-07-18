"""PEP-440-alpha-aware version parsing and comparison."""
from __future__ import annotations
import re

# "1.5.0a8" -> (1,5,0,0,8); "1.5.0" -> (1,5,0,3,0)
# pre-release ranks: a=0, b=1, rc=2, final=3
_PRE_RANK = {"a": 0, "b": 1, "rc": 2}
_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")


def parse_version(s: str) -> tuple[int, int, int, int, int]:
    """Parse version string into a 5-tuple for comparison.

    Raises ValueError on unparseable input.
    """
    s = s.strip()
    m = _PATTERN.match(s)
    if not m:
        raise ValueError(f"Cannot parse version {s!r}")
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    pre_kind = m.group(4)
    pre_num = int(m.group(5)) if m.group(5) else 0
    pre_rank = _PRE_RANK.get(pre_kind, 3) if pre_kind else 3
    return (major, minor, patch, pre_rank, pre_num)


def is_at_least(installed: str, required: str) -> bool:
    """Return True if installed >= required."""
    return parse_version(installed) >= parse_version(required)
