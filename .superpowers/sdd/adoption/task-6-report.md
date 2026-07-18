# Task 6 Report — Flashable SD Image

**Branch:** `dev/adoption-pack`  
**Based on:** HEAD `939b7ce` (task-5 fix)  
**Date:** 2026-07-18

---

## Task-5 Reconciliation

The brief assumed task-5 artifacts at `deploy/docker/` and `deploy/supervisor/`.
Actual paths:

| Brief path | Actual path | Resolution |
|---|---|---|
| `deploy/docker/Dockerfile` | `Dockerfile` (root) | `build.sh` and `image.yml` reference `REPO_DIR/Dockerfile` |
| `deploy/docker/docker-compose.yml` | `docker-compose.yml` (root) | `02-stack/00-run.sh` copies `${REPO_DIR}/docker-compose.yml` |
| `deploy/supervisor/` | `supervisor/` (root) | `02-stack/00-run.sh` copies `${REPO_DIR}/supervisor/.` |
| `deploy/supervisor/vanchor-supervisor.service` | `supervisor/vanchor-supervisor.service` | `01-run-chroot.sh` copies from `/opt/vanchor-supervisor/` |
| Supervisor API port `9123` | `9300` | deploy-pi.md and checklist use `9300` |
| Bundle format: `images/<versioned>.tar.gz` | `image.tar.gz` (fixed name) + `manifest.json` with `image_sha256` | `vanchor-load-images.sh` adapted to task-5 format |

### Requirements imposed on task-5 files (§2 — all done)

1. **`network-manager` in Dockerfile** — added to the final stage with `apt-get install -y --no-install-recommends network-manager`. Updated `test_docker_artifacts.py` test that asserted no apt-get in final stage.
2. **`network_mode: host`** — already present from task 5.
3. **`VANCHOR_HOST: 0.0.0.0`** — already present from task 5.
4. **D-Bus socket bind-mount** — added to `docker-compose.yml`: `/run/dbus/system_bus_socket:/run/dbus/system_bus_socket`. BENCH-VERIFY on real Pi hardware.

---

## DONE Checklist

- [x] `deploy/image/` tree exists exactly as §4; all scripts `bash -n` clean; pi-gen SHA pinned in `build.sh` + `image.yml`
- [x] Factory bundle flow is ZERO-network at boot: no `curl`/`apt`/`docker pull` in any stage script that runs on the Pi (zero-network test in `test_image_tooling.py` covers this)
- [x] `src/vanchor/wifi.py` (15 async def, `_split_terse`, graceful no-op) + 3 endpoints + `wifi.js` + panel card wired
- [x] `node --check` clean on `wifi.js`; shell-manifest test green (`wifi.js` in `sw.js` SHELL array + `index.html` script tag)
- [x] `scripts/gen_imager_json.py` emits valid os_list JSON (tested in `test_image_tooling.py::test_gen_imager_json`)
- [x] `.github/workflows/image.yml` parses, triggers on `v*` + dispatch, uses `ubuntu-24.04-arm`, uploads all §7 assets, BENCH-VERIFY header comment
- [x] `tests/test_wifi.py` (49 cases) + `tests/test_image_tooling.py` pass; full suite 2009 passed
- [x] `docs/image-testing.md` covers 21 checklist items (≥ 15 required by §10.1 + SD-write addendum)
- [x] `docs/deploy-pi.md` rewritten (docker/image primary, bare-metal in Appendix A, Appendix B for SD-write debugging); README + getting-started touched; CHANGELOG entry added
- [x] Every non-verifiable behaviour has a BENCH-VERIFY marker; see below
- [x] Task-5 interface reconciled; all §2 requirements added to compose/Dockerfile; deltas noted above
- [x] One commit, session trailers, no pushes/tags

---

## Owner Addendum (2026-07-18): SD-Write Minimization

| Item | Implementation |
|---|---|
| Docker logging | `daemon.json`: `log-driver=local`, `max-size=5m`, `max-file=2` (overrides brief's `json-file`; matches addendum). Per-container compose config already set (task 5). |
| journald | `02-stack/files/vanchor-journald.conf` → `/etc/systemd/journald.conf.d/50-vanchor.conf`: `Storage=volatile`, `SystemMaxUse=32M`, `ForwardToConsole=no`. |
| tmpfs /var/log | `02-stack/files/var-log.mount`: systemd mount unit, tmpfs `size=64M,noatime`. `/tmp` is on tmpfs by default in Bookworm systemd. |
| noatime | `02-stack/01-run-chroot.sh`: `sed -i '/ext4/ s/defaults/defaults,noatime/' /etc/fstab`. BENCH-VERIFY: fstab edit verified on a built image. |
| No SD swap | `02-stack/01-run-chroot.sh`: `systemctl disable dphys-swapfile`; install `zram-tools`, set `PERCENT=25`, `ALGO=lz4`. Addendum overrides brief note about keeping dphys-swapfile for Zero 2 W. |
| App-writer audit | Documented in `docs/image-testing.md` checklist item 20 with cadence table. Follow-up: unbounded `server.log` growth should be filed as a separate issue if confirmed. |
| MB/day estimate | Estimated ≤ 50 MB/day typical fishing day (8 h): docker logs ≤ 10, server.log ≤ 5, blackbox ring ≤ 20, depth chart ≤ 10, OS writes ≤ 5. Debug recorder: 0 (opt-in only). BENCH-VERIFY with `iostat` on checklist item 20. |

---

## BENCH-VERIFY Items

All require real Raspberry Pi hardware:

1. Hotspot NM autoconnect timing (25 s sleep in `vanchor-hotspot-check.sh`)
2. polkit vs uid-0-in-container D-Bus NM access (nmcli from inside container)
3. First-boot image load (`vanchor-load-images.service`, ~30–60 s on Pi 4)
4. Root fs auto-expansion (`init_resize` in `cmdline.txt`)
5. Raspberry Pi Imager customisation dialog (`init_format: systemd` path)
6. CI workflow execution (`image.yml` on first `v*` tag push)
7. pi-gen arm64 branch SHA correctness at build time
8. os_list field names compatibility with current rpi-imager version
9. noatime fstab edit surviving pi-gen image assembly (PARTUUID substitution)
10. zram-tools swap visible (`swapon --show /dev/zram0`)
11. dphys-swapfile disabled (no `/var/swap` on SD)
12. D-Bus socket bind-mount + polkit on Pi 5 Bookworm

---

## Size Budget (Estimated vs Actual)

Estimated from the brief:

| Component | Estimate |
|---|---|
| RPi OS Lite arm64 (after trim) | ~2.0 GB |
| Docker CE + plugins | ~0.4 GB |
| vanchor image layers | ~0.45 GB |
| Factory bundle | ~0.16 GB |
| Supervisor + misc | ~0.03 GB |
| **Total used** | **~3.1–3.5 GB** |
| **img.xz download** | **~0.9–1.2 GB** |
| **Free on 16 GB** | **~10.5–11 GB** |

Actuals: CI `size-report.txt` (first `v*` tag build) will provide measured values. Add noatime and SD-wear items add negligible size.

---

## Test Summary

```
2009 passed, 6 skipped, 10 deselected
tests/test_wifi.py: 49 cases (split_terse, scan, status, join, endpoints)
tests/test_image_tooling.py: 17 cases (bash -n, YAML parse, nmconnection, units, gen_imager_json, config.template, load-images content, zero-network audit, daemon.json, journald, var-log.mount)
ruff check src tests: all checks passed
node --check wifi.js: OK
```

---

## Files Created / Modified

**New files:**
- `deploy/image/config.template`
- `deploy/image/build.sh`
- `deploy/image/README.md`
- `deploy/image/stage-vanchor/prerun.sh`
- `deploy/image/stage-vanchor/EXPORT_IMAGE`
- `deploy/image/stage-vanchor/00-docker/00-run.sh`
- `deploy/image/stage-vanchor/00-docker/00-run-chroot.sh`
- `deploy/image/stage-vanchor/00-docker/files/daemon.json`
- `deploy/image/stage-vanchor/01-net/00-packages`
- `deploy/image/stage-vanchor/01-net/00-run.sh`
- `deploy/image/stage-vanchor/01-net/01-run-chroot.sh`
- `deploy/image/stage-vanchor/01-net/files/vanchor-setup.nmconnection`
- `deploy/image/stage-vanchor/01-net/files/vanchor-dnsmasq.conf`
- `deploy/image/stage-vanchor/01-net/files/vanchor-hotspot.service`
- `deploy/image/stage-vanchor/01-net/files/vanchor-hotspot-check.sh`
- `deploy/image/stage-vanchor/02-stack/00-run.sh`
- `deploy/image/stage-vanchor/02-stack/01-run-chroot.sh`
- `deploy/image/stage-vanchor/02-stack/files/motd`
- `deploy/image/stage-vanchor/02-stack/files/vanchor-load-images.service`
- `deploy/image/stage-vanchor/02-stack/files/vanchor-load-images.sh`
- `deploy/image/stage-vanchor/02-stack/files/vanchor-journald.conf`
- `deploy/image/stage-vanchor/02-stack/files/var-log.mount`
- `deploy/image/stage-vanchor/03-trim/00-run-chroot.sh`
- `scripts/gen_imager_json.py`
- `.github/workflows/image.yml`
- `src/vanchor/wifi.py`
- `src/vanchor/ui/static/wifi.js`
- `docs/image-testing.md`
- `docs/deploy-pi.md` (rewrite)
- `tests/test_wifi.py`
- `tests/test_image_tooling.py`
- `.superpowers/sdd/adoption/task-6-report.md`

**Modified files:**
- `Dockerfile` — add network-manager to final stage
- `docker-compose.yml` — add D-Bus socket bind-mount
- `src/vanchor/ui/server.py` — 3 wifi endpoints
- `src/vanchor/ui/partials/panel-data.html` — WiFi card
- `src/vanchor/ui/static/index.html` — wifi.js script tag
- `src/vanchor/ui/static/sw.js` — wifi.js in SHELL array
- `tests/test_docker_artifacts.py` — update final-stage apt-get test
- `CHANGELOG.md` — task 6 entry
- `README.md` — boat Pi one-liner + paste-URL
- `docs/getting-started.md` — deploy-pi.md link text update

---

## Known Risks / Open Items

1. **pi-gen arm64 branch vs unified master**: SHA `e4f7df5d4dff5bd3f4af57f06eaccbc2de476882` pinned from 2026-07-18. Verify it builds on the first CI run.
2. **rpi-imager os_list field drift**: `gen_imager_json.py` isolates the schema in one function — easy to update if field names change.
3. **GH arm64 runner disk**: `df` guard step fails fast if < 12 GB; cleanup step removes optional toolchains. First run needs monitoring.
4. **Single-radio hotspot drop**: UI copy warns the user; no seamless handover possible.
5. **python-zeroconf vs avahi name conflict**: non-fatal warning; documented in checklist item 15.
6. **noatime + dphys-swapfile disable on Zero 2 W**: addendum takes precedence over the brief's note; zram is the replacement. Zero 2 W with 512 MB RAM gets 25% zram (~128 MB compressed swap). May need tuning.
7. **App-level writer bounds**: server.log rotation is log-level configured but not explicitly bounded by file size in this task; follow-up issue warranted if confirmed unbounded.

---

## Fix pass

Three reviewer findings applied (2026-07-18):

1. **PSK argv doc note** — code comment added to `src/vanchor/wifi.py` at the
   `cmd += ["password", psk]` call explaining that `nmcli device wifi connect`
   does not accept the password via stdin or file descriptor (argv is the only
   option). User-facing notes added to `docs/deploy-pi.md` (new blockquote
   after the security-posture callout) and `docs/image-testing.md` (new bullet
   in "Notes on known behaviour").

2. **Exclusive apt allowlist test** — `test_dockerfile_final_stage_apt_only_allowed_packages`
   in `tests/test_docker_artifacts.py` rewritten to parse all tokens from
   `apt-get install` lines in the final Dockerfile stage, strip flags, and
   assert the resulting package set is a subset of `ALLOWED = {"network-manager"}`.
   Previously only checked for presence of `network-manager`; now rejects any
   additional package not on the allowlist.

3. **D-Bus socket pin test** — new `test_compose_dbus_socket_bind_mount` added
   to `tests/test_docker_artifacts.py` asserting
   `/run/dbus/system_bus_socket:/run/dbus/system_bus_socket` appears in the
   `vanchor` service volumes. Docstring explains the silent-failure mode.

Test run: `2010 passed, 6 skipped, 10 deselected` (full suite).
Targeted run: `64 passed` (test_docker_artifacts + test_wifi + test_image_tooling).

---

## Final-review fix pass

Consolidated fixes applied 2026-07-18 (+ A-series addendum from parallel security review):

**S1 — demo inertness (CRITICAL):** `Runtime.__init__` now auto-applies `apply_demo_mode()` whenever `config.demo.enabled is True` and the posture hasn't already been forced (guard: `not hardware.enabled and not nmea_tcp.enabled and data_dir starts with tmpdir`). Preserves existing `readonly` flag. Tests: `test_runtime_auto_applies_demo_mode_from_yaml`, `test_runtime_demo_already_applied_not_reapplied`. FIXED.

**S1b — demo thrust guard (CRITICAL):** `_run_demo_scenario` now returns early if `abs(state.motor_command.thrust) > 0.05` (hand on throttle). Test: `test_demo_scenario_skips_if_driving`. FIXED.

**S2 — supervisor update interlock (IMPORTANT):** `supervisor_proxy` in `server.py` returns 409 with `error="underway"` for `v1/update/apply`, `v1/rollback`, `v1/restore` when `runtime.state.mode != "manual"`, unless body contains `force=true`. `supervisor.js` `_handleUnderway()` helper offers force-override confirm dialog for apply and rollback. Tests: `test_supervisor_interlock_409_when_anchored`, `test_supervisor_interlock_allowed_in_manual`, `test_supervisor_interlock_force_overrides`, `test_supervisor_interlock_does_not_block_backup`. FIXED.

**I1 — provision_token missing (CRITICAL):** Implemented `provision_token(state_dir, volume_mp)` in `api.py` using `secrets.token_hex(32)`, chmod 0o600, idempotent. Tests: `test_provision_token_creates_file`, `test_provision_token_idempotent`, `test_provision_token_perms_600`, `test_main_module_imports_cleanly`. FIXED.

**I2 — container not started on first boot (CRITICAL):** Added `ensure_running()` to `SupervisorCore`; called in `__main__.py` after core creation. Checks `list_repo_tags()` for local image; starts absent/exited containers; warns and skips if image missing. Tests: `test_ensure_running_starts_absent_container`, `test_ensure_running_no_op_for_running_container`, `test_ensure_running_skips_missing_image`. FIXED.

**I3 — containers.json tag hardcoded (DOWNGRADED):** `vanchor-load-images.sh` now seeds `/var/lib/vanchor-supervisor/containers.json` from manifest `image`+`app_version` if absent. Entry includes dbus socket mount. Tests: bash -n syntax test passes. FIXED.

**I4 — enable before install (IMPORTANT):** `01-run-chroot.sh` reordered: `install.sh` now runs before `systemctl enable`. Dropped `|| true` on enable. `install.sh` fixed to use `SCRIPT_DIR` (not `REPO_ROOT`) for chroot compat; removed `systemctl daemon-reload/enable` from install.sh (caller's responsibility). Tests: `test_install_sh_uses_script_dir`, `test_install_sh_nested_layout`, `test_chroot_enable_after_install`. FIXED.

**I5 — CHANGELOG demo entry under wrong tag (MINOR):** Demo mode entry moved from `[1.5.0a8]` to `[Unreleased]`. FIXED.

**I6 — stale "writes nothing" comments (MINOR):** `app.py` ~1623 and `probe.py:6` updated to mention `MOTOR_INFO_CMD` as the second sanctioned write. FIXED.

**A1 — _DEFAULT_CONTAINERS image/dbus mismatch (HIGH):** Changed image repo to `"vanchor/vanchor"` (matches factory bundle CI). Added `/run/dbus/system_bus_socket` bind-mount. `_APP_IMAGE_REPO` constant defined. `test_supervisor_core.py` fixtures updated. Tests: `test_default_containers_has_dbus_mount`, `test_default_containers_device_cgroup_rules_match_compose`. FIXED.

**A2 — _self_update path traversal (MEDIUM):** Added same `relative_to(mp_resolved)` containment check as `_update_inspect`/`_update_apply`. Test: `test_self_update_traversal_rejected`. FIXED.

**A3 — backup_id unsanitized glob (LOW):** Added `re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,127}", backup_id)` guard before glob in `_download_backup`. Tests: `test_download_backup_rejects_wildcard`, `test_download_backup_rejects_dotdot`. FIXED.

**A4 — sha256 trust model not documented (DOC):** Added honest blockquote to `docs/deploy-pi.md` under Sideload section explaining what sha256 protects (transport corruption, not compromised CI) and that no signature exists yet. FIXED.

**A5:** Subsumed by A1. N/A.

Test run: **2032 passed, 6 skipped, 10 deselected** (full suite). ruff: clean. node --check: clean.
