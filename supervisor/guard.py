#!/usr/bin/env python3
"""ExecStartPre boot-count rollback guard.

Runs BEFORE the supervisor starts. Increments pending.json boots counter;
if boots >= 3, flips current symlink back to pending.previous and clears
pending. Always exits 0 (must never block a start).
"""
import json
import os
import sys
from pathlib import Path

INSTALL_ROOT = Path(os.environ.get("SUPERVISOR_INSTALL_ROOT", "/opt/vanchor-supervisor"))


def main():
    pending_path = INSTALL_ROOT / "pending.json"
    if not pending_path.exists():
        return
    try:
        data = json.loads(pending_path.read_text())
    except Exception as exc:
        print(f"guard: could not read pending.json: {exc}", file=sys.stderr)
        return
    boots = data.get("boots", 0) + 1
    data["boots"] = boots
    if boots >= 3:
        previous = data.get("previous")
        if previous:
            versions_dir = INSTALL_ROOT / "versions" / previous
            if versions_dir.exists():
                # Atomic symlink flip: current -> previous version
                current = INSTALL_ROOT / "current"
                tmp = INSTALL_ROOT / "current.tmp"
                if tmp.exists():
                    tmp.unlink()
                os.symlink(str(versions_dir), str(tmp))
                os.replace(str(tmp), str(current))
                print(f"guard: 3 failed boots — reverted to {previous}", file=sys.stderr)
        pending_path.unlink(missing_ok=True)
        return
    pending_path.write_text(json.dumps(data))


if __name__ == "__main__":
    main()
    sys.exit(0)
