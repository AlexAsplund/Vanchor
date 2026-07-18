"""Shared fakes for supervisor tests.

``FakeDockerBackend`` — an in-memory implementation of the DockerBackend
protocol that records the call sequence and never shells out.

``FakeHealth`` — a scripted sequence of HTTP status codes for health-gate
testing.
"""
from __future__ import annotations
import os
from pathlib import Path


class FakeDockerBackend:
    """In-memory DockerBackend implementation for tests.

    Attributes:
        images: set of (image, tag) pairs that "exist" locally.
        containers: dict of name -> container-state dict.
        volumes: dict of volume-name -> mountpoint path (str).
        calls: ordered list of (method_name, *args) recorded on every call.
        load_result: what load() returns (image:tag string); override per test.
        missing_exec_paths: set of paths that exec_test_path returns False for.
        system_df_result: override the system_df() return value.
        registry_tags_result: list returned by registry_tags().
    """

    def __init__(self, volume_root: str | Path | None = None) -> None:
        self.images: set[tuple[str, str]] = set()
        self.containers: dict[str, dict] = {}
        self.volumes: dict[str, str] = {}
        if volume_root is not None:
            self.volumes["vanchor_data"] = str(volume_root)
        self.calls: list[tuple] = []
        self.load_result: str = "ghcr.io/alexasplund/vanchor:1.5.0a9"
        self.missing_exec_paths: set[str] = set()
        self.system_df_result: dict = {
            "images_bytes": 200 * 1024 * 1024,
            "reclaimable_bytes": 50 * 1024 * 1024,
        }
        self.registry_tags_result: list[str] = []

    def _record(self, method: str, *args) -> None:
        self.calls.append((method, *args))

    def ps(self, name: str) -> dict:
        """Return container state dict: running, status, image."""
        self._record("ps", name)
        c = self.containers.get(name)
        if c is None:
            return {"running": False, "status": "absent", "image": ""}
        running = c.get("state", "unknown") == "running"
        return {"running": running, "status": c.get("state", "unknown"), "image": c.get("image", "")}

    def inspect(self, name: str) -> dict | None:
        """Return raw container dict (FakeDockerBackend only)."""
        self._record("inspect", name)
        c = self.containers.get(name)
        if c is None:
            return None
        return dict(c)

    def image_exists(self, image: str, tag: str) -> bool:
        self._record("image_exists", image, tag)
        return (image, tag) in self.images

    def pull(self, image: str, tag: str) -> None:
        self._record("pull", image, tag)
        self.images.add((image, tag))

    def load(self, tar_path: str) -> str:
        self._record("load", tar_path)
        # Parse the loaded image:tag from load_result
        if ":" in self.load_result:
            img, tag = self.load_result.rsplit(":", 1)
            self.images.add((img, tag))
        return self.load_result

    def run(self, entry: dict) -> None:
        self._record("run", entry)
        self.containers[entry["name"]] = {
            "name": entry["name"],
            "image": entry.get("image", ""),
            "tag": entry.get("tag", ""),
            "state": "running",
            "started_at": "2026-07-18T00:00:00Z",
        }

    def stop(self, name: str) -> None:
        """Stop a container.  FakeDockerBackend always succeeds (no error)."""
        self._record("stop", name)
        if name in self.containers:
            self.containers[name]["state"] = "exited"

    def rm(self, name: str) -> None:
        self._record("rm", name)
        self.containers.pop(name, None)

    def list_repo_tags(self, image: str) -> list[str]:
        """Return list of locally available tags for an image repository."""
        self._record("list_repo_tags", image)
        return [tag for (img, tag) in self.images if img == image]

    def rmi(self, image: str, tag: str) -> None:
        self._record("rmi", image, tag)
        self.images.discard((image, tag))

    def system_df(self) -> dict:
        self._record("system_df")
        return dict(self.system_df_result)

    def volume_mountpoint(self, volume: str) -> str:
        self._record("volume_mountpoint", volume)
        if volume not in self.volumes:
            raise RuntimeError(f"Volume {volume!r} not in fake volumes")
        return self.volumes[volume]

    def exec_test_path(self, name: str, path: str) -> bool:
        self._record("exec_test_path", name, path)
        return path not in self.missing_exec_paths

    def registry_tags(self, image: str) -> list[str]:
        self._record("registry_tags", image)
        return list(self.registry_tags_result)

    def prune_dangling(self) -> None:
        self._record("prune_dangling")


class FakeHealth:
    """Scripted health-check responses for gate testing.

    Pass a list of status codes; each call to __call__ pops the first one.
    Remaining calls return 0 (simulate connection error / unhealthy).
    Pass ``cycle=True`` to repeat the sequence.
    """

    def __init__(self, codes: list[int], *, cycle: bool = False) -> None:
        self._codes = list(codes)
        self._cycle = cycle
        self._orig = list(codes)

    def __call__(self, url: str) -> int:
        if not self._codes:
            if self._cycle:
                self._codes = list(self._orig)
            else:
                return 0  # simulate timeout/connection error (counts as unhealthy)
        return self._codes.pop(0)
