"""Tests for CliDockerBackend — argv construction without shelling out."""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from vanchor_supervisor.backends import CliDockerBackend, DockerError


# ------------------------------------------------------------------ #
# Recording runner
# ------------------------------------------------------------------ #

class RecordingRunner:
    """Captures calls to subprocess.run and returns scripted results."""

    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.calls: list[list[str]] = []
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        return type("Result", (), {
            "returncode": self._returncode,
            "stdout": self._stdout,
            "stderr": self._stderr,
        })()


# ------------------------------------------------------------------ #
# run() arg construction
# ------------------------------------------------------------------ #

VANCHOR_ENTRY = {
    "name": "vanchor",
    "image": "ghcr.io/alexasplund/vanchor",
    "tag": "1.5.0a8",
    "network": "host",
    "env": {"VANCHOR_HOST": "0.0.0.0", "VANCHOR_DATA_DIR": "/data"},
    "volumes": [
        {"volume": "vanchor_data", "target": "/data"},
        {"host": "/dev", "target": "/dev", "ro": True},
    ],
    "device_cgroup_rules": ["c 166:* rmw", "c 188:* rmw", "c 204:* rmw", "c 89:* rmw"],
    "devices": ["/dev/gpiochip0"],
    "restart": "unless-stopped",
}


def test_run_includes_network_host():
    recorder = RecordingRunner()
    backend = CliDockerBackend(runner=recorder)
    backend.run(VANCHOR_ENTRY)
    args = recorder.calls[-1]
    assert "--network" in args
    assert "host" in args


def test_run_includes_env_vars():
    recorder = RecordingRunner()
    backend = CliDockerBackend(runner=recorder)
    backend.run(VANCHOR_ENTRY)
    args = recorder.calls[-1]
    cmd = " ".join(args)
    assert "VANCHOR_HOST=0.0.0.0" in cmd
    assert "VANCHOR_DATA_DIR=/data" in cmd


def test_run_includes_named_volume():
    recorder = RecordingRunner()
    backend = CliDockerBackend(runner=recorder)
    backend.run(VANCHOR_ENTRY)
    args = recorder.calls[-1]
    # Named volume: -v vanchor_data:/data
    assert any("vanchor_data:/data" in a for a in args)


def test_run_includes_dev_bind_ro():
    recorder = RecordingRunner()
    backend = CliDockerBackend(runner=recorder)
    backend.run(VANCHOR_ENTRY)
    args = recorder.calls[-1]
    assert any("/dev:/dev:ro" in a for a in args)


def test_run_includes_cgroup_rules():
    recorder = RecordingRunner()
    backend = CliDockerBackend(runner=recorder)
    backend.run(VANCHOR_ENTRY)
    args = recorder.calls[-1]
    # All 4 cgroup rules
    for rule in VANCHOR_ENTRY["device_cgroup_rules"]:
        assert rule in args, f"Missing cgroup rule {rule!r} in {args}"


def test_run_includes_image_tag():
    recorder = RecordingRunner()
    backend = CliDockerBackend(runner=recorder)
    backend.run(VANCHOR_ENTRY)
    args = recorder.calls[-1]
    assert "ghcr.io/alexasplund/vanchor:1.5.0a8" in args


def test_run_skips_missing_device(tmp_path):
    """Devices that don't exist on the host must be skipped (not fatal)."""
    recorder = RecordingRunner()
    backend = CliDockerBackend(runner=recorder)
    entry = dict(VANCHOR_ENTRY, devices=["/dev/nonexistent_device_12345"])
    backend.run(entry)
    args = recorder.calls[-1]
    assert "/dev/nonexistent_device_12345" not in args


def test_run_restart_policy():
    recorder = RecordingRunner()
    backend = CliDockerBackend(runner=recorder)
    backend.run(VANCHOR_ENTRY)
    args = recorder.calls[-1]
    assert "--restart" in args
    assert "unless-stopped" in args


# ------------------------------------------------------------------ #
# DockerError on non-zero return code
# ------------------------------------------------------------------ #

def test_docker_error_on_nonzero():
    runner = RecordingRunner(returncode=1, stderr=b"container not found")
    backend = CliDockerBackend(runner=runner)
    with pytest.raises(DockerError):
        backend.stop("vanchor")


# ------------------------------------------------------------------ #
# inspect JSON parse
# ------------------------------------------------------------------ #

def test_inspect_parses_json():
    container_data = [{"Name": "/vanchor", "State": {"Status": "running"}}]
    runner = RecordingRunner(stdout=json.dumps(container_data).encode())
    backend = CliDockerBackend(runner=runner)
    result = backend.inspect("vanchor")
    assert result is not None


def test_inspect_none_when_missing():
    runner = RecordingRunner(returncode=1, stderr=b"No such container")
    backend = CliDockerBackend(runner=runner)
    result = backend.inspect("nonexistent")
    assert result is None
