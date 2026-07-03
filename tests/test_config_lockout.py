"""Tests for the non-negotiable safety-floor config lockout (roadmap #50).

The safety floor (loss-of-fix failsafe enable + the shallow-water min-depth stop)
is captured from the BASE/startup config. Wherever config is later merged,
reloaded, or applied -- a runtime Settings edit, the persisted safety geometry, a
backup-restore -- a locked key may be ratcheted SAFER but can never be WEAKENED
below its startup value. Non-locked keys still hot-reload normally.
"""

from __future__ import annotations

from vanchor.app import Runtime
from vanchor.core.config import SAFETY_FLOOR_KEYS, SafetyConfig, SafetyFloor, load
from vanchor.core.prefs import SafetyGeometryStore


# --------------------------------------------------------------------------- #
# Pure SafetyFloor policy
# --------------------------------------------------------------------------- #
def test_floor_refuses_disabling_a_locked_failsafe():
    floor = SafetyFloor(fix_failsafe_enabled=True, min_depth_m=0.0)
    # A later update trying to DISABLE the failsafe is refused (clamped ON).
    assert floor.enforce_fix_failsafe(False) is True
    # Leaving it on is fine.
    assert floor.enforce_fix_failsafe(True) is True
    # None (= "not changing it") keeps the floor value.
    assert floor.enforce_fix_failsafe(None) is True


def test_floor_allows_failsafe_off_when_base_was_off():
    # If the base config never enabled the failsafe, there is nothing to lock: a
    # boat that intentionally boots with it off is not forced on.
    floor = SafetyFloor(fix_failsafe_enabled=False, min_depth_m=0.0)
    assert floor.enforce_fix_failsafe(False) is False
    assert floor.enforce_fix_failsafe(True) is True  # can still be tightened


def test_floor_refuses_lowering_min_depth_below_base():
    floor = SafetyFloor(fix_failsafe_enabled=True, min_depth_m=2.0)
    # Lowering the stop below the startup floor is refused (kept at the floor).
    assert floor.enforce_min_depth(0.5) == 2.0
    assert floor.enforce_min_depth(0.0) == 2.0
    # Raising it (a safer, earlier stop) is allowed.
    assert floor.enforce_min_depth(3.0) == 3.0
    # None keeps the floor.
    assert floor.enforce_min_depth(None) == 2.0


def test_floor_sanitize_partial_update():
    floor = SafetyFloor(fix_failsafe_enabled=True, min_depth_m=2.0)
    out = floor.sanitize({"fix_failsafe_enabled": False, "min_depth_m": 0.1})
    assert out == {"fix_failsafe_enabled": True, "min_depth_m": 2.0}
    # Keys not present pass through untouched (non-safety partial update).
    assert floor.sanitize({"something_else": 5}) == {"something_else": 5}


def test_floor_from_config_captures_base_values():
    floor = SafetyFloor.from_config(SafetyConfig(fix_failsafe_enabled=True, min_depth_m=1.5))
    assert floor.fix_failsafe_enabled is True
    assert floor.min_depth_m == 1.5


def test_safety_floor_keys_are_named():
    assert "fix_failsafe_enabled" in SAFETY_FLOOR_KEYS
    assert "min_depth_m" in SAFETY_FLOOR_KEYS


# --------------------------------------------------------------------------- #
# Runtime command path: a Settings edit can't weaken a failsafe
# --------------------------------------------------------------------------- #
def _runtime(tmp_path, *, min_depth=2.0, failsafe=True) -> Runtime:
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    cfg.safety.min_depth_m = min_depth
    cfg.safety.fix_failsafe_enabled = failsafe
    return Runtime(cfg)


def test_command_cannot_disable_locked_fix_failsafe(tmp_path):
    rt = _runtime(tmp_path, failsafe=True)
    assert rt.controller.safety.config.fix_failsafe_enabled is True
    # Try to disable it via the same command path the UI uses -> refused.
    rt.handle_command({"type": "set_fix_failsafe", "enabled": False})
    assert rt.controller.safety.config.fix_failsafe_enabled is True
    # The persisted store also holds the floored (safe) value.
    assert rt.safety_geometry.fix_failsafe_enabled is True


def test_command_cannot_lower_locked_min_depth(tmp_path):
    rt = _runtime(tmp_path, min_depth=2.0)
    assert rt.controller.safety.config.min_depth_m == 2.0
    rt.handle_command({"type": "set_min_depth", "min_depth_m": 0.5})
    assert rt.controller.safety.config.min_depth_m == 2.0
    assert rt.safety_geometry.min_depth_m == 2.0


def test_bus_command_cannot_weaken_failsafe_at_controller(tmp_path):
    # #50 defense in depth: a command delivered on the bus "command" topic reaches
    # Controller.handle_command directly, bypassing Runtime.handle_command's floor
    # check. The floor must ALSO be enforced at the controller's mutation site.
    rt = _runtime(tmp_path, min_depth=2.0, failsafe=True)
    gov = rt.controller.safety

    # Disabling the loss-of-fix failsafe over the bus is refused.
    rt.controller.handle_command({"type": "set_fix_failsafe", "enabled": False})
    assert gov.config.fix_failsafe_enabled is True

    # Lowering min-depth below the startup floor over the bus is refused...
    rt.controller.handle_command({"type": "set_min_depth", "min_depth_m": 0.5})
    assert gov.config.min_depth_m == 2.0
    # ...but a tighten (safer) still applies.
    rt.controller.handle_command({"type": "set_min_depth", "min_depth_m": 3.5})
    assert gov.config.min_depth_m == 3.5


def test_command_can_still_raise_min_depth(tmp_path):
    # Non-weakening edits (a SAFER limit) still hot-reload normally.
    rt = _runtime(tmp_path, min_depth=2.0)
    rt.handle_command({"type": "set_min_depth", "min_depth_m": 5.0})
    assert rt.controller.safety.config.min_depth_m == 5.0
    assert rt.safety_geometry.min_depth_m == 5.0


def test_nonlocked_geometry_still_hot_reloads(tmp_path):
    # No-go zones are not a floor key; they apply live as before.
    rt = _runtime(tmp_path)
    square = [[59.0, 18.0], [59.0, 18.001], [59.001, 18.001], [59.001, 18.0]]
    rt.handle_command({"type": "set_nogo_zones", "zones": [square]})
    assert rt.controller.safety.nogo_zone_count == 1


def test_base_off_failsafe_can_be_enabled_at_runtime(tmp_path):
    # A base config with the failsafe OFF is not locked on; enabling it (tighter)
    # works, and it can then not be turned back off (now it is the floor)... but
    # the floor was captured at startup as OFF, so a later disable IS allowed.
    rt = _runtime(tmp_path, failsafe=False)
    assert rt.controller.safety.config.fix_failsafe_enabled is False
    rt.handle_command({"type": "set_fix_failsafe", "enabled": True})
    assert rt.controller.safety.config.fix_failsafe_enabled is True


# --------------------------------------------------------------------------- #
# Persisted geometry / restore path: a restored store can't weaken the floor
# --------------------------------------------------------------------------- #
def test_persisted_geometry_cannot_weaken_floor(tmp_path):
    # Simulate a backup/restore (or a hand-edited safety.json) that tries to
    # DISABLE the failsafe and LOWER the min-depth below the startup floor.
    store = SafetyGeometryStore(str(tmp_path))
    store.set_min_depth(0.5)
    store.set_fix_failsafe(False)

    # Boot a runtime whose startup floor is failsafe ON + min-depth 2.0. The
    # persisted (weakening) geometry is applied through the floor.
    rt = _runtime(tmp_path, min_depth=2.0, failsafe=True)
    assert rt.controller.safety.config.fix_failsafe_enabled is True
    assert rt.controller.safety.config.min_depth_m == 2.0


def test_persisted_geometry_can_still_tighten(tmp_path):
    # A restored store RAISING min-depth (safer) is honoured.
    store = SafetyGeometryStore(str(tmp_path))
    store.set_min_depth(4.0)
    rt = _runtime(tmp_path, min_depth=2.0, failsafe=True)
    assert rt.controller.safety.config.min_depth_m == 4.0
