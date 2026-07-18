"""Tests for vanchor_supervisor.disk — df + prune policy."""
from __future__ import annotations
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from vanchor_supervisor.config import SupervisorSettings
from vanchor_supervisor.disk import snapshot, prune
from supervisor_fakes import FakeDockerBackend


# ------------------------------------------------------------------ #
# snapshot
# ------------------------------------------------------------------ #

@pytest.fixture()
def settings() -> SupervisorSettings:
    s = SupervisorSettings()
    s.disk_warn_pct = 80.0
    s.disk_crit_pct = 92.0
    return s


def _usage(total, used):
    """Return a namedtuple matching shutil.disk_usage() output."""
    from collections import namedtuple
    DU = namedtuple("disk_usage", ["total", "used", "free"])
    return DU(total=total, used=used, free=total - used)


def test_snapshot_computes_pct(tmp_path, settings):
    total = 100 * 2**30  # 100 GiB
    used = 50 * 2**30   # 50 GiB -> 50%
    backend = FakeDockerBackend(volume_root=tmp_path)

    with patch("shutil.disk_usage", return_value=_usage(total, used)):
        result = snapshot(tmp_path, backend, settings)

    assert result["data_total_bytes"] == total
    assert result["data_used_pct"] == pytest.approx(50.0, abs=0.1)
    assert result["warn"] is False
    assert result["crit"] is False


def test_snapshot_warn_threshold(tmp_path, settings):
    total = 100 * 2**30
    used = 85 * 2**30  # 85% > 80% warn
    backend = FakeDockerBackend(volume_root=tmp_path)

    with patch("shutil.disk_usage", return_value=_usage(total, used)):
        result = snapshot(tmp_path, backend, settings)

    assert result["warn"] is True
    assert result["crit"] is False


def test_snapshot_crit_threshold(tmp_path, settings):
    total = 100 * 2**30
    used = 95 * 2**30  # 95% > 92% crit
    backend = FakeDockerBackend(volume_root=tmp_path)

    with patch("shutil.disk_usage", return_value=_usage(total, used)):
        result = snapshot(tmp_path, backend, settings)

    assert result["warn"] is True
    assert result["crit"] is True


def test_snapshot_includes_docker_df(tmp_path, settings):
    backend = FakeDockerBackend(volume_root=tmp_path)
    backend.system_df_result = {
        "images_bytes": 1_000_000_000,
        "reclaimable_bytes": 200_000_000,
    }

    with patch("shutil.disk_usage", return_value=_usage(100 * 2**30, 50 * 2**30)):
        result = snapshot(tmp_path, backend, settings)

    assert result["docker_images_bytes"] == 1_000_000_000
    assert result["docker_reclaimable_bytes"] == 200_000_000


def test_snapshot_shape(tmp_path, settings):
    backend = FakeDockerBackend(volume_root=tmp_path)
    with patch("shutil.disk_usage", return_value=_usage(100 * 2**30, 50 * 2**30)):
        result = snapshot(tmp_path, backend, settings)

    expected_keys = {
        "data_free_bytes", "data_total_bytes", "data_used_pct",
        "docker_images_bytes", "docker_reclaimable_bytes", "warn", "crit",
    }
    assert set(result.keys()) >= expected_keys


# ------------------------------------------------------------------ #
# prune
# ------------------------------------------------------------------ #

def test_prune_keeps_current_and_previous(tmp_path):
    backend = FakeDockerBackend(volume_root=tmp_path)
    backend.images = {
        ("ghcr.io/alexasplund/vanchor", "1.5.0a8"),  # current
        ("ghcr.io/alexasplund/vanchor", "1.5.0a7"),  # previous
        ("ghcr.io/alexasplund/vanchor", "1.5.0a6"),  # old - should be pruned
        ("ghcr.io/alexasplund/vanchor", "1.5.0a5"),  # older - should be pruned
    }
    containers = [
        {
            "name": "vanchor",
            "image": "ghcr.io/alexasplund/vanchor",
            "tag": "1.5.0a8",
            "previous_tag": "1.5.0a7",
        }
    ]

    result = prune(backend, containers)

    rmi_calls = [(c[1], c[2]) for c in backend.calls if c[0] == "rmi"]
    pruned_tags = {tag for (_, tag) in rmi_calls}
    assert "1.5.0a6" in pruned_tags
    assert "1.5.0a5" in pruned_tags
    # current and previous must not be pruned
    assert "1.5.0a8" not in pruned_tags
    assert "1.5.0a7" not in pruned_tags


def test_prune_calls_prune_dangling(tmp_path):
    backend = FakeDockerBackend(volume_root=tmp_path)
    containers = [
        {
            "name": "vanchor",
            "image": "ghcr.io/alexasplund/vanchor",
            "tag": "1.5.0a8",
            "previous_tag": None,
        }
    ]
    prune(backend, containers)
    assert any(c[0] == "prune_dangling" for c in backend.calls)


def test_prune_no_previous_tag(tmp_path):
    """When previous_tag is None, only keep the current tag."""
    backend = FakeDockerBackend(volume_root=tmp_path)
    backend.images = {
        ("ghcr.io/alexasplund/vanchor", "1.5.0a8"),  # current
        ("ghcr.io/alexasplund/vanchor", "1.5.0a7"),  # old - should be pruned
    }
    containers = [
        {
            "name": "vanchor",
            "image": "ghcr.io/alexasplund/vanchor",
            "tag": "1.5.0a8",
            "previous_tag": None,
        }
    ]
    prune(backend, containers)

    rmi_calls = [(c[1], c[2]) for c in backend.calls if c[0] == "rmi"]
    pruned_tags = {tag for (_, tag) in rmi_calls}
    assert "1.5.0a7" in pruned_tags
    assert "1.5.0a8" not in pruned_tags
