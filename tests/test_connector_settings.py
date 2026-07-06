"""Tests for connector settings schema, API, and live-apply (Task 8).

Covers:
- Schema exposure: GET /api/connectors list carries settings_schema + masked settings.
- POST /api/connectors/{name}/settings: type coercion, unknown key → 400,
  masked "•••" leaves stored value untouched, unknown stored keys survive.
- Live-apply: armed+running connector restarted with new settings.
- Manifest-changing setting: flipping nmea2000 thruster_control disarms
  (armed=False, needs_reconsent=True in response + next GET).
- Legacy nmea-tcp: boot re-sync skipped after a user_edited save.
- Normal settings save does NOT change armed state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from vanchor.connectors.base import Connector, ConnectorManifest, manifest_hash
from vanchor.connectors.context import ConnectorContext
from vanchor.connectors import registry as _creg
from vanchor.connectors.registry import (
    armed,
    load_grants,
    save_grants,
    register_connector,
)
from vanchor.core.config import AppConfig
from vanchor.app import Runtime
from vanchor.ui.server import create_app


# --------------------------------------------------------------------------- #
# Dummy connector with settings_schema
# --------------------------------------------------------------------------- #


class _SettingsConnector(Connector):
    """Test connector that records how it was built and exposes a schema."""

    settings_schema = [
        {"key": "host", "label": "Host", "type": "str", "default": "localhost"},
        {"key": "port", "label": "Port", "type": "int", "default": 8080},
        {"key": "ratio", "label": "Ratio", "type": "float", "default": 1.0},
        {"key": "flag", "label": "Flag", "type": "bool", "default": False},
        {"key": "token", "label": "Token", "type": "str", "default": "", "secret": True,
         "hint": "Stored plaintext"},
    ]

    def __init__(self, host: str = "localhost", port: int = 8080,
                 ratio: float = 1.0, flag: bool = False, token: str = "") -> None:
        self.host = host
        self.port = port
        self.ratio = ratio
        self.flag = flag
        self.token = token
        self.started = False
        self.stopped = False
        self._ctx: ConnectorContext | None = None

    @property
    def manifest(self) -> ConnectorManifest:  # type: ignore[override]
        return ConnectorManifest(
            name="settings-dummy",
            label="Settings Dummy",
            description="Test connector with settings.",
            consumes=("telemetry",),
        )

    async def start(self, ctx: ConnectorContext) -> None:
        self._ctx = ctx
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def debug(self) -> str:
        return f"SettingsConnector(host={self.host}, port={self.port})"


# Track last built instance per name
_LAST_BUILT: dict[str, _SettingsConnector] = {}


def _register_settings_dummy(name: str = "settings-dummy") -> None:
    _LAST_BUILT.pop(name, None)

    def _build(settings: dict) -> Connector:
        c = _SettingsConnector(
            host=str(settings.get("host", "localhost")),
            port=int(settings.get("port", 8080)),
            ratio=float(settings.get("ratio", 1.0)),
            flag=bool(settings.get("flag", False)),
            token=str(settings.get("token", "")),
        )
        _LAST_BUILT[name] = c
        return c

    register_connector(name, _build, label="Settings Dummy")


def _unregister(name: str) -> None:
    _creg._REGISTRY.pop(name, None)  # type: ignore[attr-defined]
    _LAST_BUILT.pop(name, None)


def _runtime(tmp_path: Path, *, nmea_tcp_enabled: bool = False) -> Runtime:
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.nmea_tcp.enabled = nmea_tcp_enabled
    if nmea_tcp_enabled:
        cfg.nmea_tcp.port = 0
    return Runtime(cfg)


def _arm_grant(name: str, tmp_path: Path,
               settings: dict | None = None,
               extra: dict | None = None) -> None:
    """Write an armed grant for ``name`` into tmp_path/connectors.json."""
    sp = _creg.spec(name)
    assert sp is not None
    conn = sp.build(settings or {})
    grants = load_grants(tmp_path)
    g: dict[str, Any] = {
        "enabled": True,
        "manifest_hash": manifest_hash(conn.manifest),
        "settings": settings or {},
    }
    if extra:
        g.update(extra)
    grants[name] = g
    save_grants(tmp_path, grants)


# --------------------------------------------------------------------------- #
# 1. Schema exposure: GET /api/connectors carries settings + settings_schema
# --------------------------------------------------------------------------- #


def test_schema_exposed_in_list(tmp_path: Path) -> None:
    """connector_status() includes settings_schema and masked settings."""
    _register_settings_dummy("schema-exp-test")
    try:
        # Arm with a token set
        save_grants(tmp_path, {
            "schema-exp-test": {
                "enabled": True,
                "manifest_hash": manifest_hash(_creg.build("schema-exp-test", {}).manifest),
                "settings": {"host": "1.2.3.4", "port": 9000, "token": "mysecret"},
            }
        })
        rt = _runtime(tmp_path)
        status = rt.connector_status()
        entry = next((e for e in status if e["name"] == "schema-exp-test"), None)
        assert entry is not None
        assert "settings_schema" in entry
        assert "settings" in entry
        schema_keys = [f["key"] for f in entry["settings_schema"]]
        assert "host" in schema_keys
        assert "port" in schema_keys
        assert "token" in schema_keys
        # host/port visible
        assert entry["settings"]["host"] == "1.2.3.4"
        assert entry["settings"]["port"] == 9000
        # token masked
        assert entry["settings"]["token"] == "•••"
    finally:
        _unregister("schema-exp-test")


def test_secret_empty_shows_empty_string(tmp_path: Path) -> None:
    """Unset secret shows '' not '•••'."""
    _register_settings_dummy("schema-empty-secret")
    try:
        rt = _runtime(tmp_path)
        status = rt.connector_status()
        entry = next((e for e in status if e["name"] == "schema-empty-secret"), None)
        assert entry is not None
        assert entry["settings"]["token"] == ""
    finally:
        _unregister("schema-empty-secret")


def test_schema_via_api(tmp_path: Path) -> None:
    """GET /api/connectors returns settings_schema in the JSON response."""
    _register_settings_dummy("schema-api-test")
    try:
        app = create_app(_runtime(tmp_path))
        with TestClient(app) as c:
            r = c.get("/api/connectors")
            assert r.status_code == 200
            entries = {e["name"]: e for e in r.json()["connectors"]}
            assert "schema-api-test" in entries
            assert "settings_schema" in entries["schema-api-test"]
            assert "settings" in entries["schema-api-test"]
    finally:
        _unregister("schema-api-test")


# --------------------------------------------------------------------------- #
# 2. POST /api/connectors/{name}/settings — type coercion
# --------------------------------------------------------------------------- #


async def test_settings_type_coercion(tmp_path: Path) -> None:
    """int/float values passed as strings are coerced correctly."""
    _register_settings_dummy("coerce-test")
    try:
        rt = _runtime(tmp_path)
        # Pass string "9090" for an int field and "2.5" for float
        result = await rt.set_connector_settings("coerce-test", {
            "host": "example.com",
            "port": "9090",    # string that should become int
            "ratio": "2.5",    # string that should become float
            "flag": True,
            "token": "",
        })
        assert result["ok"] is True
        grants = load_grants(tmp_path)
        stored = grants["coerce-test"]["settings"]
        assert stored["port"] == 9090
        assert isinstance(stored["port"], int)
        assert stored["ratio"] == pytest.approx(2.5)
        assert isinstance(stored["ratio"], float)
    finally:
        _unregister("coerce-test")


def test_settings_unknown_key_returns_400(tmp_path: Path) -> None:
    """POSTing an unknown key returns 400."""
    _register_settings_dummy("unknown-key-test")
    try:
        app = create_app(_runtime(tmp_path))
        with TestClient(app) as c:
            r = c.post("/api/connectors/unknown-key-test/settings",
                       json={"host": "x", "not_a_real_key": 99})
            assert r.status_code == 400
            body = r.json()
            assert body["ok"] is False
            assert "unknown" in body["error"].lower()
    finally:
        _unregister("unknown-key-test")


async def test_masked_secret_leaves_value_unchanged(tmp_path: Path) -> None:
    """Posting '•••' for a secret field does NOT overwrite the stored value."""
    _register_settings_dummy("secret-mask-test")
    try:
        rt = _runtime(tmp_path)
        # First set a real secret
        await rt.set_connector_settings("secret-mask-test", {
            "host": "h", "port": 1, "ratio": 1.0, "flag": False,
            "token": "real_secret_value",
        })
        stored_before = load_grants(tmp_path)["secret-mask-test"]["settings"]["token"]
        assert stored_before == "real_secret_value"

        # Now post with the masked placeholder — secret must be unchanged
        await rt.set_connector_settings("secret-mask-test", {
            "host": "h2", "port": 2, "ratio": 1.0, "flag": False,
            "token": "•••",
        })
        stored_after = load_grants(tmp_path)["secret-mask-test"]["settings"]["token"]
        assert stored_after == "real_secret_value", (
            "'•••' must not clobber the stored secret"
        )
    finally:
        _unregister("secret-mask-test")


async def test_unknown_stored_keys_survive_save(tmp_path: Path) -> None:
    """Keys in the grant store that are not in the schema must survive a save."""
    _register_settings_dummy("unknown-stored-test")
    try:
        rt = _runtime(tmp_path)
        # Manually plant an extra key in the grant store
        save_grants(tmp_path, {
            "unknown-stored-test": {
                "enabled": False,
                "manifest_hash": "",
                "settings": {"host": "h", "port": 80, "legacy_key": "preserve_me"},
            }
        })
        rt._connector_grants = load_grants(tmp_path)

        await rt.set_connector_settings("unknown-stored-test", {"host": "new-host"})
        stored = load_grants(tmp_path)["unknown-stored-test"]["settings"]
        assert stored.get("legacy_key") == "preserve_me", (
            "unknown stored keys must survive settings save"
        )
        assert stored["host"] == "new-host"
    finally:
        _unregister("unknown-stored-test")


# --------------------------------------------------------------------------- #
# 3. Live-apply: running connector restarted with new settings
# --------------------------------------------------------------------------- #


async def test_live_apply_restarts_running_connector(tmp_path: Path) -> None:
    """Saving settings on a running connector stops+rebuilds+starts it with new values."""
    _register_settings_dummy("live-apply-test")
    try:
        rt = _runtime(tmp_path)
        # Arm and start
        await rt.set_connector_armed("live-apply-test", True)
        old_conn = _LAST_BUILT.get("live-apply-test")
        assert old_conn is not None and old_conn.started

        # Save new settings
        result = await rt.set_connector_settings("live-apply-test", {
            "host": "newhost", "port": 9999, "ratio": 3.0, "flag": True, "token": "",
        })
        assert result["ok"] is True
        assert result["running"] is True
        assert not result.get("needs_reconsent", False)

        new_conn = _LAST_BUILT.get("live-apply-test")
        assert new_conn is not None
        assert new_conn is not old_conn, "must have built a new instance"
        assert new_conn.host == "newhost"
        assert new_conn.port == 9999
        assert old_conn.stopped, "old instance must have been stopped"
    finally:
        conn = rt.connectors.pop("live-apply-test", None)
        if conn:
            await conn.stop()
        _unregister("live-apply-test")


async def test_unarmed_connector_settings_persist_no_start(tmp_path: Path) -> None:
    """Saving settings on an unarmed connector persists values without starting it."""
    _register_settings_dummy("unarmed-persist-test")
    try:
        rt = _runtime(tmp_path)
        result = await rt.set_connector_settings("unarmed-persist-test", {
            "host": "persist-host", "port": 7777,
            "ratio": 0.5, "flag": False, "token": "",
        })
        assert result["ok"] is True
        assert result["running"] is False

        stored = load_grants(tmp_path)["unarmed-persist-test"]["settings"]
        assert stored["host"] == "persist-host"
        assert "unarmed-persist-test" not in rt.connectors
    finally:
        _unregister("unarmed-persist-test")


# --------------------------------------------------------------------------- #
# 4. Manifest-changing setting: nmea2000 thruster_control → disarms
# --------------------------------------------------------------------------- #


async def test_thruster_control_flip_disarms_and_needs_reconsent(tmp_path: Path) -> None:
    """Flipping nmea2000 thruster_control changes the manifest hash → disarms
    the connector and signals needs_reconsent=True in the response."""
    from vanchor.connectors.nmea2000 import build_manifest, MANIFEST

    rt = _runtime(tmp_path)

    # Arm the connector with thruster_control=False (the default manifest)
    result_arm = await rt.set_connector_armed("nmea2000", True)
    assert result_arm["ok"] is True

    grants_before = load_grants(tmp_path)
    hash_before = grants_before["nmea2000"]["manifest_hash"]
    assert hash_before == manifest_hash(MANIFEST), (
        "armed hash must match the no-thruster manifest"
    )

    # Flip thruster_control = True via settings
    result = await rt.set_connector_settings("nmea2000", {"thruster_control": True})
    assert result["ok"] is True
    assert result["needs_reconsent"] is True

    # Connector should NOT be running (stopped due to manifest change)
    assert "nmea2000" not in rt.connectors

    # Next GET should show needs_reconsent=True, armed=False
    status = rt.connector_status()
    entry = next((e for e in status if e["name"] == "nmea2000"), None)
    assert entry is not None
    assert entry["armed"] is False
    assert entry["needs_reconsent"] is True


# --------------------------------------------------------------------------- #
# 5. Normal settings save does NOT change armed state
# --------------------------------------------------------------------------- #


async def test_normal_settings_save_preserves_armed_state(tmp_path: Path) -> None:
    """Saving a non-manifest setting (e.g. 'host') must not disarm the connector."""
    _register_settings_dummy("no-disarm-test")
    try:
        rt = _runtime(tmp_path)
        await rt.set_connector_armed("no-disarm-test", True)

        result = await rt.set_connector_settings("no-disarm-test", {
            "host": "changed-host", "port": 4444,
            "ratio": 1.0, "flag": False, "token": "",
        })
        assert result["ok"] is True
        assert not result.get("needs_reconsent", False)

        # Still armed
        status = rt.connector_status()
        entry = next((e for e in status if e["name"] == "no-disarm-test"), None)
        assert entry is not None
        assert entry["armed"] is True, "normal settings save must not disarm"
    finally:
        conn = rt.connectors.pop("no-disarm-test", None)
        if conn:
            await conn.stop()
        _unregister("no-disarm-test")


# --------------------------------------------------------------------------- #
# 6. Legacy nmea-tcp boot re-sync skipped after user_edited save
# --------------------------------------------------------------------------- #


def test_user_edited_prevents_nmea_tcp_resync(tmp_path: Path) -> None:
    """After the user saves custom host/port via set_connector_settings,
    the boot re-sync must NOT clobber them on the next Runtime init."""
    from vanchor.connectors.nmea_tcp import MANIFEST as _NMEA_TCP_MANIFEST

    # Plant a grant with user_edited=True and a custom port
    user_port = 19999
    save_grants(tmp_path, {
        "nmea-tcp": {
            "enabled": True,
            "manifest_hash": manifest_hash(_NMEA_TCP_MANIFEST),
            "settings": {
                "host": "0.0.0.0",
                "port": user_port,
                "user_edited": True,
            },
        }
    })

    # Boot a new Runtime with a DIFFERENT cfg port — should NOT resync
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.nmea_tcp.host = "0.0.0.0"
    cfg.nmea_tcp.port = 10110   # different from user_port
    Runtime(cfg)

    updated = load_grants(tmp_path)
    assert updated["nmea-tcp"]["settings"]["port"] == user_port, (
        "user_edited flag must prevent the cfg re-sync from clobbering the user's port"
    )


def test_no_user_edited_still_resyncs(tmp_path: Path) -> None:
    """Without user_edited, the boot re-sync still overwrites stale host/port."""
    from vanchor.connectors.nmea_tcp import MANIFEST as _NMEA_TCP_MANIFEST

    save_grants(tmp_path, {
        "nmea-tcp": {
            "enabled": True,
            "manifest_hash": manifest_hash(_NMEA_TCP_MANIFEST),
            "settings": {"host": "0.0.0.0", "port": 10110},
        }
    })

    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.nmea_tcp.host = "0.0.0.0"
    cfg.nmea_tcp.port = 12345
    Runtime(cfg)

    updated = load_grants(tmp_path)
    assert updated["nmea-tcp"]["settings"]["port"] == 12345, (
        "without user_edited, cfg changes must still resync"
    )


# --------------------------------------------------------------------------- #
# 7. API endpoint round-trip
# --------------------------------------------------------------------------- #


def test_api_settings_endpoint_saves(tmp_path: Path) -> None:
    """POST /api/connectors/{name}/settings persists settings and returns {ok}."""
    _register_settings_dummy("api-settings-test")
    try:
        app = create_app(_runtime(tmp_path))
        with TestClient(app) as c:
            r = c.post("/api/connectors/api-settings-test/settings",
                       json={"host": "api-host", "port": 1234,
                             "ratio": 0.75, "flag": False, "token": ""})
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True

        # Verify persisted
        stored = load_grants(tmp_path)["api-settings-test"]["settings"]
        assert stored["host"] == "api-host"
        assert stored["port"] == 1234
        assert stored["user_edited"] is True
    finally:
        _unregister("api-settings-test")


def test_api_settings_unknown_key_400(tmp_path: Path) -> None:
    """POST with an unknown key returns 400."""
    _register_settings_dummy("api-settings-bad-key")
    try:
        app = create_app(_runtime(tmp_path))
        with TestClient(app) as c:
            r = c.post("/api/connectors/api-settings-bad-key/settings",
                       json={"host": "h", "not_in_schema": "x"})
            assert r.status_code == 400
            assert r.json()["ok"] is False
    finally:
        _unregister("api-settings-bad-key")


def test_api_settings_unknown_connector_400(tmp_path: Path) -> None:
    """POST for an unknown connector name returns 400."""
    app = create_app(_runtime(tmp_path))
    with TestClient(app) as c:
        r = c.post("/api/connectors/no-such-connector-xyz/settings",
                   json={"host": "x"})
        assert r.status_code == 400
        assert r.json()["ok"] is False


# --------------------------------------------------------------------------- #
# 8. user_edited persisted and returned in grant store
# --------------------------------------------------------------------------- #


async def test_user_edited_flag_is_set(tmp_path: Path) -> None:
    """set_connector_settings always writes user_edited=True to the grant."""
    _register_settings_dummy("user-edited-flag-test")
    try:
        rt = _runtime(tmp_path)
        await rt.set_connector_settings("user-edited-flag-test", {
            "host": "test", "port": 80, "ratio": 1.0, "flag": False, "token": "",
        })
        stored = load_grants(tmp_path)["user-edited-flag-test"]["settings"]
        assert stored.get("user_edited") is True
    finally:
        _unregister("user-edited-flag-test")
