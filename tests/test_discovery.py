"""mDNS discovery advertisement (vanchor.local auto-find)."""
import builtins

from vanchor import discovery
from vanchor.core.config import load


def test_mdns_is_on_by_default():
    assert load(None).server.mdns is True


def test_primary_ip_passthrough_for_a_specific_host():
    assert discovery._primary_ip("192.168.1.50") == "192.168.1.50"


def test_primary_ip_resolves_a_wildcard_bind():
    ip = discovery._primary_ip("0.0.0.0")
    assert ip is None or (isinstance(ip, str) and ip.count(".") == 3)


def test_advertise_is_a_graceful_noop_without_zeroconf(monkeypatch):
    real_import = builtins.__import__

    def _no_zeroconf(name, *a, **k):
        if name == "zeroconf":
            raise ImportError("simulated missing zeroconf")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_zeroconf)
    assert discovery.advertise(8000, "0.0.0.0") is None  # no crash, just skipped


def test_advertise_returns_a_closable_handle_or_none():
    # The real path: it should register (returns a handle) or gracefully skip
    # (None) -- never raise. Whatever it returns, close() must be safe.
    adv = discovery.advertise(0, "127.0.0.1", name="VanchorTest")
    try:
        assert adv is None or hasattr(adv, "close")
    finally:
        if adv is not None:
            adv.close()  # must not raise
