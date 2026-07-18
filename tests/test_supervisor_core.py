"""Tests for vanchor_supervisor.core — SupervisorCore state machine."""
from __future__ import annotations
import json
import sys
import time
import tarfile
import io
import hashlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import make_bundle  # noqa: E402

from vanchor_supervisor.config import SupervisorSettings
from vanchor_supervisor.core import SupervisorCore
from supervisor_fakes import FakeDockerBackend, FakeHealth


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture()
def settings(tmp_path) -> SupervisorSettings:
    s = SupervisorSettings()
    s.state_dir = str(tmp_path / "state")
    s.data_volume = "vanchor_data"
    s.health_gate_s = 0.5   # short gate so tests run in <1s each
    s.health_ok_count = 2
    s.health_poll_s = 0.001  # fast polling for tests
    s.min_free_mb_for_update = 0  # disable disk floor in tests
    Path(s.state_dir).mkdir(parents=True, exist_ok=True)
    return s


@pytest.fixture()
def backend(tmp_path) -> FakeDockerBackend:
    vol_root = tmp_path / "data"
    vol_root.mkdir()
    b = FakeDockerBackend(volume_root=vol_root)
    # _DEFAULT_CONTAINERS uses "vanchor/vanchor" (matches factory bundle CI).
    b.images.add(("vanchor/vanchor", "1.5.0a8"))
    b.containers["vanchor"] = {
        "name": "vanchor",
        "state": "running",
        "started_at": "2026-07-18T00:00:00Z",
    }
    return b


@pytest.fixture()
def healthy() -> FakeHealth:
    # Returns 200 many times (for health gate)
    return FakeHealth([200] * 20)


def _make_app_bundle(tmp_path: Path, *, tag: str = "1.5.0a9",
                     min_supervisor: str = "0.1.0") -> Path:
    img = tmp_path / "image.tar.gz"
    img.write_bytes(b"\x1f\x8b" + b"img" * 50)
    out = tmp_path / f"app-{tag}.bundle.tar"
    make_bundle.make_app_bundle(
        image_tar_gz=img,
        image="vanchor/vanchor",  # matches _DEFAULT_CONTAINERS / factory bundle CI
        tag=tag,
        min_supervisor=min_supervisor,
        arch="arm64",
        out=out,
    )
    return out


# ------------------------------------------------------------------ #
# Bootstrap
# ------------------------------------------------------------------ #

def test_bootstrap_creates_containers_json(tmp_path, settings, backend, healthy):
    core = SupervisorCore(settings, backend, health_fetch=healthy)
    containers_file = Path(settings.state_dir) / "containers.json"
    assert containers_file.exists()
    data = json.loads(containers_file.read_text())
    assert isinstance(data, list)
    assert data[0]["name"] == "vanchor"


def test_bootstrap_idempotent(tmp_path, settings, backend, healthy):
    core1 = SupervisorCore(settings, backend, health_fetch=healthy)
    containers_before = (Path(settings.state_dir) / "containers.json").read_text()
    core2 = SupervisorCore(settings, backend, health_fetch=healthy)
    containers_after = (Path(settings.state_dir) / "containers.json").read_text()
    assert json.loads(containers_before) == json.loads(containers_after)


# ------------------------------------------------------------------ #
# Bundle update — happy path
# ------------------------------------------------------------------ #

def test_bundle_update_happy_path(tmp_path, settings, backend, healthy):
    bundle = _make_app_bundle(tmp_path, tag="1.5.0a9")
    # Place bundle in volume mountpoint/updates/
    updates_dir = Path(backend.volumes["vanchor_data"]) / "updates"
    updates_dir.mkdir()
    bundle_in_vol = updates_dir / bundle.name
    bundle_in_vol.write_bytes(bundle.read_bytes())

    core = SupervisorCore(settings, backend, health_fetch=healthy,
                          sleep=lambda _: None)
    job = core.apply_update("vanchor", bundle_rel=f"updates/{bundle.name}")
    job_id = job["id"]
    # Wait for job to complete (it runs in a thread)
    _wait_for_job(core, job_id, timeout=5.0)

    job = core.get_job(job_id)
    assert job["ok"] is True, f"Expected ok=True, got job={job}"
    assert job["rolled_back"] is False
    assert job.get("to_tag") == "1.5.0a9"
    assert job.get("from_tag") == "1.5.0a8"

    # Verify backend calls: load -> stop -> rm -> run
    call_names = [c[0] for c in backend.calls]
    assert "load" in call_names
    assert "stop" in call_names
    assert "rm" in call_names
    assert "run" in call_names

    # Verify containers.json updated
    containers = json.loads((Path(settings.state_dir) / "containers.json").read_text())
    assert containers[0]["tag"] == "1.5.0a9"
    assert containers[0]["previous_tag"] == "1.5.0a8"

    # Verify bundle file was deleted after success
    assert not bundle_in_vol.exists()


def _wait_for_job(core: SupervisorCore, job_or_id, timeout: float = 5.0) -> None:
    """Wait for a job to reach a terminal phase.

    Accepts either a job dict (returned by apply_update/rollback) or a string id.
    """
    if isinstance(job_or_id, dict):
        job_id = job_or_id["id"]
    else:
        job_id = job_or_id
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = core.get_job(job_id)
        if job and job.get("phase") in ("done", "failed"):
            return
        time.sleep(0.05)
    raise TimeoutError(f"Job {job_id} did not complete within {timeout}s")


# ------------------------------------------------------------------ #
# Health-gate fail → rollback
# ------------------------------------------------------------------ #

def test_health_gate_fail_triggers_rollback(tmp_path, settings, backend):
    bundle = _make_app_bundle(tmp_path, tag="1.5.0a9")
    updates_dir = Path(backend.volumes["vanchor_data"]) / "updates"
    updates_dir.mkdir()
    bundle_in_vol = updates_dir / bundle.name
    bundle_in_vol.write_bytes(bundle.read_bytes())

    # Return 503/0 forever so the update gate times out and rollback is triggered.
    # With health_gate_s=0.5 and no sleep, this runs for 500ms then fails.
    # The rollback gate also fails (FakeHealth returns 0 after all codes consumed),
    # but that's fine — the test only checks ok=False, rolled_back=True, tag=1.5.0a8.
    fail_then_ok = FakeHealth([503] * 50)
    core = SupervisorCore(settings, backend, health_fetch=fail_then_ok,
                          sleep=lambda _: None)
    job = core.apply_update("vanchor", bundle_rel=f"updates/{bundle.name}")
    job_id = job["id"]
    _wait_for_job(core, job_id, timeout=15.0)

    job = core.get_job(job_id)
    assert job["ok"] is False
    assert job["rolled_back"] is True
    # The container should be running the old tag
    containers = json.loads((Path(settings.state_dir) / "containers.json").read_text())
    assert containers[0]["tag"] == "1.5.0a8"


# ------------------------------------------------------------------ #
# Rollback-gate-also-fails → rollback_unhealthy, no loop
# ------------------------------------------------------------------ #

def test_rollback_gate_also_fails(tmp_path, settings, backend):
    bundle = _make_app_bundle(tmp_path, tag="1.5.0a9")
    updates_dir = Path(backend.volumes["vanchor_data"]) / "updates"
    updates_dir.mkdir()
    bundle_in_vol = updates_dir / bundle.name
    bundle_in_vol.write_bytes(bundle.read_bytes())

    always_fail = FakeHealth([503] * 200)
    core = SupervisorCore(settings, backend, health_fetch=always_fail,
                          sleep=lambda _: None)
    job = core.apply_update("vanchor", bundle_rel=f"updates/{bundle.name}")
    job_id = job["id"]
    _wait_for_job(core, job_id, timeout=15.0)

    job = core.get_job(job_id)
    assert job["ok"] is False
    assert job["error"] == "rollback_unhealthy"
    # Must not loop: ensure run() was called at most twice (once for new, once for rollback)
    run_calls = [c for c in backend.calls if c[0] == "run"]
    assert len(run_calls) <= 2


# ------------------------------------------------------------------ #
# Registry source → uses pull
# ------------------------------------------------------------------ #

def test_registry_source_uses_pull(tmp_path, settings, backend, healthy):
    core = SupervisorCore(settings, backend, health_fetch=healthy,
                          sleep=lambda _: None)
    job = core.apply_update("vanchor", tag="1.5.0a9")
    job_id = job["id"]
    _wait_for_job(core, job_id, timeout=5.0)

    pull_calls = [c for c in backend.calls if c[0] == "pull"]
    assert len(pull_calls) >= 1
    assert pull_calls[-1][1] == "vanchor/vanchor"
    assert pull_calls[-1][2] == "1.5.0a9"

    job = core.get_job(job_id)
    assert job["ok"] is True


# ------------------------------------------------------------------ #
# min_supervisor constraint
# ------------------------------------------------------------------ #

def test_min_supervisor_too_old_refused(tmp_path, settings, backend, healthy):
    # Bundle requires supervisor 9.0.0 but installed is 0.1.0
    bundle = _make_app_bundle(tmp_path, tag="1.5.0a9", min_supervisor="9.0.0")
    updates_dir = Path(backend.volumes["vanchor_data"]) / "updates"
    updates_dir.mkdir()
    bundle_in_vol = updates_dir / bundle.name
    bundle_in_vol.write_bytes(bundle.read_bytes())

    core = SupervisorCore(settings, backend, health_fetch=healthy,
                          sleep=lambda _: None)
    job = core.apply_update("vanchor", bundle_rel=f"updates/{bundle.name}")
    job_id = job["id"]
    _wait_for_job(core, job_id, timeout=5.0)

    job = core.get_job(job_id)
    assert job["ok"] is False
    assert job["error"].startswith("supervisor_too_old")
    # Container must be untouched
    load_calls = [c for c in backend.calls if c[0] == "load"]
    assert len(load_calls) == 0


# ------------------------------------------------------------------ #
# Bundle path traversal rejection
# ------------------------------------------------------------------ #

def test_bundle_path_traversal_rejected(tmp_path, settings, backend, healthy):
    core = SupervisorCore(settings, backend, health_fetch=healthy,
                          sleep=lambda _: None)
    job = core.apply_update("vanchor", bundle_rel="../../etc/passwd")
    job_id = job["id"]
    _wait_for_job(core, job_id, timeout=5.0)

    job = core.get_job(job_id)
    assert job["ok"] is False
    assert "traversal" in job["error"].lower() or "invalid" in job["error"].lower()


# ------------------------------------------------------------------ #
# Busy → 409
# ------------------------------------------------------------------ #

def test_busy_rejects_second_apply(tmp_path, settings, backend):
    # Use slow health to keep the job running
    slow_health = FakeHealth([503] * 200)  # always fail, but slowly
    core = SupervisorCore(settings, backend, health_fetch=slow_health,
                          sleep=lambda _: None)

    # Start first job
    from vanchor_supervisor.core import BusyError
    core.apply_update("vanchor", tag="1.5.0a9")

    # Immediately try to start another (should raise BusyError)
    with pytest.raises(BusyError):
        core.apply_update("vanchor", tag="1.5.0a9")


# ------------------------------------------------------------------ #
# Rollback with no previous tag
# ------------------------------------------------------------------ #

def test_rollback_no_previous_fails(tmp_path, settings, backend, healthy):
    core = SupervisorCore(settings, backend, health_fetch=healthy,
                          sleep=lambda _: None)
    # Ensure no previous_tag
    containers = json.loads((Path(settings.state_dir) / "containers.json").read_text())
    containers[0]["previous_tag"] = None
    (Path(settings.state_dir) / "containers.json").write_text(json.dumps(containers))
    core._containers = core._load_containers()

    job = core.rollback("vanchor")
    job_id = job["id"]
    # Job is immediately failed (no thread needed)
    _wait_for_job(core, job_id, timeout=2.0)

    job = core.get_job(job_id)
    assert job["ok"] is False
    assert job["error"] == "no_previous"


# ------------------------------------------------------------------ #
# Job persistence
# ------------------------------------------------------------------ #

def test_jobs_persisted_across_instances(tmp_path, settings, backend, healthy):
    core1 = SupervisorCore(settings, backend, health_fetch=healthy,
                           sleep=lambda _: None)
    job = core1.apply_update("vanchor", tag="1.5.0a9")
    job_id = job["id"]
    _wait_for_job(core1, job_id, timeout=5.0)

    # A new instance can still read the job
    core2 = SupervisorCore(settings, backend, health_fetch=healthy,
                           sleep=lambda _: None)
    job2 = core2.get_job(job_id)
    assert job2 is not None
    assert job2["id"] == job_id
    assert job2["ok"] is True


def test_last_job_readable(tmp_path, settings, backend, healthy):
    core = SupervisorCore(settings, backend, health_fetch=healthy,
                          sleep=lambda _: None)
    job = core.apply_update("vanchor", tag="1.5.0a9")
    job_id = job["id"]
    _wait_for_job(core, job_id, timeout=5.0)

    last = core.get_last_job()
    assert last is not None
    assert last["id"] == job_id


# ------------------------------------------------------------------ #
# I2: ensure_running reconcile
# ------------------------------------------------------------------ #

def test_ensure_running_starts_absent_container(tmp_path, settings, backend, healthy):
    """I2: absent container with a restart policy is started."""
    # Remove the container from the fake backend so it's absent.
    backend.containers.pop("vanchor", None)
    # Image is still present (added in backend fixture).
    core = SupervisorCore(settings, backend, health_fetch=healthy)

    core.ensure_running()

    run_calls = [c for c in backend.calls if c[0] == "run"]
    assert run_calls, "ensure_running must call backend.run for absent container"
    started_name = run_calls[0][1]["name"]
    assert started_name == "vanchor"


def test_ensure_running_no_op_for_running_container(tmp_path, settings, backend, healthy):
    """I2: container already running is not restarted."""
    # Container is running (set in backend fixture).
    core = SupervisorCore(settings, backend, health_fetch=healthy)
    backend.calls.clear()  # clear calls from init

    core.ensure_running()

    run_calls = [c for c in backend.calls if c[0] == "run"]
    assert not run_calls, "ensure_running must not restart a running container"


def test_ensure_running_skips_missing_image(tmp_path, settings, backend, healthy):
    """I2: if image is missing locally, log a warning and do not crash."""
    backend.containers.pop("vanchor", None)
    backend.images.clear()  # no images locally
    core = SupervisorCore(settings, backend, health_fetch=healthy)

    # Must not raise.
    core.ensure_running()

    run_calls = [c for c in backend.calls if c[0] == "run"]
    assert not run_calls, "ensure_running must not run container if image is absent"
