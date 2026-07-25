"""Command-audit / dispatch cluster extracted from Runtime (issue #77).

The 3 methods that handle the command audit ring and command dispatch live here.
``CommandDispatcher`` holds a back-reference to ``Runtime`` via ``self._rt``
for shared state that remains on Runtime.

Private state ownership:
  ``_command_audit`` — stays on Runtime (referenced by record_command shim +
  tests that call rt.record_command / rt.command_audit directly).

Monkeypatch-seam rule: tests patch ``rt.handle_command`` and related methods
on the Runtime shim (e.g. ``test_connectors_runtime.py`` replaces
``rt.handle_command`` with a spy). Every method of CommandDispatcher that
dispatches to Runtime methods routes through ``self._rt.<m>()`` so that
any test-time patch on ``rt`` is honoured. Calls to the 3 command methods
themselves stay ``self.<m>()`` (intra-cluster).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time

from ..core import events

logger = logging.getLogger("vanchor.app")


class CommandDispatcher:
    """Command-audit / dispatch cluster -- split out of Runtime."""

    def __init__(self, rt) -> None:
        self._rt = rt   # back-reference to Runtime for shared state

    # ------------------------------------------------------------------ #
    # Command audit log (#26)
    # ------------------------------------------------------------------ #

    def record_command(
        self, ctype: object, source: str, outcome: str, detail: str | None = None
    ) -> None:
        """Append one command to the bounded audit ring (#26).

        ``source`` is "helm"|"observer"|"rest"; ``outcome`` is
        "accepted"|"denied"|"error" (+ an optional short ``detail`` on error).
        Called from the command entry points (WS handler + REST /api/command).
        Pings (and typeless messages) are never recorded so the audit stays a
        log of real commands. Uses the wall clock so the timestamp is displayable.
        """
        rt = self._rt
        if ctype in (None, "", "ping"):
            return
        entry = {
            "ts": rt._now_fn(),
            "type": str(ctype),
            "source": source,
            "outcome": outcome,
        }
        if detail:
            entry["detail"] = str(detail)[:200]
        rt._command_audit.append(entry)

    def command_audit(self, n: int = 50) -> dict:
        """Return the most recent ``n`` audited commands, oldest first / newest
        last. ``n`` is clamped to [1, 200] (the ring size)."""
        rt = self._rt
        n = max(1, min(int(n), 200))
        return {"commands": list(rt._command_audit)[-n:]}

    # ------------------------------------------------------------------ #
    # Command dispatch
    # ------------------------------------------------------------------ #

    def handle_command(self, command: dict) -> None:
        rt = self._rt
        ctype = command.get("type")
        if rt.debug.active and ctype not in (None,):
            rt.debug.write("command", command, time.time())
        if ctype == "set_environment" and rt.simulator is not None:
            env = rt.simulator.environment
            for key in (
                "current_speed",
                "current_dir",
                "wind_speed",
                "wind_dir",
                "gust_amplitude_mps",
                "wind_variability",
                "current_variability",
            ):
                if key in command:
                    setattr(env, key, float(command[key]))
            # Re-anchor the slow weather wander on the new base values.
            rt.simulator.set_weather_base()
            rt._save_environment()
        elif ctype == "weather_preset" and rt.simulator is not None:
            rt._apply_weather_preset(str(command.get("id", "")))
        elif ctype == "sim_fault":
            rt.set_sim_fault(
                str(command.get("name", "")),
                bool(command.get("enabled", True)),
                **{k: v for k, v in command.items()
                   if k not in ("type", "name", "enabled")},
            )
        elif ctype == "teleport":
            rt._teleport(command)
        elif ctype == "inject_nmea":
            asyncio.ensure_future(rt.bus.publish(events.NMEA_IN, str(command["sentence"])))
        elif ctype == "set_gps_offset":
            # On a SIMULATED GPS, "adjust my position" must MOVE the boat, not
            # install an offset: the sim GPS has no bias to correct, so an
            # offset would shift the PERCEIVED frame away from the sim truth —
            # the depth sounder keeps sampling truth, and chart-relative
            # behaviours (contour follow, charted depth, divergence) then run
            # displaced by exactly the offset (field report: contour followed
            # "at its original unadjusted position"). Real GPS sources keep the
            # normal offset calibration — there the offset corrects a real
            # bias, so perception and physics align.
            if type(rt.gps).__name__ == "SimGps" and rt.simulator is not None:
                rt.navigator.clear_gps_offset()
                rt._teleport({
                    "lat": float(command["true_lat"]),
                    "lon": float(command["true_lon"]),
                })
                logger.info("gps offset on sim GPS -> teleported the sim boat instead")
            else:
                rt.navigator.set_gps_offset(
                    float(command["true_lat"]), float(command["true_lon"])
                )
        elif ctype == "clear_gps_offset":
            rt.navigator.clear_gps_offset()
        elif ctype == "set_land_guard":
            gov = rt.controller.safety
            enabled = command.get("enabled")
            margin = command.get("margin_m")
            if enabled is not None:
                gov.config.land_guard_enabled = bool(enabled)
            if margin is not None:
                gov.config.land_guard_margin_m = max(1.0, min(200.0, float(margin)))
            rt.safety_geometry.set_land_guard(
                None if enabled is None else bool(enabled),
                None if margin is None else gov.config.land_guard_margin_m,
            )
            logger.info("land guard: enabled=%s margin=%.0fm",
                        gov.config.land_guard_enabled,
                        gov.config.land_guard_margin_m)
        elif ctype == "anchor_alarm_set":
            # Drop the ALARM anchor (passive watch circle; NO motor involvement).
            # Explicit lat/lon wins (future map-tap placement); default = the
            # boat's current position. Refused without a position.
            from ..core.models import GeoPoint
            lat, lon = command.get("lat"), command.get("lon")
            if lat is not None and lon is not None:
                point = GeoPoint(float(lat), float(lon))
            else:
                pos = rt.state.position
                if pos is None or pos.is_null():
                    logger.warning("anchor_alarm_set ignored: no position fix")
                    return
                point = pos
            radius = float(command.get(
                "radius_m", rt.config.safety.anchor_alarm_default_radius_m))
            snap = rt.anchor_alarm.set(point, radius, now=rt._now_fn())
            logger.info("anchor alarm ARMED: %.5f, %.5f r=%.0fm",
                        point.lat, point.lon, snap["radius_m"])
        elif ctype == "anchor_alarm_clear":
            rt.anchor_alarm.clear()
            logger.info("anchor alarm cleared")
        elif ctype == "anchor_alarm_recover":
            rt._anchor_alarm_recover()
        elif ctype == "set_auto_apb":
            enabled = bool(command.get("enabled", False))
            rt.config.safety.auto_follow_apb = enabled
            rt.safety_geometry.set_auto_follow_apb(enabled)
            logger.info("auto Follow-APB %s", "enabled" if enabled else "disabled")
        elif ctype == "load_route":
            rt._load_route(command)
        elif ctype == "set_battery":
            rt._set_battery(command.get("soc_pct"))
        elif ctype == "return_to_launch":
            rt.return_to_launch()
        elif ctype == "trip_start":
            rt.trip_start(command.get("name"))
        elif ctype == "trip_stop":
            rt.trip_stop()
        elif ctype in ("set_nogo_zones", "set_min_depth", "set_fix_failsafe"):
            # Safety geometry: update the live governor (controller) AND persist
            # to the server-side store so it survives a restart (#23). The
            # governor stays the authority; we mirror its resulting state (for
            # min-depth/failsafe) and the raw command rings (for no-go zones,
            # which the governor keeps only as prepared shapely polygons).
            #
            # Safety-floor lockout (#50): a runtime Settings edit may make the
            # failsafes SAFER but never weaker -- clamp a disable / a lowered
            # min-depth back to the startup floor BEFORE it reaches the governor,
            # so the persisted mirror below also stores the floored value.
            if ctype == "set_min_depth":
                command = {
                    **command,
                    "min_depth_m": rt.safety_floor.enforce_min_depth(
                        command.get("min_depth_m", 0.0)
                    ),
                }
            elif ctype == "set_fix_failsafe":
                command = {
                    **command,
                    "enabled": rt.safety_floor.enforce_fix_failsafe(
                        command.get("enabled", False)
                    ),
                }
            rt.controller.handle_command(command)
            if ctype == "set_nogo_zones":
                rt.safety_geometry.set_nogo_zones(command.get("zones", []))
            elif ctype == "set_min_depth":
                rt.safety_geometry.set_min_depth(rt.controller.safety.config.min_depth_m)
            else:
                rt.safety_geometry.set_fix_failsafe(
                    rt.controller.safety.config.fix_failsafe_enabled
                )
        else:
            rt.controller.handle_command(command)
