"""Tests for deploy/image/ tooling and related artifacts (adoption task 6).

All tests are runnable without pi-gen, Docker, or a real SD card.
BENCH-VERIFY items are covered by docs/image-testing.md; these tests check
what IS testable here: syntax, JSON/YAML parse, field presence.
"""
from __future__ import annotations

import configparser
import hashlib
import json
import pathlib
import subprocess
import sys
import textwrap

import pytest
import yaml

REPO = pathlib.Path(__file__).parent.parent
STAGE_ROOT = REPO / "deploy" / "image" / "stage-vanchor"
IMAGE_DIR = REPO / "deploy" / "image"


# ---------------------------------------------------------------------------
# 1. bash -n on every shell script under deploy/image/
# ---------------------------------------------------------------------------

def _collect_sh():
    return list(IMAGE_DIR.rglob("*.sh"))


@pytest.mark.parametrize("script", _collect_sh(), ids=lambda p: str(p.relative_to(REPO)))
def test_shell_syntax(script):
    """bash -n validates syntax without executing."""
    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"bash -n failed for {script}:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# 2. .github/workflows/image.yml parses and has expected structure
# ---------------------------------------------------------------------------

def test_image_workflow_yaml():
    wf = REPO / ".github" / "workflows" / "image.yml"
    assert wf.exists(), "image.yml workflow file not found"
    data = yaml.safe_load(wf.read_text())

    # Triggers
    on = data.get("on", data.get(True, {}))  # yaml parses 'on' as True
    assert "push" in on or "workflow_dispatch" in on, "workflow missing expected triggers"
    if "push" in on:
        tags = on["push"].get("tags", [])
        assert any("v*" in t for t in tags), "push trigger should include v* tags"

    # Both jobs exist
    jobs = data.get("jobs", {})
    assert "bundle" in jobs, "missing 'bundle' job"
    assert "image" in jobs, "missing 'image' job"

    # arm64 runner
    for job_name, job in jobs.items():
        runs_on = job.get("runs-on", "")
        assert "arm" in str(runs_on), f"job {job_name} should run on arm64 runner, got {runs_on!r}"


# ---------------------------------------------------------------------------
# 3. vanchor-setup.nmconnection parses correctly
# ---------------------------------------------------------------------------

def test_nmconnection():
    path = STAGE_ROOT / "01-net" / "files" / "vanchor-setup.nmconnection"
    assert path.exists(), f"nmconnection not found: {path}"
    cfg = configparser.RawConfigParser()
    cfg.optionxform = str  # preserve case
    cfg.read_string(path.read_text())

    assert cfg.get("wifi", "mode") == "ap"
    assert cfg.get("ipv4", "method") == "shared"
    psk = cfg.get("wifi-security", "psk")
    assert len(psk) >= 8, f"PSK too short: {psk!r}"
    priority = cfg.get("connection", "autoconnect-priority")
    assert priority == "-10"


# ---------------------------------------------------------------------------
# 4. Systemd unit files: required fields present
# ---------------------------------------------------------------------------

def test_load_images_service():
    path = STAGE_ROOT / "02-stack" / "files" / "vanchor-load-images.service"
    assert path.exists()
    text = path.read_text()
    assert "ConditionPathExists=!/var/lib/vanchor/.images-loaded" in text
    assert "After=docker.service" in text
    assert "WantedBy=multi-user.target" in text


def test_hotspot_service():
    path = STAGE_ROOT / "01-net" / "files" / "vanchor-hotspot.service"
    assert path.exists()
    text = path.read_text()
    assert "After=NetworkManager.service" in text
    assert "WantedBy=multi-user.target" in text


# ---------------------------------------------------------------------------
# 5. gen_imager_json.py: output has expected keys, sha matches, URL has version
# ---------------------------------------------------------------------------

def test_gen_imager_json(tmp_path):
    # Create a dummy .img.xz file
    fake_img = tmp_path / "vanchor-1.5.0a8-arm64.img.xz"
    fake_img.write_bytes(b"fake image content for testing" * 100)

    # Fake sizes
    extract_size = 4_000_000_000
    extract_sha = "a" * 64  # placeholder

    out_json = tmp_path / "os_list.json"

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "gen_imager_json.py"),
            "--version", "1.5.0a8",
            "--img", str(fake_img),
            "--extract-size", str(extract_size),
            "--extract-sha256", extract_sha,
            "--out", str(out_json),
            "--date", "2026-07-18",
        ],
        capture_output=True, text=True, cwd=str(REPO),
    )
    assert result.returncode == 0, f"gen_imager_json.py failed:\n{result.stderr}"

    data = json.loads(out_json.read_text())
    assert "os_list" in data
    entry = data["os_list"][0]

    # Required fields
    for key in ["name", "description", "url", "extract_size", "extract_sha256",
                "image_download_size", "image_download_sha256", "release_date",
                "init_format", "devices"]:
        assert key in entry, f"Missing key: {key}"

    # sha256 of the fake file matches
    expected_sha = hashlib.sha256(fake_img.read_bytes()).hexdigest()
    assert entry["image_download_sha256"] == expected_sha

    # Version appears in URL
    assert "1.5.0a8" in entry["url"]

    # JSON round-trips
    assert json.loads(json.dumps(data)) == data


# ---------------------------------------------------------------------------
# 6. config.template: required fields present
# ---------------------------------------------------------------------------

def test_config_template():
    path = IMAGE_DIR / "config.template"
    assert path.exists()
    text = path.read_text()
    assert "ENABLE_SSH=0" in text
    assert "@VERSION@" in text
    assert "./stage-vanchor" in text
    # STAGE_LIST ends with ./stage-vanchor
    for line in text.splitlines():
        if line.startswith("STAGE_LIST="):
            assert line.rstrip().endswith("./stage-vanchor\"") or \
                   line.rstrip().endswith("./stage-vanchor"), \
                   f"STAGE_LIST should end with ./stage-vanchor: {line!r}"


# ---------------------------------------------------------------------------
# 7. vanchor-load-images.sh: mentions sha256 and docker load
# ---------------------------------------------------------------------------

def test_load_images_sh_content():
    path = STAGE_ROOT / "02-stack" / "files" / "vanchor-load-images.sh"
    assert path.exists()
    text = path.read_text()
    assert "sha256" in text, "load script should verify sha256"
    assert '"load"' in text or "docker load" in text, "load script should call docker load"
    assert "docker pull" not in text, "load script must not docker pull (zero-network)"
    assert "curl" not in text, "load script must not use curl (zero-network)"
    assert "apt" not in text, "load script must not use apt (zero-network)"


# ---------------------------------------------------------------------------
# 8. daemon.json: valid JSON, correct log driver (addendum: local driver)
# ---------------------------------------------------------------------------

def test_daemon_json():
    path = STAGE_ROOT / "00-docker" / "files" / "daemon.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data.get("log-driver") == "local", \
        "Addendum requires log-driver=local (not json-file)"
    opts = data.get("log-opts", {})
    assert "max-size" in opts
    assert "max-file" in opts


# ---------------------------------------------------------------------------
# 9. Zero-network audit: no curl/docker pull/apt in stage scripts that run on Pi
# ---------------------------------------------------------------------------

BOOT_TIME_SCRIPTS = [
    STAGE_ROOT / "01-net" / "00-run.sh",
    STAGE_ROOT / "01-net" / "01-run-chroot.sh",
    STAGE_ROOT / "02-stack" / "00-run.sh",
    STAGE_ROOT / "02-stack" / "01-run-chroot.sh",
    STAGE_ROOT / "02-stack" / "files" / "vanchor-load-images.sh",
    STAGE_ROOT / "02-stack" / "files" / "vanchor-hotspot-check.sh" if \
        (STAGE_ROOT / "01-net" / "files" / "vanchor-hotspot-check.sh").exists() else
        STAGE_ROOT / "01-net" / "files" / "vanchor-hotspot-check.sh",
    STAGE_ROOT / "03-trim" / "00-run-chroot.sh",
]

# Correct path - the hotspot check script lives in 01-net
BOOT_TIME_SCRIPTS_FIXED = [
    STAGE_ROOT / "01-net" / "00-run.sh",
    STAGE_ROOT / "01-net" / "01-run-chroot.sh",
    STAGE_ROOT / "01-net" / "files" / "vanchor-hotspot-check.sh",
    STAGE_ROOT / "02-stack" / "00-run.sh",
    STAGE_ROOT / "02-stack" / "01-run-chroot.sh",
    STAGE_ROOT / "02-stack" / "files" / "vanchor-load-images.sh",
    STAGE_ROOT / "03-trim" / "00-run-chroot.sh",
]

# 00-docker runs at BUILD time in the pi-gen chroot with network; it's exempt
BUILD_TIME_SCRIPTS = [
    STAGE_ROOT / "00-docker" / "00-run-chroot.sh",
]


@pytest.mark.parametrize("script", BOOT_TIME_SCRIPTS_FIXED,
                          ids=lambda p: str(p.relative_to(REPO)))
def test_zero_network_at_boot(script):
    """Scripts that run on the Pi (not in the pi-gen build chroot) must not
    use network-requiring tools."""
    if not script.exists():
        pytest.skip(f"Script not found: {script}")
    text = script.read_text()
    # Strip comment lines before checking for forbidden commands
    non_comment_lines = [
        ln for ln in text.splitlines()
        if not ln.lstrip().startswith("#")
    ]
    code = "\n".join(non_comment_lines)
    assert "docker pull" not in code, f"{script.name}: must not docker pull"
    assert "curl " not in code and not code.startswith("curl"), \
        f"{script.name}: must not use curl"
    # apt is allowed in *-run-chroot.sh ONLY for the 01-run-chroot (purging modemmanager)
    # but NOT in files/ scripts that run on the live Pi
    if "files/" in str(script):
        assert "apt-get" not in code, \
            f"{script.name}: files/ scripts must not use apt-get"


# ---------------------------------------------------------------------------
# 10. journald and var-log.mount files exist and have expected content
# ---------------------------------------------------------------------------

def test_journald_conf():
    path = STAGE_ROOT / "02-stack" / "files" / "vanchor-journald.conf"
    assert path.exists()
    text = path.read_text()
    assert "Storage=volatile" in text


def test_var_log_mount():
    path = STAGE_ROOT / "02-stack" / "files" / "var-log.mount"
    assert path.exists()
    text = path.read_text()
    assert "tmpfs" in text
    assert "Where=/var/log" in text
