"""Versioned backup / restore of all persistent Vanchor state.

A *backup* is a single in-memory ZIP archive bundling everything worth keeping
from the runtime's ``data_dir`` plus the small slice of client state the web UI
keeps in ``localStorage``. It is self-describing: a ``manifest.json`` at the
root records the format, schema version, app version, creation time and the list
of contained entries, so a restore can validate it and (in the future) migrate
older layouts forward.

Archive layout
--------------
::

    manifest.json        # see below
    client.json          # the UI's localStorage dict (keys prefixed "vanchor-")
    boats.json           # boat profiles            (if present in data_dir)
    depthmap.json        # accumulated depth soundings (if present)
    devices.json         # persisted device/hardware config (if present)
    trips/<id>.json      # per-outing trip logs     (every file under trips/)

Deliberately EXCLUDED: ``water_cache/`` and ``debug/`` -- both are large and
fully regenerable, so they would only bloat the archive.

Manifest shape
--------------
::

    {
        "format": "vanchor-backup",   # constant magic; restore rejects anything else
        "schema_version": 1,           # == SCHEMA_VERSION at creation time
        "app_version": "0.1.0",       # the package version that wrote it
        "created_at": "2026-06-26T12:00:00Z",  # ISO8601, PASSED IN (never datetime.now())
        "contents": ["boats.json", "depthmap.json", "trips/trip-...json", ...]
    }

Versioning + migration
----------------------
``SCHEMA_VERSION`` is the on-disk layout version. Bump it whenever the set of
files, their names, or their internal shape changes in a way a plain extract
can't handle. For each bump add a migration step keyed on the *source* version
inside :func:`_migrate` -- that function is the single, explicit extension point
for "convert old backups": it receives the parsed manifest (and the open zip)
and returns a possibly-rewritten manifest before extraction. Today it is a
no-op pass-through; future versions chain ``v1 -> v2 -> ...`` steps there.

A backup whose ``schema_version`` is NEWER than this build's ``SCHEMA_VERSION``
is still restored best-effort (unknown files are simply ignored) with a warning,
so a downgrade never hard-fails.
"""

from __future__ import annotations

import io
import json
import logging
import os
import posixpath
import zipfile
from importlib.metadata import PackageNotFoundError, version as _pkg_version

logger = logging.getLogger("vanchor.backup")

# On-disk backup layout version. Bump on any incompatible change to the file
# set / names / shapes, and add a matching migration step in ``_migrate``.
SCHEMA_VERSION = 1

# The magic identifying a Vanchor backup; restore rejects anything else.
FORMAT = "vanchor-backup"

# The top-level data_dir files we back up (regenerable caches excluded).
_DATA_FILES = ("boats.json", "depthmap.json", "devices.json")
# The sub-directory of per-outing trip logs (every *.json inside is included).
_TRIPS_DIR = "trips"
# Caches that are large + regenerable -> never included.
# NOTE: push/ is intentionally NOT listed here (no _DATA_FILES entry, no
# _EXCLUDED_DIRS entry either). The VAPID private key must not appear in
# support ZIPs; subscriptions are per-browser capability URLs that would
# silently misdirect someone's phone if restored onto another install.
# The re-sync in push.js makes re-subscribing automatic on next card open.
_EXCLUDED_DIRS = ("water_cache", "debug", "updates")


def _app_version() -> str:
    """The installed package version, or ``"unknown"`` if not resolvable."""
    try:
        return _pkg_version("vanchor-ng")
    except PackageNotFoundError:  # pragma: no cover - only if run uninstalled
        return "unknown"


def create_backup(
    data_dir: str,
    client: dict | None = None,
    app_version: str | None = None,
    *,
    created_at: str = "1970-01-01T00:00:00Z",
) -> bytes:
    """Build a versioned backup ZIP of ``data_dir`` (+ ``client`` state) in memory.

    ``client`` is the UI's ``localStorage`` slice (keys prefixed ``vanchor-``);
    ``None`` is stored as an empty object. ``created_at`` is an ISO8601 string
    the *caller* supplies (the endpoint passes the request time) -- this module
    never calls ``datetime.now()`` so backups are reproducible/testable.
    ``app_version`` defaults to the installed package version.

    Returns the raw ``.zip`` bytes.
    """
    if app_version is None:
        app_version = _app_version()
    client_obj = client if isinstance(client, dict) else {}

    contents: list[str] = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Top-level data files (each optional -- a fresh install may lack some).
        for name in _DATA_FILES:
            path = os.path.join(data_dir, name)
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    zf.writestr(name, fh.read())
                contents.append(name)

        # Every trip log under trips/ (kept under the same arc-name prefix).
        trips_dir = os.path.join(data_dir, _TRIPS_DIR)
        if os.path.isdir(trips_dir):
            for entry in sorted(os.listdir(trips_dir)):
                src = os.path.join(trips_dir, entry)
                if not os.path.isfile(src):
                    continue
                arcname = f"{_TRIPS_DIR}/{entry}"
                with open(src, "rb") as fh:
                    zf.writestr(arcname, fh.read())
                contents.append(arcname)

        # client.json always present (even if empty) so restore is uniform.
        zf.writestr("client.json", json.dumps(client_obj, indent=2))

        manifest = {
            "format": FORMAT,
            "schema_version": SCHEMA_VERSION,
            "app_version": app_version,
            "created_at": created_at,
            "contents": contents,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    logger.info("created backup: %d entries, schema v%d", len(contents), SCHEMA_VERSION)
    return buf.getvalue()


def _is_safe_member(name: str) -> bool:
    """Reject zip-slip entries: absolute paths, drive letters, or any ``..``
    traversal. Only relative, in-tree paths are allowed."""
    if not name or name.startswith(("/", "\\")):
        return False
    if ":" in name:  # e.g. a Windows drive letter
        return False
    # Normalise with POSIX semantics and ensure it stays in-tree.
    norm = posixpath.normpath(name)
    if norm.startswith("..") or norm.startswith("/") or os.path.isabs(norm):
        return False
    parts = norm.split("/")
    return ".." not in parts


def _migrate(manifest: dict, zf: zipfile.ZipFile) -> dict:
    """Migration extension point -- convert an OLDER backup's manifest forward.

    Called by :func:`restore_backup` when ``manifest["schema_version"] <
    SCHEMA_VERSION``. Today there is only one schema version, so this is a no-op
    pass-through. When ``SCHEMA_VERSION`` is bumped, add steps here keyed on the
    *source* version, e.g.::

        src = manifest.get("schema_version", 1)
        if src < 2:
            manifest = _migrate_v1_to_v2(manifest, zf)
        if src < 3:
            manifest = _migrate_v2_to_v3(manifest, zf)
        ...

    Each step may rewrite the manifest (and read from ``zf``) and returns the
    upgraded manifest. Returning the manifest unchanged keeps the plain extract
    path working for the current version.
    """
    return manifest


def restore_backup(data_dir: str, zip_bytes: bytes) -> dict:
    """Restore a backup ZIP into ``data_dir`` (overwriting existing files).

    Validates the manifest (rejecting anything whose ``format`` is not
    :data:`FORMAT`). A backup from a NEWER schema is restored best-effort with a
    warning; an OLDER one is run through :func:`_migrate` first. Known data files
    (``boats.json``, ``depthmap.json``, ``devices.json``, and everything under
    ``trips/``) are extracted; the ``trips/`` dir is created as needed. Entries
    with absolute or ``..`` paths (zip-slip) are ignored.

    Returns ``{ok, schema_version, app_version, created_at, restored, client,
    warnings}``. Raises :class:`ValueError` (mapped to HTTP 400 by the endpoint)
    on a corrupt zip or a missing/invalid manifest.
    """
    warnings: list[str] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"not a valid zip archive: {exc}") from exc

    with zf:
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError as exc:
            raise ValueError("backup is missing manifest.json") from exc
        except (ValueError, OSError) as exc:
            raise ValueError(f"backup manifest is unreadable: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise ValueError("not a vanchor-backup archive")

        schema_version = manifest.get("schema_version", 0)
        if not isinstance(schema_version, int):
            raise ValueError("backup manifest has an invalid schema_version")

        if schema_version > SCHEMA_VERSION:
            warnings.append(
                "newer backup; some data may be ignored "
                f"(backup schema v{schema_version} > supported v{SCHEMA_VERSION})"
            )
        elif schema_version < SCHEMA_VERSION:
            # Older layout -> run the migration hook to bring it forward.
            manifest = _migrate(manifest, zf)

        # client.json (optional; default empty). Tolerate a malformed one.
        client: dict = {}
        try:
            raw_client = json.loads(zf.read("client.json"))
            if isinstance(raw_client, dict):
                client = raw_client
            else:
                warnings.append("client.json was not an object; ignored")
        except KeyError:
            pass
        except (ValueError, OSError):
            warnings.append("client.json was unreadable; ignored")

        os.makedirs(data_dir, exist_ok=True)
        restored: list[str] = []
        for info in zf.infolist():
            name = info.filename
            if name.endswith("/"):
                continue  # directory entry
            if name in ("manifest.json", "client.json"):
                continue
            if not _is_safe_member(name):
                warnings.append(f"ignored unsafe archive path: {name!r}")
                continue
            # Only extract known top-level files and trip logs.
            is_trip = name.startswith(f"{_TRIPS_DIR}/") and name.endswith(".json")
            if name not in _DATA_FILES and not is_trip:
                # Unknown entry (e.g. from a newer schema) -> ignore quietly.
                if name.split("/", 1)[0] in _EXCLUDED_DIRS:
                    continue
                continue
            dest = os.path.join(data_dir, *name.split("/"))
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            with open(dest, "wb") as out:
                out.write(zf.read(name))
            restored.append(name)

    logger.info("restored backup: %d files, schema v%s", len(restored), schema_version)
    return {
        "ok": True,
        "schema_version": schema_version,
        "app_version": manifest.get("app_version"),
        "created_at": manifest.get("created_at"),
        "restored": restored,
        "client": client,
        "warnings": warnings,
    }
