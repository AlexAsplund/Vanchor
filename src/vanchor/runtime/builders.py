"""Boat-parameter and device-config builder helpers used by the Runtime.

These are pure functions that translate an ``AppConfig`` (or sub-sections of
it) into the concrete domain objects expected by the simulator, navigator, and
controller.  They live here — rather than in ``app.py`` — so ``app.py`` can
stay focused on wiring and lifecycle management.
"""

from __future__ import annotations

import math


def _build_boat_params(cfg):
    """Build the physics-model parameters for the configured boat geometry."""
    bc = cfg.boat
    if cfg.sim.model == "fossen":
        from ..sim.fossen import FossenParams

        return FossenParams(
            length=bc.length_m,
            beam=bc.beam_m,
            mass=bc.mass_kg,
            max_thrust_n=bc.max_thrust_n,
            reverse_efficiency=bc.reverse_efficiency,
            max_speed_mps=bc.max_speed_mps,
            thruster_x_m=bc.thruster_x_m(),
            thruster_y_m=bc.thruster_y_m,
            max_steer_angle_deg=bc.max_steer_angle_deg,
            hull_tracking=bc.hull_tracking,
        )
    from ..sim.boat import BoatParams

    return BoatParams(
        max_speed_mps=bc.max_speed_mps,
        max_turn_rate_deg=bc.max_turn_rate_deg,
        reverse_efficiency=bc.reverse_efficiency,
    )


def _thrust_yaw_ff_norm(cfg) -> float:
    """Thrust-yaw feed-forward as a steering-command fraction.

    The boat config gives the cancelling deflection in radians; the helm command
    is a fraction of the full mechanical swing (``max_steer_angle_deg``, the same
    range the sim maps the command onto), so normalise by that. ``steer_sign`` is
    applied by the helm, not here.
    """
    bc = cfg.boat
    if bc.max_steer_angle_deg <= 0:
        return 0.0
    return bc.thrust_yaw_ff_angle() / math.radians(bc.max_steer_angle_deg)


def _make_fusion():
    """A GNSS/INS complementary fusion filter (M9N UBX + HWT901B IMU)."""
    from ..nav.fusion import NavFusion
    return NavFusion()


def _make_gps_filter():
    """An accuracy-weighted GPS position low-pass (nav.gps_filter)."""
    from ..nav.gps_filter import GpsPositionFilter
    return GpsPositionFilter()


def _build_battery_config(cfg):
    """Map the app `battery:` config onto the sim battery model (#60)."""
    from ..sim.battery import BatteryConfig as SimBatteryConfig

    b = cfg.battery
    return SimBatteryConfig(
        capacity_ah=b.capacity_ah,
        nominal_v=b.nominal_v,
        reserve_pct=b.reserve_pct,
        # Pass the recent-draw smoothing time constant through so YAML tuning of
        # the range/time-to-empty estimate actually takes effect (#10); without
        # this the sim battery silently kept its default draw_tau_s.
        draw_tau_s=b.draw_tau_s,
    )


def _mask_connector_settings(schema: list, stored: dict) -> dict:
    """Build a display-safe settings dict from ``schema`` and ``stored`` values.

    For each field in ``schema``:
    - The value is taken from ``stored`` if present, else from the field's
      ``default``.
    - Secret fields (``secret: True``) are masked: ``"•••"`` when the stored
      value is non-empty, ``""`` when it is empty/absent.
    - Internal runtime keys (``data_dir``, ``user_edited``) are never included.

    Returns a ``{key: value}`` dict covering every schema field.
    """
    result: dict = {}
    for field in schema:
        key = field.get("key")
        if not key:
            continue
        default = field.get("default", "")
        val = stored.get(key, default)
        if field.get("secret"):
            result[key] = "•••" if val else ""
        else:
            result[key] = val
    return result


def _overlay_menu_values(schema: dict, saved: dict) -> dict:
    """Return a copy of a device-menu ``schema`` with each setting's ``value``
    replaced by the saved value for that key (when present) -- so the UI shows
    persisted choices, not just factory defaults."""
    settings = []
    for s in schema.get("settings", []):
        s = dict(s)
        if s.get("key") in saved:
            s["value"] = saved[s["key"]]
        settings.append(s)
    return {**schema, "settings": settings, "actions": list(schema.get("actions", []))}
