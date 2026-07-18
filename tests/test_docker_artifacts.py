"""Drift guards for the Docker container-runtime artifacts.

No docker daemon needed — these parse the YAML/Dockerfile/dockerignore files
at the repo root and assert they meet the spec from task-5-brief.md §4.
"""
from __future__ import annotations
import re
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent


# ------------------------------------------------------------------ #
# docker-compose.yml
# ------------------------------------------------------------------ #

@pytest.fixture(scope="module")
def compose() -> dict:
    yaml = pytest.importorskip("yaml")
    text = (REPO / "docker-compose.yml").read_text()
    return yaml.safe_load(text)


def test_compose_network_mode_host(compose):
    svc = compose["services"]["vanchor"]
    assert svc["network_mode"] == "host", "network_mode must be 'host' for mDNS"


def test_compose_restart_policy(compose):
    svc = compose["services"]["vanchor"]
    assert svc["restart"] == "unless-stopped"


def test_compose_dev_bind_ro(compose):
    svc = compose["services"]["vanchor"]
    vols = svc.get("volumes", [])
    assert "/dev:/dev:ro" in vols, "read-only /dev bind required for hotplug"


def test_compose_volume_target_data(compose):
    svc = compose["services"]["vanchor"]
    vols = svc.get("volumes", [])
    # One of the volume entries must target /data (can be named or bind)
    assert any("/data" in str(v) for v in vols), "volume target /data required"


def test_compose_cgroup_rules_exact(compose):
    svc = compose["services"]["vanchor"]
    rules = svc.get("device_cgroup_rules", [])
    expected = {
        "c 166:* rmw",
        "c 188:* rmw",
        "c 204:* rmw",
        "c 89:* rmw",
    }
    assert set(rules) == expected, f"Expected cgroup rules {expected!r}, got {rules!r}"


def test_compose_bounded_logging(compose):
    """SD-card wear: container logging must be bounded (local driver, 2 x 5 MB)."""
    svc = compose["services"]["vanchor"]
    logging_cfg = svc.get("logging")
    assert logging_cfg, "logging section required to bound log growth"
    assert logging_cfg["driver"] == "local"
    opts = logging_cfg.get("options", {})
    assert opts.get("max-size") == "5m"
    assert str(opts.get("max-file")) == "2"


# ------------------------------------------------------------------ #
# Dockerfile
# ------------------------------------------------------------------ #

@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return (REPO / "Dockerfile").read_text()


def test_dockerfile_healthcheck_hits_api_state(dockerfile_text):
    assert "HEALTHCHECK" in dockerfile_text
    assert "/api/state" in dockerfile_text


def test_dockerfile_slim_bookworm_in_both_from_lines(dockerfile_text):
    from_lines = [ln for ln in dockerfile_text.splitlines() if ln.startswith("FROM")]
    assert len(from_lines) >= 2, "Expected multi-stage build"
    for line in from_lines:
        assert "slim-bookworm" in line, f"FROM line missing slim-bookworm: {line!r}"


def test_dockerfile_final_stage_apt_only_allowed_packages(dockerfile_text):
    # The final stage may only install explicitly allowed OS packages.
    # Adoption task 6 adds network-manager (provides nmcli for WiFi setup).
    # Split on FROM: the last FROM block is the final stage.
    blocks = dockerfile_text.split("FROM ")
    final_stage = blocks[-1]
    if "apt-get" in final_stage:
        assert "network-manager" in final_stage, (
            "Final stage apt-get must only install network-manager (nmcli for WiFi); "
            "found unexpected apt-get usage"
        )


def test_dockerfile_data_dir_env(dockerfile_text):
    assert "VANCHOR_DATA_DIR=/data" in dockerfile_text


def test_dockerfile_healthcheck_uses_python_urllib(dockerfile_text):
    # Must use python urllib, not curl (curl not installed)
    assert "urllib" in dockerfile_text or "python" in dockerfile_text.lower()


# ------------------------------------------------------------------ #
# .dockerignore
# ------------------------------------------------------------------ #

@pytest.fixture(scope="module")
def dockerignore_text() -> str:
    p = REPO / ".dockerignore"
    assert p.exists(), ".dockerignore must exist"
    return p.read_text()


def test_dockerignore_excludes_venv(dockerignore_text):
    assert ".venv" in dockerignore_text


def test_dockerignore_excludes_data_dir(dockerignore_text):
    assert "vanchor_data" in dockerignore_text


def test_dockerignore_excludes_git(dockerignore_text):
    assert ".git" in dockerignore_text
