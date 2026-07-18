# Vanchor Supervisor

Host-side daemon that owns the Vanchor container lifecycle: OTA updates, backups, rollback, and disk management.

## Overview and Architecture

The supervisor is a **Python daemon running directly on the host** (not inside a container). This is a deliberate design choice: a supervisor that lives outside the container it manages can stop, replace, and restart that container without any self-referential bootstrapping problem.

```
Pi host
├── /opt/vanchor-supervisor/        ← installed supervisor code
│   ├── current -> versions/0.1.0/  ← symlink, atomically swapped on update
│   ├── versions/
│   │   └── 0.1.0/
│   │       └── vanchor_supervisor/ ← the Python package
│   └── guard.py                    ← ExecStartPre rollback guard
├── /var/lib/vanchor-supervisor/    ← runtime state
│   ├── containers.json             ← source of truth for the container
│   ├── token                       ← auth token (chmod 600)
│   ├── jobs/                       ← persisted job state
│   └── backups/                    ← volume snapshots
├── /etc/vanchor-supervisor/
│   └── config.json                 ← optional settings overrides
└── docker                          ← manages the vanchor container
    └── vanchor container
        └── /data (docker volume)
            └── supervisor/token    ← copy for in-container access (chmod 644)
```

Key responsibilities:
- **OTA updates**: pull a new image or load from a bundle, recreate the container, health-gate, auto-rollback on failure.
- **Backup/restore**: snapshot the `/data` docker volume to `.tar.gz`, prune old snapshots.
- **Self-update**: install a new supervisor version from a supervisor bundle with boot-count rollback via `guard.py`.
- **Disk management**: report usage, prune old image layers.
- **Device policy**: verify required hardware devices are visible in the container.

## Requirements

- Python 3.11+ (system Python, no virtualenv needed)
- Docker (with the `docker` CLI on `$PATH`, accessible by root)
- systemd (for the service unit and `sd_notify` integration)
- Linux host (Raspberry Pi OS, Ubuntu, Debian)

## Installation

```bash
# From the repo root, as root:
sudo bash supervisor/install.sh
```

The script is **idempotent**: re-running it installs the current source version without disturbing existing state (`containers.json`, token, backups).

> **BENCH-VERIFY only**: `install.sh` is not run in CI because it requires a live host with Docker and systemd. Verify it on a Pi before shipping.

After installation the service starts automatically:

```bash
systemctl status vanchor-supervisor
journalctl -u vanchor-supervisor -f
```

## Configuration

Settings live in `/etc/vanchor-supervisor/config.json`. All keys are optional; missing keys fall back to defaults.

```json
{
    "listen_host": "127.0.0.1",
    "listen_port": 9300,
    "state_dir": "/var/lib/vanchor-supervisor",
    "install_root": "/opt/vanchor-supervisor",
    "data_volume": "vanchor_data",
    "health_gate_s": 60.0,
    "health_ok_count": 3,
    "health_poll_s": 2.0,
    "backup_retention": 5,
    "disk_warn_pct": 80.0,
    "disk_crit_pct": 92.0,
    "min_free_mb_for_update": 500
}
```

## API

The HTTP API listens on `127.0.0.1:9300` (localhost only, never exposed externally). Every request requires the `X-Supervisor-Token` header.

The token is generated on first start and written to:
- `/var/lib/vanchor-supervisor/token` (host, chmod 600)
- `<data-volume>/supervisor/token` (inside the data volume, chmod 644, readable by the app)

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/status` | Overall status: containers, disk, recent jobs |
| GET | `/v1/containers` | List all configured containers |
| GET | `/v1/containers/<name>` | Single container info + live `docker ps` |
| POST | `/v1/containers/<name>/update` | Start update job `{tag?, bundle_rel?}` |
| POST | `/v1/containers/<name>/rollback` | Start rollback to previous_tag |
| POST | `/v1/containers/<name>/backup` | Start backup job |
| POST | `/v1/containers/<name>/restore` | Start restore job `{backup_id}` |
| GET | `/v1/jobs` | List recent jobs (newest first) |
| GET | `/v1/jobs/<id>` | Single job status |
| GET | `/v1/backups` | List available backups |
| POST | `/v1/prune` | Prune old image tags + dangling layers |
| POST | `/v1/self-update` | Install supervisor bundle `{bundle_rel}` |
| GET | `/v1/disk` | Disk usage snapshot |
| GET | `/v1/devices/<name>` | Device policy check for a container |

All mutating operations are asynchronous: they accept the job immediately (HTTP 202) and return a `job_id`. Poll `GET /v1/jobs/<id>` for completion. If a job is already running, POST returns HTTP 409.

### Example

```bash
TOKEN=$(cat /var/lib/vanchor-supervisor/token)
curl -s -H "X-Supervisor-Token: $TOKEN" http://127.0.0.1:9300/v1/status | python3 -m json.tool
```

## Update and Rollback

### OTA update (pull from registry)

```bash
curl -s -X POST -H "X-Supervisor-Token: $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"tag": "1.5.1"}' \
     http://127.0.0.1:9300/v1/containers/vanchor/update
```

Phases: `verify → backup → load_or_pull → recreate → health_gate → done`

If the health gate fails (no 3 consecutive HTTP 200s on `/api/state` within 60 s), the supervisor automatically rolls back to the previous tag and re-gates. If the rollback gate also fails, the job ends with `error="rollback_unhealthy"` and the system is left in place for manual intervention.

### Manual rollback

```bash
curl -s -X POST -H "X-Supervisor-Token: $TOKEN" \
     http://127.0.0.1:9300/v1/containers/vanchor/rollback
```

### Bundle-based update (for air-gapped / SD-card delivery)

Drop a `.bundle.tar` file into the data volume's `updates/` directory, then:

```bash
curl -s -X POST -H "X-Supervisor-Token: $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"bundle_rel": "updates/vanchor-app-1.5.1-arm64.bundle.tar"}' \
     http://127.0.0.1:9300/v1/containers/vanchor/update
```

## Self-Update and Boot-Count Guard

### Supervisor self-update

```bash
curl -s -X POST -H "X-Supervisor-Token: $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"bundle_rel": "updates/vanchor-supervisor-0.2.0.bundle.tar"}' \
     http://127.0.0.1:9300/v1/self-update
```

This installs the new version into `/opt/vanchor-supervisor/versions/<ver>/`, writes `pending.json`, and atomically flips the `current` symlink. The service is then expected to be restarted (e.g., by systemd `Restart=always`).

### Boot-count guard (`guard.py`)

`guard.py` runs as `ExecStartPre` in the systemd unit, **before** the supervisor Python process starts. It:

1. Reads `pending.json` and increments the `boots` counter.
2. If `boots < 3`: writes the incremented counter back and exits 0 (supervisor starts normally).
3. If `boots >= 3`: flips the `current` symlink back to `pending.previous`, deletes `pending.json`, and exits 0.

On successful startup, the supervisor itself reads `pending.json`, verifies the running version matches, and clears it. This means:
- 3 failed starts → guard reverts to previous version automatically.
- 1 successful start → pending cleared, rollback window closed.

## Trust Boundary

The supervisor API is **localhost-only** (`127.0.0.1`) and requires a token. This is the same trust level as the app's own `/api/restore` endpoint: anyone with shell access on the Pi can read the token file, but remote network access is not possible without explicit port forwarding.

The token is also written to `<data-volume>/supervisor/token` (chmod 644) so the Vanchor app can read it if it needs to surface update controls in the UI.

## Why Root?

The supervisor runs as root because it needs to:
- Invoke `docker` CLI commands (docker socket is root-owned on stock Pi OS)
- Write to `/var/lib/` and `/opt/` for state and installation
- Read/write the systemd drop-in for self-update

This is consistent with other container management tools (Portainer agent, Balena supervisor) on embedded Linux hosts.

## Add-On Readiness

The `containers.json` format supports multiple container entries. Future add-on packs can register additional containers that the supervisor manages alongside `vanchor`. The API is keyed by container name, so add-ons get the same update/backup/rollback machinery for free.
