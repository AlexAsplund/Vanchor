"""Telemetry-frame assembly extracted from Runtime (issue #74).

The 4 methods that build the per-frame telemetry snapshot -- ``battery_snapshot``,
``_supervisor_snapshot``, ``_health_snapshot``, and ``telemetry`` -- live here.
``TelemetryBuilder`` holds a back-reference to ``Runtime`` via ``self._rt`` for
shared state that remains on Runtime (state, config, bus, navigator, controller,
gps, compass, depth_map, battery_monitor, watchdog, supervisor_link, connectors,
and any collaborator like ``_devices``, ``_depth``, etc.).
"""

from __future__ import annotations

import time
import logging

logger = logging.getLogger("vanchor.app")


class TelemetryBuilder:
    """Telemetry frame assembly -- split out of Runtime."""

    def __init__(self, rt) -> None:
        self._rt = rt   # back-reference to Runtime for shared state

    # ------------------------------------------------------------------ #
    # Battery (#60)
    # ------------------------------------------------------------------ #
    def battery_snapshot(self) -> dict:
        """Battery telemetry from the active battery monitor (#42) — the sim pack
        or a real shunt driver.

        When there is NO battery source — ``battery_source: none`` (the operator
        turned the gauge off), or a driver that could not be built — the battery
        is **disabled**: report ``soc_pct=None`` so the UI shows no level and the
        alarm / thrust-ladder / RTL paths (all guarded on a ``None`` SoC) stay
        quiet. We deliberately do NOT fall back to the simulator's pack here: that
        resurrected a battery the operator had disabled, showing a fake level and
        firing low/critical alarms for a gauge that isn't there. ``range_m`` stays
        a number (0.0) because ``evaluate_rtl_recommend`` compares ``range_m <=
        0.0`` before it checks the (None) SoC."""
        rt = self._rt
        if getattr(rt, "battery_monitor", None) is not None:
            return rt.battery_monitor.snapshot()
        return {
            "soc_pct": None,
            "voltage_v": None,
            "current_a": None,
            "draw_w": None,
            "range_m": 0.0,
            "time_to_empty_s": None,
            "present": False,
        }

    # ------------------------------------------------------------------ #
    # Supervisor link snapshot
    # ------------------------------------------------------------------ #
    def _supervisor_snapshot(self) -> dict:
        """Supervisor link state for the UI (update/backup/disk cards + banners).

        Always returns a dict (never None) so the UI can safely read keys.
        The first container's tag/previous_tag are promoted to the top level
        so the UI can render the installed version without iterating."""
        from .. import __version__
        rt = self._rt
        if rt.supervisor_link is None:
            return {"available": False, "app_version": __version__}
        st = rt._supervisor_status
        if not st:
            return {"available": False, "app_version": __version__}
        containers = st.get("containers", [])
        first = containers[0] if containers else {}
        return {
            "available": True,
            "app_version": __version__,
            "supervisor_version": st.get("supervisor_version"),
            "api_version": st.get("api_version"),
            "tag": first.get("tag"),
            "previous_tag": first.get("previous_tag"),
            "containers": containers,
            "disk": st.get("disk"),
            "job": st.get("job"),
            "last_job": st.get("last_job"),
            "warnings": st.get("warnings", []),
            "backups": st.get("backups", {}),
        }

    # ------------------------------------------------------------------ #
    # Health snapshot
    # ------------------------------------------------------------------ #
    def _health_snapshot(self) -> dict:
        """Cheap per-sensor freshness + controller-loop health for telemetry.

        Ages are seconds since each input last arrived (``None`` when it has
        never been received); ``controller_tick_age_s`` is the control loop's
        heartbeat age (``None`` before the loop has run). Also surfaces the
        wave-1 ``controller_fault`` and the governor's active staleness flags.
        No I/O -- pure reads off the shared state and the last governor status."""
        rt = self._rt
        now = rt._mono_fn()
        st = rt.state

        def _age(stamp: float | None) -> float | None:
            return round(now - stamp, 2) if stamp is not None else None

        tick = st.controller_last_tick_monotonic
        status = rt.controller.safety_status
        depth_age = _age(st.depth_received_mono)
        depth_stale_s = rt.controller.safety.config.depth_stale_s
        health = {
            "fix_age_s": _age(st.fix_received_mono),
            "heading_age_s": _age(st.heading_received_mono),
            "depth_age_s": depth_age,
            "imu_age_s": _age(st.imu_received_mono),
            "controller_fault": st.controller_fault,
            "controller_tick_age_s": (round(now - tick, 2) if tick else None),
            # Active staleness / freshness flags (last governor tick).
            "heading_stale": status.heading_stale,
            "fix_lost": status.fix_lost,
            "depth_stale": depth_age is not None and depth_age > depth_stale_s,
        }
        # Per-device connection health, surfaced from serial devices that expose
        # ``healthy`` / ``last_data_monotonic`` (the reconnect work). Sim devices
        # lack the attributes, so the block is omitted entirely on a sim-only
        # runtime -- keeping the base health shape unchanged when no real device
        # reports health.
        devices = rt._device_health(now)
        if devices:
            health["devices"] = devices
        return health

    # ------------------------------------------------------------------ #
    # Full telemetry frame
    # ------------------------------------------------------------------ #
    def telemetry(self) -> dict:
        """Build a PURE telemetry snapshot -- no side effects.

        This is called by BOTH the WS broadcaster and ``GET /api/state``, so it
        must not mutate anything: the periodic safety evaluations (launch
        capture, RTL recommend, link failsafe), trip accumulation and depth
        persistence all live in the supervisor task (see ``_run_supervisor`` /
        ``_supervise_once``); depth-sounding accumulation is driven by the
        broadcaster via ``record_depth_sounding`` so polling ``/api/state`` can't
        double-record soundings or perturb failsafe timing (findings M2/H4/#7).
        """
        from ..core.capabilities import mode_availability
        from ..core.models import ControlModeName

        rt = self._rt
        # During replay, play recorded frames back instead of live state. Live
        # safety evaluation keeps running regardless -- it lives in the
        # supervisor now, not here -- so swapping the displayed frame can't
        # disable it.
        if rt.replay.active:
            frame = rt.replay.current(time.time())
            if frame is not None:
                return frame

        payload = rt.state.to_dict()
        payload["safety"] = rt.controller.safety_status.to_dict()
        # Land guard settings ride the safety block (the status part comes from
        # the governor; enabled/margin/chart come from config + runtime).
        payload["safety"]["land_guard"].update({
            "enabled": rt.controller.safety.config.land_guard_enabled,
            "margin_m": rt.controller.safety.config.land_guard_margin_m,
            "have_chart": rt.controller.safety.has_water_geometry,
        })
        # Server-side safety geometry (#23) so the browser becomes a CACHE, not
        # the source of truth: a freshly-opened client renders the SERVER's
        # zones/min-depth/failsafe. Raw no-go rings come from the persistence
        # store; min-depth + failsafe are read live off the governor (the
        # authority). safety.js adopts these as truth and only pushes local ->
        # server on an explicit edit or a one-time migration (no echo loop).
        _gov = rt.controller.safety.config
        payload["safety_geometry"] = {
            "nogo_zones": rt.safety_geometry.nogo_zones,
            "min_depth_m": _gov.min_depth_m,
            "fix_failsafe_enabled": _gov.fix_failsafe_enabled,
        }
        # Passive anchor alarm (adoption #10): armed circle + live breach state.
        payload["anchor_alarm"] = rt.anchor_alarm.snapshot()
        payload["health"] = self._health_snapshot()
        # Device availability + per-mode gating (Not-connected devices disable the
        # modes/functions that need them; the UI shows the reason).
        payload["devices"] = rt.device_status()
        payload["mode_availability"] = mode_availability(
            {k: v["connected"] for k, v in payload["devices"].items()}
        )
        payload["battery"] = self.battery_snapshot()
        payload["link"] = {
            "client_connected": rt._ui_clients > 0,
            "since_s": (
                round(rt._mono_fn() - rt._last_client_seen, 1)
                if rt._last_client_seen is not None
                else None
            ),
            "failsafe_engaged": rt._link_failsafe_engaged,
            # What the failsafe DID: "continue" | "hold" | "stop" | None. Lets
            # the UI report "continuing mission" instead of a blanket
            # "holding position" when continue-mission is on.
            "failsafe_action": rt._link_failsafe_action,
        }
        ctrl = rt.controller
        # Manual course-hold line (bearing + anchor), for the chart overlay.
        mc = ctrl.manual
        payload["manual_course"] = (
            {"bearing": mc.course_bearing,
             "lat": mc.course_origin.lat, "lon": mc.course_origin.lon}
            if mc.course_bearing is not None and mc.course_origin is not None
            else None
        )
        payload["auto_apb"] = {
            "enabled": rt.config.safety.auto_follow_apb,
            # engaged = the CURRENT Follow-APB session was started by auto-APB
            "engaged": (rt._auto_apb_latched
                        and rt.state.mode == ControlModeName.FOLLOW_APB),
        }
        payload["cruise"] = {
            "enabled": ctrl.cruise_knots is not None,
            "target_knots": ctrl.cruise_knots or 0.0,
        }
        payload["track"] = {
            "recording": ctrl.track.recording,
            "count": len(ctrl.track.points),
            # Most recent breadcrumbs for the map (cap the payload size).
            "points": [[p.lat, p.lon] for p in ctrl.track.points[-300:]],
        }
        payload["trip"] = rt.trip.snapshot(rt._now_fn())
        # Expose (read-only) the accumulated depth map. Accumulation + periodic
        # persistence are NOT done here (telemetry() is a pure snapshot): the
        # broadcaster drives sounding accumulation via record_depth_sounding()
        # and the supervisor checkpoints to disk off the event loop.
        # depth_count is a cheap scalar; depth_points (~28 KB) is the bulk of the
        # frame. telemetry() returns the COMPLETE snapshot (so /api/state is
        # deterministic + full); the high-rate WS broadcaster decimates
        # depth_points to ~1 Hz (see ui/server.py:broadcaster). The frontend uses
        # depth_count for the readout and retains the last points when the WS
        # omits the array.
        payload["depth_count"] = len(rt.depth_map.points)
        payload["depth_points"] = rt.depth_map.as_list()

        # Closed-loop steering unit: target (pre-slew) vs feedback (actual head
        # angle). On hardware the feedback is the AS5600; in sim it's the
        # slew-limited applied steering.
        boat = rt.config.boat
        max_ang = rt.state.max_steer_angle_deg
        actual = rt.state.motor_command.steering
        target_deg = rt.controller.safety.desired_steering * max_ang
        angle_deg = actual * max_ang
        rng = max(1.0, boat.steer_range_deg)
        payload["steering"] = {
            "commanded": round(actual, 3),
            "target_deg": round(target_deg, 1),
            "angle_deg": round(angle_deg, 1),
            "rate_dps": boat.max_steer_rate_dps,
            "range_deg": boat.steer_range_deg,
            "wrap_pct": round(max(-100.0, min(100.0, angle_deg / rng * 100.0)), 0),
            "feedback_ok": True,
        }
        # On real hardware the steering Arduino reports its *measured* azimuth
        # back over serial (#83). SerialMotorController parses those ``A`` lines
        # into ``last_feedback``; when present we surface the real feedback
        # instead of the commanded estimate. The sim motor has no such
        # attribute, so the simulator path keeps the modelled values above.
        feedback = getattr(rt.controller.motor, "last_feedback", None)
        if feedback is not None:
            payload["steering"]["angle_deg"] = round(feedback.angle_deg, 1)
            payload["steering"]["wrap_pct"] = round(feedback.wrap_pct, 0)
            payload["steering"]["feedback_ok"] = feedback.ok
        payload["boat"] = rt.boat_profile()
        payload["gps_offset"] = {
            "dlat": rt.navigator.gps_dlat,
            "dlon": rt.navigator.gps_dlon,
            "active": rt.navigator.gps_offset_active,
        }
        payload["throttle_override"] = {
            "active": rt.controller.throttle_override is not None,
            "percent": (
                rt.controller.throttle_override * 100.0
                if rt.controller.throttle_override is not None
                else 0.0
            ),
        }
        # Guided pattern modes (#57/#58/#59) -- expose each mode's live state.
        contour_mode = ctrl.modes[ControlModeName.CONTOUR_FOLLOW]
        payload["contour"] = {
            "target_depth_m": round(rt.state.contour_target_depth_m, 1),
            "depth_m": round(rt.state.depth_m, 1),
            "error_m": round(contour_mode.error_m, 2),
        }
        orbit_mode = ctrl.modes[ControlModeName.ORBIT]
        payload["orbit"] = {
            "center_lat": (
                rt.state.orbit_center.lat if rt.state.orbit_center else None
            ),
            "center_lon": (
                rt.state.orbit_center.lon if rt.state.orbit_center else None
            ),
            "radius_m": round(rt.state.orbit_radius_m, 1),
            "direction": rt.state.orbit_direction,
            "range_m": round(orbit_mode.range_m, 2),
        }
        trolling_mode = ctrl.modes[ControlModeName.TROLLING]
        payload["trolling"] = {
            "base_heading": round(rt.state.trolling_base_heading, 2),
            "amplitude_deg": round(rt.state.trolling_amplitude_deg, 1),
            "period_s": round(rt.state.trolling_period_s, 1),
            "phase": round(trolling_mode.phase, 3),
        }
        # Learned anchor mode (#34): the live residual-decay guardrail + polarity
        # bookkeeping, so a degraded hybrid falling back to its PID floor is
        # visible (hold_quality itself is in state.to_dict()).
        ml_mode = ctrl.modes.get(ControlModeName.ANCHOR_ML)
        if ml_mode is not None:
            payload["anchor_ml"] = {
                "residual_scale": round(ml_mode.residual_scale, 3),
                "residual_scale_effective": round(
                    ml_mode.residual_scale_effective, 3
                ),
                "guard_hold_ratio": round(ml_mode.guard_hold_ratio, 3),
                "steer_sign": ml_mode.steer_sign,
                "policy_steer_sign": ml_mode.policy_steer_sign,
            }
        payload["nav"] = {
            "paused": rt.controller.suspended is not None,
            "suspended_mode": (
                rt.controller.suspended["mode"].value
                if rt.controller.suspended is not None
                else None
            ),
        }
        payload["calibration"] = rt.calibration.snapshot()
        payload["sim_enabled"] = rt.simulator is not None
        payload["demo_mode"] = rt.config.demo.enabled
        payload["demo_readonly"] = rt.config.demo.enabled and rt.config.demo.readonly
        if rt.simulator is not None:
            truth = rt.simulator.truth()
            env = rt.simulator.environment
            payload["truth"] = {
                "lat": truth.point.lat,
                "lon": truth.point.lon,
                "heading_deg": round(truth.heading_deg, 2),
                "speed_mps": round(truth.speed_mps, 3),
            }
            payload["environment"] = {
                "current_speed": env.current_speed,
                "current_dir": env.current_dir,
                "wind_speed": round(env.wind_speed, 2),
                "wind_dir": round(env.wind_dir, 1),
                "gust_amplitude_mps": env.gust_amplitude_mps,
                # Slow-wander amount so the UI can show how variable it is.
                "wind_variability": env.wind_variability,
                "current_variability": env.current_variability,
                # Instantaneous gusty wind the boat actually feels right now.
                "wind_gust_now": round(env.wind_speed + rt.simulator.current_gust_mps, 2),
            }
        payload["debug"] = rt.debug.status()
        payload["replay"] = {"active": rt.replay.active, "name": rt.replay.name} if rt.replay.active else {"active": False}
        payload["supervisor"] = self._supervisor_snapshot()
        # NB: recording this frame into the debug session is done by the
        # broadcaster (off the event loop), not here -- telemetry() is pure so
        # that GET /api/state polling can't inject phantom frames into a session.
        return payload
