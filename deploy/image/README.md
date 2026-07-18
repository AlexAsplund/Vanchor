# Vanchor-NG SD image builder

Builds a flashable Raspberry Pi SD image (`.img.xz`) containing:

- Raspberry Pi OS Lite arm64 (Bookworm) — stages 0–2 of pi-gen
- Docker CE + docker-compose plugin
- The `vanchor/vanchor` docker image pre-loaded (no first-boot internet required)
- The `vanchor-supervisor` host daemon
- NetworkManager setup hotspot (`vanchor-setup` / `vanchor-boat`)
- SD-write minimisation: volatile journald, tmpfs `/tmp`+`/var/log`, noatime,
  bounded docker logs, zram swap

---

## Prerequisites

### Native arm64 (recommended: GH `ubuntu-24.04-arm`, Pi 5)

- Docker with `--privileged` support (pi-gen's `build-docker.sh` requires it)
- ~15 GB free disk for the pi-gen work directory
- Python 3.11+ (to read version from `pyproject.toml`)
- git

### x86 host (slower: ~2–4 h with emulation)

All of the above, plus:

```bash
sudo apt install -y qemu-user-static
sudo update-binfmts --enable
```

pi-gen detects binfmt and uses it automatically.

---

## Pinned pi-gen commit

```
e4f7df5d4dff5bd3f4af57f06eaccbc2de476882
```

This is the HEAD of the **`arm64` branch** of
`https://github.com/RPi-Distro/pi-gen` as of 2026-07-18. To upgrade:
find the current HEAD of the `arm64` branch, update `PIGEN_SHA` in
`build.sh` and `image.yml`, test, and commit.

---

## Build the update bundle first

The image bakes in the docker image as a bundle. Build the bundle before
calling `build.sh`:

```bash
# 1. Build and save the docker image (arm64)
docker buildx build --platform linux/arm64 -f Dockerfile \
    -t "vanchor/vanchor:$(python3 -c 'import tomllib,pathlib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])')" \
    --load .

VER=$(python3 -c 'import tomllib,pathlib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])')
mkdir -p images
docker save "vanchor/vanchor:$VER" | gzip > "images/image.tar.gz"

# 2. Create the bundle tar (make_bundle.py is stdlib-only)
python3 scripts/make_bundle.py app \
    --image "vanchor/vanchor" \
    --tag "$VER" \
    --min-supervisor "0.1.0" \
    --arch arm64 \
    --image-tar images/image.tar.gz \
    --out "vanchor-update-$VER.bundle.tar"
```

---

## Build the SD image

```bash
# From the repo root:
deploy/image/build.sh \
    --version "$VER" \
    --bundle "vanchor-update-$VER.bundle.tar"
    # optional: --workdir /path/to/scratch (default: ./work/pi-gen-build)
```

Output: `work/pi-gen-build/pi-gen/deploy/vanchor-<version>-arm64.img.xz`

**What `build.sh` does:**
1. Clones pi-gen at the pinned SHA into `workdir/pi-gen/` (skips if present).
2. Substitutes `@VERSION@` in `config.template` → `pi-gen/config`.
3. Copies `stage-vanchor/` into `pi-gen/`.
4. Copies the bundle tar into `stage-vanchor/02-stack/files/factory-bundle.tar`.
5. Runs `pi-gen/build-docker.sh` (privileged Docker container handles the
   chroot cross-compilation).

---

## Honest build times

| Environment | Time estimate |
|---|---|
| GitHub-hosted `ubuntu-24.04-arm` (native arm64) | 40–75 min |
| Pi 5 (native arm64, local) | 50–90 min |
| x86 laptop + qemu-user-static | 2–4 h |

First run will be at the slower end (no caches). Subsequent runs with
`--workdir` pointing at a kept directory reuse the pi-gen clone but re-run
the full pi-gen build (pi-gen doesn't have incremental builds).

---

## Artifacts (from a completed build)

| File | Description |
|---|---|
| `vanchor-<version>-arm64.img.xz` | Flashable image (xz-compressed, ~0.9–1.2 GB) |
| `vanchor-<version>-arm64.img.xz.sha256` | Download checksum |
| `vanchor-update-<version>.bundle.tar` | Docker image bundle (sideload + factory source) |
| `vanchor-update-<version>.bundle.tar.sha256` | Bundle checksum |
| `os_list.json` | Raspberry Pi Imager custom repository JSON |
| `size-report.txt` | Rootfs df + image size vs budget |

---

## Size budget (16 GB card ≈ 14.4 GiB usable)

| Component | Estimate |
|---|---|
| RPi OS Lite arm64 rootfs (stage0-2, after trim) | ~2.0 GB |
| docker-ce + containerd + cli + compose plugin | ~0.4 GB |
| vanchor image loaded layers (python:3.12-slim ~130 MB + deps 184 MB + app ~15 MB) | ~0.45 GB |
| Factory bundle at `/opt/vanchor/factory/` | ~0.16 GB |
| Supervisor + compose + units | ~0.03 GB |
| **Used after first boot** | **~3.1–3.5 GB** |
| **`.img.xz` download** | **~0.9–1.2 GB** |
| **Free on a 16 GB card** | **~10.5–11 GB** |

CI prints actuals in `size-report.txt`.

---

## stage-vanchor layout

```
stage-vanchor/
  prerun.sh              pi-gen boilerplate (copy_previous if no rootfs)
  EXPORT_IMAGE           marker: export this stage as a bootable image
  00-docker/
    00-run.sh            install daemon.json (bounded log driver)
    00-run-chroot.sh     docker-ce apt repo + install + systemctl enable
    files/daemon.json    log-driver=local, max-size=5m, max-file=2
  01-net/
    00-packages          avahi-daemon
    00-run.sh            install NM connection + dnsmasq + hotspot files
    01-run-chroot.sh     wifi country, purge modemmanager, enable hotspot svc
    files/
      vanchor-setup.nmconnection   WPA2-PSK AP profile, priority -10
      vanchor-dnsmasq.conf         address=/vanchor.local/10.42.0.1
      vanchor-hotspot.service      systemd oneshot fallback
      vanchor-hotspot-check.sh     25 s sleep then nmcli up if no active conn
  02-stack/
    00-run.sh            copy compose + supervisor + bundle + units into rootfs
    01-run-chroot.sh     enable units, add groups, noatime, zram, disable SD swap
    files/
      factory-bundle.tar       PLACEHOLDER (build.sh copies the real bundle here)
      vanchor-load-images.service   first-boot oneshot (condition: stamp absent)
      vanchor-load-images.sh        sha256-verify + docker load from bundle
      motd                          console note (hotspot creds, SSH off)
      vanchor-journald.conf         Storage=volatile + 32 MB cap
      var-log.mount                 /var/log on tmpfs (64 MB)
  03-trim/
    00-run-chroot.sh     apt clean, doc/man purge, df capture → size-report.txt
```
