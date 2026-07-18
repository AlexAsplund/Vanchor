"""Real-Docker integration tests for the supervisor CliDockerBackend + core.

These tests require a running docker daemon and are excluded from the default
suite.  Run them with::

    pytest -m docker tests/test_docker_integration.py

HYGIENE
-------
* Every container created by these tests is named ``vanchor-sup-test-*``.
* Every image uses the local repo ``vanchor-sup-test/*``.
* Every volume is named ``vanchor-sup-test-*``.
* Images carry the label ``vanchor.sup.test=true``.
* Cleanup removes ONLY resources created here (by label / name prefix);
  unrelated containers (e.g. ``vanchor-kicad``) are never touched.
* ``docker system prune`` is never called.

DESIGN NOTE — cross-test prune isolation
-----------------------------------------
After a successful update, SupervisorCore prunes all tags of the container's
image repository except the current and previous tags.  To prevent tests from
clobbering each other's images via prune, each test that exercises the full
update pipeline uses its own local image repository:

  vanchor-sup-test/app        — backend unit tests (ps/run/stop)
  vanchor-sup-test/app-pass   — health-gate-pass test (v1 → v2)
  vanchor-sup-test/app-fail   — health-gate-fail + rollback test (v-ok → v-bad)
"""
from __future__ import annotations
import io
import subprocess
import sys
import tarfile
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.docker

# ---------------------------------------------------------------------------
# Constants – all test resources use this prefix / label
# ---------------------------------------------------------------------------

CONTAINER_NAME = "vanchor-sup-test-app"
VOLUME_NAME = "vanchor-sup-test-data"
HEALTH_PORT = 39873   # ephemeral; unlikely to conflict
TEST_LABEL = "vanchor.sup.test=true"

# Per-test image repos to avoid cross-test prune interference
REPO_BASIC = "vanchor-sup-test/app"           # backend unit tests
REPO_PASS = "vanchor-sup-test/app-pass"        # health-gate-pass test
REPO_FAIL = "vanchor-sup-test/app-fail"        # rollback test


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------

def _docker(*args: str, check: bool = True) -> str:
    """Run a docker CLI command; raise on non-zero if check=True."""
    result = subprocess.run(
        ["docker"] + list(args), capture_output=True, text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {args!r} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _healthy_script() -> str:
    return textwrap.dedent(f"""\
        import http.server

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        http.server.HTTPServer(("0.0.0.0", {HEALTH_PORT}), H).serve_forever()
    """)


def _unhealthy_script() -> str:
    return textwrap.dedent(f"""\
        import http.server

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(503)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"no")

            def log_message(self, *a):
                pass

        http.server.HTTPServer(("0.0.0.0", {HEALTH_PORT}), H).serve_forever()
    """)


def _build_image(full_ref: str, script: str) -> None:
    """Build a stub image using an in-memory tar context (no temp dirs needed).

    The Dockerfile COPYs a Python script file so the script can use normal
    indentation without one-liner/eval tricks.
    """
    dockerfile = textwrap.dedent(f"""\
        FROM python:3.12-alpine
        LABEL {TEST_LABEL}
        COPY server.py /server.py
        CMD ["python3", "/server.py"]
    """).encode()
    script_bytes = script.encode()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in [("Dockerfile", dockerfile), ("server.py", script_bytes)]:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)

    result = subprocess.run(
        ["docker", "build", "--label", TEST_LABEL, "-t", full_ref, "-"],
        input=buf.read(),
        capture_output=True,
    )
    assert result.returncode == 0, (
        f"Failed to build {full_ref}:\n{result.stderr.decode()}"
    )


def _wait_for_job(core, job: dict, timeout: float = 30.0) -> dict:
    """Poll SupervisorCore until the job reaches done/failed, or pytest.fail."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        j = core.get_job(job["id"])
        if j and j.get("phase") in ("done", "failed"):
            return j
        time.sleep(0.5)
    j = core.get_job(job["id"])
    pytest.fail(f"Job {job['id'][:8]} timed out; last state: {j}")


# ---------------------------------------------------------------------------
# Session-scoped fixtures — build / teardown once per pytest run
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def stub_images():
    """Build all stub images from python:3.12-alpine (already local).

    Repos are kept isolated so that each test's post-update prune cannot
    accidentally remove another test's images.
    """
    images_to_build = [
        # Backend unit tests
        (f"{REPO_BASIC}:latest",   _healthy_script()),
        # Health-gate-pass test — two healthy "versions" of the same repo
        (f"{REPO_PASS}:v1",        _healthy_script()),
        (f"{REPO_PASS}:v2",        _healthy_script()),
        # Rollback test — healthy v-ok + unhealthy v-bad in the same repo
        (f"{REPO_FAIL}:v-ok",      _healthy_script()),
        (f"{REPO_FAIL}:v-bad",     _unhealthy_script()),
    ]
    for ref, script in images_to_build:
        _build_image(ref, script)

    yield

    # Cleanup: only images we created (by repo name; never touch unrelated images)
    for ref, _ in images_to_build:
        subprocess.run(["docker", "rmi", "-f", ref], capture_output=True)


@pytest.fixture(scope="session")
def test_volume():
    """Create a named docker volume for volume-mountpoint tests."""
    _docker("volume", "create", "--label", TEST_LABEL, VOLUME_NAME)
    yield VOLUME_NAME
    subprocess.run(["docker", "volume", "rm", "-f", VOLUME_NAME], capture_output=True)


# ---------------------------------------------------------------------------
# Per-test fixture — remove test container before/after each test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_container():
    """Remove the test container before and after each test."""
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)
    yield
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)


# ---------------------------------------------------------------------------
# Core / backend factory helpers
# ---------------------------------------------------------------------------

class _LocalCliDockerBackend:
    """CliDockerBackend wrapper for integration tests.

    Two overrides from the production backend:
    - pull()  → no-op (images are pre-built locally; no registry needed).
    - stop()  → uses a 2-second grace period (-t 2) so the rollback cycle
                 completes in reasonable time; docker's default is 10 seconds.
    """

    def __init__(self):
        sys.path.insert(0, str(Path(__file__).parent.parent / "supervisor"))
        from vanchor_supervisor.backends import CliDockerBackend, DockerError
        self._inner = CliDockerBackend()
        self._DockerError = DockerError

    def pull(self, image: str, tag: str) -> None:
        """No-op — images are already built locally."""

    def stop(self, name: str) -> None:
        """docker stop -t 2 <name> — short grace period for test efficiency."""
        result = subprocess.run(
            ["docker", "stop", "-t", "2", name],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise self._DockerError(
                f"docker stop -t 2 {name!r} failed: {result.stderr.strip()}"
            )

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _make_core(
    tmp_path: Path,
    image: str,
    tag: str,
    *,
    health_gate_s: float = 8.0,
) -> tuple:
    """Build a SupervisorCore with the real CliDockerBackend."""
    sys.path.insert(0, str(Path(__file__).parent.parent / "supervisor"))
    from vanchor_supervisor.core import SupervisorCore
    from vanchor_supervisor.config import SupervisorSettings

    settings = SupervisorSettings()
    settings.state_dir = str(tmp_path / "state")
    settings.data_volume = VOLUME_NAME
    settings.health_gate_s = health_gate_s
    settings.health_ok_count = 2
    settings.health_poll_s = 0.5
    settings.backup_retention = 5

    backend = _LocalCliDockerBackend()
    core = SupervisorCore(settings, backend)

    entry = {
        "name": CONTAINER_NAME,
        "image": image,
        "tag": tag,
        "previous_tag": None,
        "network": "host",
        "restart": "no",        # never auto-restart in tests
        "env": {},
        "volumes": [],          # no volume mount for update/health-gate tests
        "device_cgroup_rules": [],
        "devices": [],
        "health_url": f"http://127.0.0.1:{HEALTH_PORT}/health",
    }
    # Override the default containers.json with our test entry
    core._containers = [entry]
    core._save_containers()
    return core, entry


# ---------------------------------------------------------------------------
# Tests — backend unit level (no full update cycle)
# ---------------------------------------------------------------------------

@pytest.mark.docker
def test_backend_ps_absent(stub_images):
    """ps() on a non-existent container returns running=False."""
    backend = _LocalCliDockerBackend()
    state = backend.ps("vanchor-sup-test-never-exists-xyz")
    assert state["running"] is False
    assert state["status"] == "absent"


@pytest.mark.docker
def test_backend_run_and_ps(stub_images):
    """run() starts a container; ps() reflects running state."""
    backend = _LocalCliDockerBackend()
    entry = {
        "name": CONTAINER_NAME,
        "image": REPO_BASIC,
        "tag": "latest",
        "network": "host",
        "restart": "no",
        "env": {},
        "volumes": [],
        "device_cgroup_rules": [],
        "devices": [],
    }
    cid = backend.run(entry)
    assert cid  # non-empty container ID
    time.sleep(0.5)
    state = backend.ps(CONTAINER_NAME)
    assert state["running"] is True


@pytest.mark.docker
def test_backend_stop_and_ps(stub_images):
    """stop() transitions a running container to exited."""
    backend = _LocalCliDockerBackend()
    entry = {
        "name": CONTAINER_NAME,
        "image": REPO_BASIC,
        "tag": "latest",
        "network": "host",
        "restart": "no",
        "env": {},
        "volumes": [],
        "device_cgroup_rules": [],
        "devices": [],
    }
    backend.run(entry)
    time.sleep(0.3)
    backend.stop(CONTAINER_NAME)
    state = backend.ps(CONTAINER_NAME)
    assert state["running"] is False


@pytest.mark.docker
def test_backend_volume_mountpoint(test_volume):
    """volume_mountpoint() returns the host path of a named volume."""
    backend = _LocalCliDockerBackend()
    mp = backend.volume_mountpoint(VOLUME_NAME)
    assert mp  # non-empty path
    assert mp.startswith("/")


# ---------------------------------------------------------------------------
# Tests — full update lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.docker
def test_health_gate_pass(stub_images, tmp_path):
    """Update from v1 to v2 (both healthy): job ok=True, not rolled back."""
    core, entry = _make_core(tmp_path, REPO_PASS, "v1", health_gate_s=8.0)

    # Start the container at v1
    core.backend.run(entry)
    time.sleep(0.5)

    # Apply update to v2 (same healthy image, different tag)
    job = core.apply_update(CONTAINER_NAME, tag="v2")

    final = _wait_for_job(core, job, timeout=20.0)
    assert final["ok"] is True, f"job failed: {final.get('error')}"
    assert final["rolled_back"] is False


@pytest.mark.docker
def test_health_gate_fail_triggers_rollback(stub_images, tmp_path):
    """Update to unhealthy image: gate fails → auto-rollback to previous tag."""
    core, entry = _make_core(tmp_path, REPO_FAIL, "v-ok", health_gate_s=5.0)

    # Start with healthy v-ok image
    core.backend.run(entry)
    time.sleep(0.5)

    # Apply update to the unhealthy v-bad image — gate will time out after 5 s
    job = core.apply_update(CONTAINER_NAME, tag="v-bad")

    # Allow enough time for: health_gate (5 s) + stop (2 s) + run + rollback gate (5 s)
    final = _wait_for_job(core, job, timeout=30.0)

    assert final["rolled_back"] is True, f"Expected rollback; job: {final}"
    assert final["ok"] is False

    # Container must be running again (rolled back to v-ok)
    state = core.backend.ps(CONTAINER_NAME)
    assert state["running"] is True


# ---------------------------------------------------------------------------
# Tests — backup pipeline
# ---------------------------------------------------------------------------

@pytest.mark.docker
def test_backup_of_named_volume(stub_images, test_volume, tmp_path):
    """create_backup() archives a volume and produces a non-empty .tar.gz.

    The supervisor needs root to access /var/lib/docker/volumes directly.
    On dev machines we patch volume_mountpoint to return a user-writable
    tmp dir, preserving real integration of the core → backup pipeline
    while still verifying that volume_mountpoint() itself works.
    """
    core, entry = _make_core(tmp_path, REPO_BASIC, "latest", health_gate_s=5.0)

    # Verify volume_mountpoint() can contact docker (real call)
    real_mp = core.backend.volume_mountpoint(VOLUME_NAME)
    assert real_mp  # proves docker volume inspect works

    # Use a user-accessible tmp dir so backup.create() can open it
    fake_vol = tmp_path / "vol_data"
    fake_vol.mkdir()
    (fake_vol / "app.json").write_text('{"version": "test"}')
    (fake_vol / "depth.json").write_text("[]")

    # Patch volume_mountpoint on the inner backend to return our user dir
    with patch.object(core.backend._inner, "volume_mountpoint",
                      return_value=str(fake_vol)):
        job = core.create_backup(CONTAINER_NAME)
        final = _wait_for_job(core, job, timeout=30.0)

    assert final["ok"] is True, f"backup failed: {final.get('error')}"
    backup_path = Path(final["backup_path"])
    assert backup_path.exists()
    assert backup_path.suffix == ".gz"
    assert backup_path.stat().st_size > 0

    backups = core.list_backups()
    assert any(b["id"] == backup_path.name for b in backups)
