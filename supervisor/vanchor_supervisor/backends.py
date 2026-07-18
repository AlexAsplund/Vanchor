"""Docker backend abstraction.

CliDockerBackend wraps docker CLI via subprocess and is the production
implementation.  Tests inject a FakeDockerBackend (defined in tests/).
"""
from __future__ import annotations

import os
import subprocess
from typing import Callable, List, Optional

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DockerError(RuntimeError):
    """Raised when a docker CLI call returns non-zero."""


# ---------------------------------------------------------------------------
# Protocol (structural typing without runtime cost)
# ---------------------------------------------------------------------------

# We describe the interface as a set of documented methods.  Python duck-typing
# means concrete classes just need to implement the same signatures.


class DockerBackend:
    """Interface that supervisor code depends on.  Not instantiated directly."""

    def ps(self, name: str) -> dict:
        """Return a dict with keys: running (bool), status (str), image (str)."""
        raise NotImplementedError

    def inspect(self, name: str) -> dict | None:
        """Return raw docker inspect JSON for container, or None if absent."""
        raise NotImplementedError

    def stop(self, name: str) -> None:
        """docker stop <name>. Raises DockerError on non-zero exit."""
        raise NotImplementedError

    def rm(self, name: str) -> None:
        """docker rm -f <name> — no error if absent."""
        raise NotImplementedError

    def run(self, entry: dict) -> str:
        """docker run -d … returning the container ID."""
        raise NotImplementedError

    def pull(self, image: str, tag: str) -> None:
        """docker pull <image>:<tag>."""
        raise NotImplementedError

    def load(self, tar_path: str) -> str:
        """docker load < <tar_path>, return the loaded image ref."""
        raise NotImplementedError

    def exec_test_path(self, container_name: str, path: str) -> bool:
        """Return True if <path> exists inside the container."""
        raise NotImplementedError

    def system_df(self) -> dict:
        """Return docker system df summary as a dict with images_bytes, reclaimable_bytes."""
        raise NotImplementedError

    def list_repo_tags(self, image: str) -> list[str]:
        """Return list of tags available locally for a given image repository."""
        raise NotImplementedError

    def images(self, repository: str) -> list[dict]:
        """Return list of dicts with keys: tag, id for a given repository."""
        raise NotImplementedError

    def rmi(self, image: str, tag: str) -> None:
        """docker rmi <image>:<tag> — no error if absent."""
        raise NotImplementedError

    def prune_dangling(self) -> None:
        """docker image prune -f — remove dangling images."""
        raise NotImplementedError

    def volume_mountpoint(self, volume_name: str) -> str:
        """Return the host mountpoint of a named docker volume."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# CLI implementation
# ---------------------------------------------------------------------------


class CliDockerBackend:
    """Implements DockerBackend via docker CLI calls."""

    def __init__(self, runner: Optional[Callable] = None) -> None:
        self._run = runner if runner is not None else subprocess.run

    def _check(self, args: list[str], **kwargs) -> subprocess.CompletedProcess:
        result = self._run(
            args,
            capture_output=True,
            text=True,
            **kwargs,
        )
        if result.returncode != 0:
            raise DockerError(
                f"Command {args!r} failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        return result

    def ps(self, name: str) -> dict:
        """Return container info dict.  Absent containers → running=False."""
        try:
            result = self._check(
                ["docker", "inspect", "--format",
                 "{{.State.Status}}|{{.Config.Image}}", name]
            )
        except DockerError:
            return {"running": False, "status": "absent", "image": ""}
        parts = result.stdout.strip().split("|", 1)
        status = parts[0] if parts else "unknown"
        image = parts[1] if len(parts) > 1 else ""
        return {"running": status == "running", "status": status, "image": image}

    def inspect(self, name: str) -> dict | None:
        """Return parsed docker inspect JSON for the container, or None if absent."""
        import json as _json
        try:
            result = self._check(["docker", "inspect", name])
        except DockerError:
            return None
        try:
            data = _json.loads(result.stdout)
            return data[0] if data else None
        except (ValueError, IndexError):
            return None

    def stop(self, name: str) -> None:
        """docker stop <name>. Raises DockerError on non-zero exit."""
        self._check(["docker", "stop", name])

    def rm(self, name: str) -> None:
        try:
            self._check(["docker", "rm", "-f", name])
        except DockerError:
            pass  # already absent

    def run(self, entry: dict) -> str:
        """Build and execute docker run -d from a containers.json entry."""
        name = entry["name"]
        image = entry["image"]
        tag = entry["tag"]
        network = entry.get("network", "host")
        restart = entry.get("restart", "unless-stopped")

        args = [
            "docker", "run", "-d",
            "--name", name,
            "--network", network,
            "--restart", restart,
        ]

        # Log bounds: unbounded json-file logs wear out SD cards.  Default to
        # the local driver capped at 2 x 5 MB; overridable per entry via a
        # "logging" field: {"driver": ..., "options": {...}} (add-on tunable).
        logging_cfg = entry.get("logging") or {
            "driver": "local",
            "options": {"max-size": "5m", "max-file": "2"},
        }
        driver = logging_cfg.get("driver")
        if driver:
            args += ["--log-driver", driver]
        for opt_k, opt_v in (logging_cfg.get("options") or {}).items():
            args += ["--log-opt", f"{opt_k}={opt_v}"]

        # Environment variables
        for k, v in (entry.get("env") or {}).items():
            args += ["-e", f"{k}={v}"]

        # Volumes: named volume or bind mount
        for vol in (entry.get("volumes") or []):
            if "volume" in vol:
                args += ["-v", f"{vol['volume']}:{vol['target']}"]
            elif "host" in vol:
                src = vol["host"]
                tgt = vol["target"]
                if vol.get("ro"):
                    args += ["-v", f"{src}:{tgt}:ro"]
                else:
                    args += ["-v", f"{src}:{tgt}"]

        # Device cgroup rules
        for rule in (entry.get("device_cgroup_rules") or []):
            args += ["--device-cgroup-rule", rule]

        # Devices — skip any that don't exist on the host
        for device in (entry.get("devices") or []):
            if os.path.exists(device):
                args += ["--device", device]

        args.append(f"{image}:{tag}")

        result = self._check(args)
        return result.stdout.strip()

    def pull(self, image: str, tag: str) -> None:
        self._check(["docker", "pull", f"{image}:{tag}"])

    def load(self, tar_path: str) -> str:
        """docker load < <tar_path>, return loaded image ref."""
        with open(tar_path, "rb") as fh:
            result = self._run(
                ["docker", "load"],
                stdin=fh,
                capture_output=True,
                text=True,
            )
        if result.returncode != 0:
            raise DockerError(f"docker load failed: {result.stderr.strip()}")
        # Output is typically: "Loaded image: <ref>"
        for line in result.stdout.splitlines():
            if line.startswith("Loaded image"):
                return line.split(":", 1)[1].strip()
        return result.stdout.strip()

    def exec_test_path(self, container_name: str, path: str) -> bool:
        """Return True if <path> exists inside the running container."""
        try:
            result = self._run(
                ["docker", "exec", container_name, "test", "-e", path],
                capture_output=True,
            )
            return result.returncode == 0
        except Exception:
            return False

    def system_df(self) -> dict:
        """Return docker system df in JSON format."""
        try:
            result = self._check(["docker", "system", "df", "--format", "{{json .}}"])
            import json
            # Output is multiple JSON lines; collect all
            lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
            items = [json.loads(l) for l in lines]
            return {"items": items}
        except Exception:
            return {"items": []}

    def list_repo_tags(self, image: str) -> list[str]:
        """Return list of tags available locally for image repository."""
        try:
            result = self._check([
                "docker", "images", "--format", "{{.Tag}}", image,
            ])
            return [t.strip() for t in result.stdout.strip().splitlines() if t.strip()]
        except DockerError:
            return []

    def images(self, repository: str) -> list[dict]:
        """List all local tags for a repository as list of {tag, id} dicts."""
        try:
            result = self._check([
                "docker", "images", "--format",
                "{{.Tag}}|{{.ID}}", repository,
            ])
            out = []
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split("|", 1)
                out.append({"tag": parts[0], "id": parts[1] if len(parts) > 1 else ""})
            return out
        except DockerError:
            return []

    def rmi(self, image: str, tag: str) -> None:
        """docker rmi <image>:<tag> — silently ignores missing images."""
        try:
            self._check(["docker", "rmi", f"{image}:{tag}"])
        except DockerError:
            pass  # image absent or in use

    def prune_dangling(self) -> None:
        try:
            self._check(["docker", "image", "prune", "-f"])
        except DockerError:
            pass

    def volume_mountpoint(self, volume_name: str) -> str:
        result = self._check([
            "docker", "volume", "inspect",
            "--format", "{{.Mountpoint}}",
            volume_name,
        ])
        return result.stdout.strip()
