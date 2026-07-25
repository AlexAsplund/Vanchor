"""Tests for the extension kernel (:mod:`vanchor.ext`).

Covers the three pieces this PR ships as the shared plug-in machinery:
:func:`discover` (never raises with no packs), :class:`Registry`
(register/get/names, duplicate + api-version skips), and the :class:`Manifest`
frozen dataclass.
"""

from __future__ import annotations

import pytest

from vanchor.ext import Capability, Manifest, Registry, discover


# --------------------------------------------------------------------------- #
# discover
# --------------------------------------------------------------------------- #
def test_discover_noops_with_no_packs() -> None:
    """No packs installed under an unknown group -> a quiet no-op, never raises."""
    assert list(discover("vanchor._no_such_group_")) == []


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_registry_register_get_names_all() -> None:
    reg: Registry[object] = Registry("driver", api_version=1)
    a, b = object(), object()
    reg.register("alpha", a)
    reg.register("beta", b)
    assert reg.get("alpha") is a
    assert reg.names() == ["alpha", "beta"]   # sorted/stable
    assert reg.all() == {"alpha": a, "beta": b}


def test_registry_duplicate_name_is_skipped_not_raised() -> None:
    reg: Registry[object] = Registry("driver", api_version=1)
    first, second = object(), object()
    reg.register("dup", first)
    reg.register("dup", second)   # logged + skipped, no raise
    assert reg.get("dup") is first   # original kept


def test_registry_api_version_mismatch_is_skipped() -> None:
    reg: Registry[object] = Registry("driver", api_version=1)
    reg.register("mismatch", object(), api_version=2)   # logged + skipped
    assert "mismatch" not in reg.names()
    with pytest.raises(KeyError):
        reg.get("mismatch")


def test_registry_matching_api_version_registers() -> None:
    reg: Registry[object] = Registry("driver", api_version=1)
    dev = object()
    reg.register("ok", dev, api_version=1)
    assert reg.get("ok") is dev


# --------------------------------------------------------------------------- #
# Manifest / Capability
# --------------------------------------------------------------------------- #
def test_manifest_fields_and_defaults() -> None:
    m = Manifest(name="pack", version="1.2.3", kind="driver", api_version=1)
    assert (m.name, m.version, m.kind, m.api_version) == ("pack", "1.2.3", "driver", 1)
    assert m.capabilities == ()
    assert m.author == ""


def test_manifest_is_frozen_and_hashable() -> None:
    m = Manifest(
        name="pack", version="1", kind="connector", api_version=2,
        capabilities=("read",), author="alex",
    )
    assert hash(m) == hash(m)   # hashable (consent can key on it)
    with pytest.raises(Exception):
        m.name = "other"  # type: ignore[misc]  # frozen


def test_capability_is_a_marker_base() -> None:
    class MyCap(Capability):
        pass

    assert isinstance(MyCap(), Capability)
