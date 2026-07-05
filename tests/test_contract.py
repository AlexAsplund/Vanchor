"""The API contract (vanchor.core.contract) stays honest: every telemetry key
the server emits and every command the controller accepts must be declared, so
the self-describing /api/contract can't silently drift out of date."""
import re
from pathlib import Path

from vanchor.app import Runtime
from vanchor.core import contract
from vanchor.core.config import load


def test_every_emitted_telemetry_key_is_declared():
    keys = set(Runtime(load(None)).telemetry().keys())
    undeclared = keys - set(contract.TELEMETRY_FIELDS)
    assert not undeclared, (
        f"telemetry emits undeclared keys {sorted(undeclared)} -- add them to "
        "vanchor.core.contract.TELEMETRY_FIELDS")


def test_declared_telemetry_fields_have_type_and_desc():
    for key, meta in contract.TELEMETRY_FIELDS.items():
        assert "type" in meta and "desc" in meta, f"{key} missing type/desc"


def test_every_controller_command_is_declared():
    src = Path("src/vanchor/controller/controller.py").read_text()
    ctypes = set(re.findall(r'ctype == "([a-z_]+)"', src))
    assert ctypes, "no ctypes found -- test needs updating"
    undeclared = ctypes - set(contract.COMMANDS)
    assert not undeclared, (
        f"controller handles undeclared commands {sorted(undeclared)} -- add "
        "them to vanchor.core.contract.COMMANDS")


def test_build_contract_is_versioned_and_self_describing():
    c = contract.build_contract(envelope_version=3)
    assert c["schema_version"] == contract.SCHEMA_VERSION
    assert c["envelope_version"] == 3
    assert "units" in c
    assert c["telemetry"] and c["commands"]
