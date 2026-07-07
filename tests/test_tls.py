"""HTTPS/TLS plumbing: cert auto-generation, reuse, config paths, port probe."""
import shutil
import socket
import subprocess

import pytest

from vanchor.tls import CERT_NAME, KEY_NAME, ensure_tls_cert, port_free

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None,
                                   reason="openssl binary not available")


@needs_openssl
def test_autogenerates_self_signed_cert_with_vanchor_local_cn(tmp_path):
    pair = ensure_tls_cert(tmp_path)
    assert pair is not None
    cert, key = pair
    assert cert.endswith(CERT_NAME) and key.endswith(KEY_NAME)
    text = subprocess.run(["openssl", "x509", "-in", cert, "-noout", "-subject",
                           "-ext", "subjectAltName"],
                          capture_output=True, text=True, check=True).stdout
    assert "CN = vanchor.local" in text or "CN=vanchor.local" in text
    assert "vanchor.local" in text and "localhost" in text and "127.0.0.1" in text


@needs_openssl
def test_reuses_existing_cert(tmp_path):
    first = ensure_tls_cert(tmp_path)
    mtime = (tmp_path / "tls" / CERT_NAME).stat().st_mtime_ns
    second = ensure_tls_cert(tmp_path)
    assert first == second
    assert (tmp_path / "tls" / CERT_NAME).stat().st_mtime_ns == mtime  # not regenerated


def test_configured_paths_win(tmp_path):
    c = tmp_path / "my.crt"
    k = tmp_path / "my.key"
    c.write_text("x")
    k.write_text("y")
    assert ensure_tls_cert(tmp_path, str(c), str(k)) == (str(c), str(k))


def test_missing_configured_paths_disable_https(tmp_path):
    assert ensure_tls_cert(tmp_path, str(tmp_path / "no.crt"), str(tmp_path / "no.key")) is None
    assert not (tmp_path / "tls").exists()  # no auto-gen fallback when configured


def test_port_free_probe():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        taken = s.getsockname()[1]
        assert port_free("127.0.0.1", taken) is False
    assert port_free("127.0.0.1", taken) is True  # released after close
