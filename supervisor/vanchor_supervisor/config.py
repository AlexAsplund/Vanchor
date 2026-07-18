"""SupervisorSettings: JSON-backed config at /etc/vanchor-supervisor/config.json."""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass, asdict, fields
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class SupervisorSettings:
    listen_host: str = "127.0.0.1"      # NEVER expose beyond localhost
    listen_port: int = 9300
    state_dir: str = "/var/lib/vanchor-supervisor"   # jobs/, backups/, containers.json, token
    install_root: str = "/opt/vanchor-supervisor"    # versions/, current, guard.py
    data_volume: str = "vanchor_data"   # docker volume holding /data
    health_gate_s: float = 60.0         # max wait for healthy after recreate
    health_ok_count: int = 3            # consecutive 200s required
    health_poll_s: float = 2.0
    backup_retention: int = 5
    disk_warn_pct: float = 80.0
    disk_crit_pct: float = 92.0
    min_free_mb_for_update: int = 500   # refuse update below this free space


def load_settings(path: str | Path | None = None) -> SupervisorSettings:
    """Load settings from a JSON file, using defaults for any missing/unknown key."""
    if path is None:
        path = "/etc/vanchor-supervisor/config.json"
    p = Path(path)
    if not p.exists():
        return SupervisorSettings()
    try:
        data = json.loads(p.read_text())
    except Exception as exc:
        log.warning("Failed to read %s: %s — using defaults", p, exc)
        return SupervisorSettings()
    known = {f.name for f in fields(SupervisorSettings)}
    kwargs = {}
    for k, v in data.items():
        if k not in known:
            log.warning("Unknown settings key %r (ignored)", k)
            continue
        kwargs[k] = v
    return SupervisorSettings(**kwargs)
