"""Helm-Pico firmware flasher: UF2 validation, picotool runner, endpoints."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.hardware import pico_flash
from vanchor.ui.server import create_app


def make_uf2(blocks: int = 2) -> bytes:
    """A minimally valid UF2 image: n 512-byte blocks with both magics."""
    block = bytearray(512)
    block[0:4] = b"UF2\n"
    block[4:8] = (0x9E5D5157).to_bytes(4, "little")
    return bytes(block) * blocks


@pytest.fixture()
def rt():
    return Runtime(load(None))


@pytest.fixture()
def client(rt):
    with TestClient(create_app(rt)) as c:
        yield c


class TestValidateUf2:
    def test_valid_image_passes(self):
        assert pico_flash.validate_uf2(make_uf2()) is None

    def test_empty_rejected(self):
        assert pico_flash.validate_uf2(b"") is not None

    def test_wrong_magic_rejected(self):
        bad = bytearray(make_uf2())
        bad[0] = 0x00
        assert "magic" in pico_flash.validate_uf2(bytes(bad))

    def test_unaligned_size_rejected(self):
        assert "512" in pico_flash.validate_uf2(make_uf2() + b"x")

    def test_html_error_page_rejected(self):
        assert pico_flash.validate_uf2(b"<html>Not Found</html>" * 32) is not None

    def test_oversized_rejected(self):
        blocks = pico_flash.MAX_UF2_BYTES // 512 + 1
        assert "large" in pico_flash.validate_uf2(make_uf2(blocks))


class TestFlashRunner:
    def test_missing_picotool_is_clear(self, monkeypatch):
        monkeypatch.setattr(pico_flash, "picotool_path", lambda: None)
        ok, out = pico_flash.flash("/tmp/x.uf2")
        assert not ok and "picotool is not installed" in out

    def test_success_passes_flags(self, monkeypatch):
        monkeypatch.setattr(pico_flash, "picotool_path", lambda: "/usr/bin/picotool")
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="Loading... OK", stderr="")

        ok, out = pico_flash.flash("/tmp/x.uf2", run=fake_run)
        assert ok and "OK" in out
        # -f = reboot the running app into BOOTSEL; -x = reboot into the app.
        assert seen["cmd"][1:4] == ["load", "-f", "-x"]

    def test_no_device_gets_reflash_hint(self, monkeypatch):
        monkeypatch.setattr(pico_flash, "picotool_path", lambda: "/usr/bin/picotool")

        def fake_run(cmd, **kw):
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr="No accessible RP-series devices")

        ok, out = pico_flash.flash("/tmp/x.uf2", run=fake_run)
        assert not ok and "reflash the SD card" in out

    def test_timeout_is_reported(self, monkeypatch):
        monkeypatch.setattr(pico_flash, "picotool_path", lambda: "/usr/bin/picotool")

        def fake_run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 120)

        ok, out = pico_flash.flash("/tmp/x.uf2", run=fake_run)
        assert not ok and "timed out" in out


class TestFlashEndpoint:
    def test_status_reports_picotool(self, client):
        r = client.get("/api/hw/pico/flash")
        assert r.status_code == 200
        assert isinstance(r.json()["picotool"], bool)

    def test_garbage_upload_is_400(self, client):
        r = client.post("/api/hw/pico/flash",
                        files={"file": ("x.uf2", b"not a firmware")})
        assert r.status_code == 400
        assert r.json()["ok"] is False

    def test_underway_is_409(self, rt, client, monkeypatch):
        rt.state.mode = "anchor"
        r = client.post("/api/hw/pico/flash",
                        files={"file": ("x.uf2", make_uf2())})
        assert r.status_code == 409
        assert r.json()["error"] == "underway"

    def test_valid_flash_invokes_runner(self, rt, client, monkeypatch, tmp_path):
        called = {}

        def fake_flash(path):
            called["path"] = path
            return True, "Loading... OK"

        monkeypatch.setattr(pico_flash, "flash", fake_flash)
        r = client.post("/api/hw/pico/flash",
                        files={"file": ("fw.uf2", make_uf2())})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert called["path"].endswith("helm-firmware.uf2")

    def test_force_overrides_underway(self, rt, client, monkeypatch):
        rt.state.mode = "anchor"
        monkeypatch.setattr(pico_flash, "flash", lambda p: (True, "OK"))
        r = client.post("/api/hw/pico/flash?force=true",
                        files={"file": ("fw.uf2", make_uf2())})
        assert r.status_code == 200
        assert r.json()["ok"] is True
