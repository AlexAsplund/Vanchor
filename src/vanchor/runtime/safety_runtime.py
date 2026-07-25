"""Failsafe + power + supervisor cluster extracted from Runtime (issue #76).

⚠️ SAFETY-CRITICAL: this cluster contains the **link-loss deadman** and
**auto-RTL** -- part of the non-negotiable safety floor. The 13 methods moved
here are a byte-for-byte behaviour-preserving move: battery-source discovery
and monitor construction, the sim battery SoC setter, the ``_underway``
predicate, the lost-link failsafe, edge-triggered Web Push alerts, the battery
RTL recommend/auto-RTL scheduler, the low-battery thrust-derating ladder, and
the periodic supervisor loop + host-supervisor status poller.

``SafetyRuntime`` holds a back-reference to ``Runtime`` via ``self._rt`` for the
shared state that remains on Runtime. ALL of the failsafe/link/battery/rtl/
supervisor private attributes stay on Runtime and are read/written through
``self._rt`` -- they are heavily cross-referenced by TelemetryBuilder
(``_ui_clients``, ``_link_failsafe_engaged``/``_link_failsafe_action``,
``_supervisor_status``), the control loop (``_last_client_seen``), ``ui/server.py``
(``_supervisor_status``) and the test-suite (which sets/asserts ``_push_prev``,
``_push_prev_cap``, ``_rtl_in_flight``, ``_supervisor_status`` directly).

Clock injection is preserved: tests inject ``rt._mono_fn``/``rt._now_fn`` and
these methods read them through ``self._rt``. Tests also monkeypatch
``rt._schedule_auto_rtl`` and expect it honoured by ``evaluate_rtl_recommend`` /
``_battery_rtl_handoff``, and monkeypatch ``rt.evaluate_rtl_recommend`` /
``rt.evaluate_push_alerts`` and expect ``rt._supervise_once`` to honour them --
so those cross-calls are routed back through ``self._rt`` (the Runtime shims),
NOT ``self`` on this collaborator.
"""

from __future__ import annotations

import asyncio
import logging

from ..core.config import AppConfig
from ..core.models import ControlModeName
from ..hardware import registry

logger = logging.getLogger("vanchor.app")


class SafetyRuntime:
    """Failsafe + power + supervisor cluster -- split out of Runtime."""

    def __init__(self, rt) -> None:
        self._rt = rt   # back-reference to Runtime for shared state

    # ------------------------------------------------------------------ #
    # Battery source discovery + monitor construction (#42/#43)
    # ------------------------------------------------------------------ #
    def _battery_sources(self) -> tuple:
        """Built-in battery sources + any registered driver sources (e.g.
        ``ina226``), discovered from the registry so a pack driver needs no edit
        here."""
        rt = self._rt
        return rt._BATTERY_SOURCES + tuple(registry.sources("battery"))

    def _build_battery_monitor(self, cfg: AppConfig, simulator):
        """Build the battery monitor (#42) — the reference registry-driven 4th
        device kind. ``sim`` presents the simulator's integrated pack (the
        baseline, identical telemetry to before); any other source is a pluggable
        driver built through the versioned capability API (#43). A driver that
        can't be built (missing lib, no hardware) is skipped with a warning — the
        rest of the boat still runs — mirroring the compass-driver resilience.

        Default: ``sim`` when a simulated boat exists (unchanged behaviour), else
        ``none`` (a real battery monitor is not implied by enabling serial GPS/
        compass/motor)."""
        rt = self._rt
        source = cfg.hardware.battery_source or ("sim" if simulator is not None else "none")
        if source in ("none", None):
            return None
        if source == "sim":
            if simulator is None:
                return None
            from ..hardware.drivers.battery import SimBatteryMonitor
            return SimBatteryMonitor(simulator.battery)
        if registry.uses_context("battery", source):
            try:
                ctx = rt._driver_context("battery", source, cfg.battery)
                return registry.build_with_context("battery", source, ctx)
            except Exception as exc:  # noqa: BLE001 - a bad driver must not crash startup
                logger.warning(
                    "battery source %r could not be built (%s); running without a "
                    "battery monitor. Change it in Settings -> Devices.", source, exc)
                return None
        if registry.has("battery", source):  # legacy (runtime, cfg) driver
            try:
                return registry.build_device("battery", source, rt, cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("battery source %r could not be built (%s).", source, exc)
                return None
        logger.warning("unknown battery source %r; running without a battery monitor.", source)
        return None

    # ------------------------------------------------------------------ #
    # Sim battery SoC setter (#60)
    # ------------------------------------------------------------------ #
    def _set_battery(self, soc_pct: object) -> None:
        """Set/reset the battery state-of-charge (#60). Sim-only: on real
        hardware the SOC comes from a battery monitor over the HAL."""
        rt = self._rt
        if soc_pct is None or rt.simulator is None:
            logger.info("set_battery ignored (no value or no sim battery)")
            return
        rt.simulator.battery.set_soc(float(soc_pct))
        logger.info("battery SOC set to %.0f%%", float(soc_pct))

    # ------------------------------------------------------------------ #
    # Lost-connection failsafe (#64) + RTL auto-recommend (#61)
    # ------------------------------------------------------------------ #
    def _underway(self) -> bool:
        """True when the boat is actively making way and a lost link must be
        caught -- i.e. NOT idle. Every guided/cruising mode counts, plus MANUAL
        while the operator is actually commanding thrust (driving by hand): a
        client loss there must not leave the boat motoring on forever (#64).
        Station-keeping anchor-hold is excluded (it is already holding)."""
        # Imported lazily from app to avoid an import cycle (app imports this
        # module); these are module-level constants -- the values are identical.
        from ..app import _MANUAL_UNDERWAY_THRUST_EPS, _UNDERWAY_MODES
        rt = self._rt
        if rt.state.mode in _UNDERWAY_MODES:
            return True
        if rt.state.mode == ControlModeName.MANUAL:
            return abs(rt.state.motor_command.thrust) > _MANUAL_UNDERWAY_THRUST_EPS
        return False

    def evaluate_link_failsafe(self, now: float | None = None) -> bool:
        """Engage the lost-link failsafe if no UI client has been seen for the
        timeout while underway. In a guided mode this holds position
        (anchor-hold); driving MANUALLY it STOPS (zero thrust) -- there is no
        target to hold to, so the safe action is to cut the motor. Returns True
        if it engaged on this call. Idempotent and clock-injectable (pass the
        MONOTONIC ``now`` in tests)."""
        rt = self._rt
        if now is None:
            now = rt._mono_fn()
        timeout = rt.config.safety.link_loss_timeout_s
        connected = rt._ui_clients > 0
        if connected or rt._last_client_seen is None or rt._link_failsafe_engaged:
            return False
        if not self._underway():
            return False
        if now - rt._last_client_seen < timeout:
            return False
        if rt.state.mode == ControlModeName.MANUAL:
            # Driving by hand with the link gone -> cut the motor (STOP). This
            # deadman is part of the safety floor and is NOT configurable.
            logger.warning("link lost %.0fs while driving manually; STOP (zero thrust)", timeout)
            rt.controller.handle_command({"type": "stop"})
            rt._link_failsafe_action = "stop"
        elif rt.config.safety.link_loss_continue_mission:
            # Unsupervised missions (the default: a locked phone must not park
            # an active route): keep flying the guided mode; geofence/depth/
            # battery failsafes still apply. Logged + latched (fires once).
            logger.warning("link lost %.0fs while underway; continuing mission "
                           "(safety.link_loss_continue_mission)", timeout)
            rt._link_failsafe_action = "continue"
        else:
            # Guided mode with continue-mission off -> hold position here.
            logger.warning("link lost %.0fs while underway; engaging hold-position", timeout)
            rt.controller.handle_command({"type": "anchor_hold"})
            rt._link_failsafe_action = "hold"
        rt._link_failsafe_engaged = True
        return True

    def evaluate_push_alerts(self) -> None:
        """Edge-triggered Web Push dispatch (adoption #7). Mirrors the client-side
        banner conditions in static/alerts.js, but server-side so an alarm
        reaches the phone with NO client connected. Observe-only: reads state,
        never commands. Each send is enqueued to the push worker thread.

        Every edge is also recorded into the server-side alert log (Task 1
        D8/A14) so the alert-history panel survives a page reload."""
        rt = self._rt
        prev = rt._push_prev

        # Boat position for alert-log entries ("Show on map"); None when no fix.
        _pos = rt.state.position
        _lat = _pos.lat if (_pos is not None and not _pos.is_null()) else None
        _lon = _pos.lon if (_pos is not None and not _pos.is_null()) else None

        # ---- anchor drag ----
        drag = bool(rt.controller.safety_status.drag_alarm)
        if drag and not prev.get("drag"):
            dist = rt.state.distance_to_anchor_m
            body = (f"Boat {dist:.0f} m from anchor" if rt.state.anchor is not None
                    else "Boat has dragged from anchor")
            rt.push.notify("anchor_drag", "Anchor drag alarm", body)
            rt.alert_log.record("alarm", f"Anchor drag alarm — {body}",
                                kind="drag", lat=_lat, lon=_lon)
        prev["drag"] = drag

        # ---- passive anchor alarm breach (via on_breach hook) ----
        # The on_breach hook (appended in __init__) fires the push immediately
        # on False->True; also track firing here for consistent edge state.
        aalarm = bool(rt.anchor_alarm.firing)
        prev["anchor_alarm"] = aalarm  # track but don't double-fire; hook does it

        # ---- battery RTL recommend ----
        battery_rtl = bool(rt.state.rtl_recommended)
        if battery_rtl and not prev.get("battery_rtl"):
            rt.push.notify("battery", "Battery low", "Return to launch recommended")
            rt.alert_log.record("warn", "Battery low — return to launch recommended",
                                kind="battery", lat=_lat, lon=_lon)
        prev["battery_rtl"] = battery_rtl

        # ---- battery SoC ladder (UI convention: warn <25%, crit <10%) ----
        # Independent of the RTL estimate (Task 1 A5); edge-triggered so each
        # threshold crossing records/pushes exactly once, not once per tick.
        soc_val = rt.battery_snapshot().get("soc_pct")
        if soc_val is not None:
            soc_f = float(soc_val)
            batt_crit = soc_f < 10.0
            batt_low = soc_f < 25.0
            if batt_crit and not prev.get("batt_crit"):
                rt.push.notify("battery", "Battery critical",
                               f"Battery at {soc_f:.0f}%")
                rt.alert_log.record("alarm", f"Battery critical — {soc_f:.0f}%",
                                    kind="battery", lat=_lat, lon=_lon)
            elif batt_low and not batt_crit and not prev.get("batt_low"):
                rt.push.notify("battery", "Battery low",
                               f"Battery at {soc_f:.0f}%")
                rt.alert_log.record("warn", f"Battery low — {soc_f:.0f}%",
                                    kind="battery", lat=_lat, lon=_lon)
            prev["batt_crit"] = batt_crit
            prev["batt_low"] = batt_low

        # ---- link loss failsafe ----
        link = bool(rt._link_failsafe_engaged)
        if link and not prev.get("link"):
            action = rt._link_failsafe_action
            if action == "stop":
                body = "Motor stopped (link-loss failsafe)"
            elif action == "continue":
                body = "Continuing mission unsupervised"
            else:
                body = "Holding position (failsafe)"
            rt.push.notify("link", "Connection lost", body)
            rt.alert_log.record("alarm", f"Connection lost — {body}",
                                kind="link", lat=_lat, lon=_lon)
        prev["link"] = link

        # ---- shallow stop ----
        shallow = bool(rt.controller.safety_status.shallow_stop)
        if shallow and not prev.get("shallow"):
            min_d = rt.controller.safety.config.min_depth_m
            rt.push.notify("depth", "Shallow water",
                           f"Auto-stopped: depth below {min_d:.1f} m")
            rt.alert_log.record("alarm",
                                f"Shallow — auto-stopped (depth below {min_d:.1f} m)",
                                kind="shallow", lat=_lat, lon=_lon)
        prev["shallow"] = shallow

        # ---- depth divergence ----
        diverge = bool(rt.state.depth_divergence_alert)
        if diverge and not prev.get("diverge"):
            rt.push.notify("depth", "Depth warning",
                           "Sounder disagrees with chart — possible uncharted shoal")
            rt.alert_log.record("warn",
                                "Depth warning — sounder disagrees with chart",
                                kind="depth", lat=_lat, lon=_lon)
        prev["diverge"] = diverge

        # ---- GPS fix lost ----
        fix_lost = bool(rt.controller.safety_status.fix_lost)
        if fix_lost and not prev.get("fix_lost"):
            rt.push.notify("link", "GPS fix lost", "Thrust cut until fix returns")
            rt.alert_log.record("alarm", "GPS fix lost — thrust cut until fix returns",
                                kind="gps", lat=_lat, lon=_lon)
        prev["fix_lost"] = fix_lost

        # ---- battery ladder step (cap decrease) ----
        cap = rt.controller.safety.thrust_cap
        if cap < rt._push_prev_cap - 1e-9:
            soc = rt.battery_snapshot().get("soc_pct")
            rt.push.notify(
                "battery", "Battery low",
                f"Thrust limited to {cap:.0%}"
                + (f" at {soc:.0f}% charge" if soc is not None else ""),
            )
        rt._push_prev_cap = cap

    def evaluate_rtl_recommend(self) -> bool:
        """Set ``state.rtl_recommended`` when the battery range has dropped to
        within ``rtl_margin_m`` of the distance home (so the boat can *just* make
        it back). If ``auto_rtl`` is set, engage RTL. Returns the new flag."""
        rt = self._rt
        launch = rt.state.launch
        pos = rt.state.position
        if launch is None or pos is None or pos.is_null():
            rt.state.rtl_recommended = False
            return False
        range_m = rt.battery_snapshot().get("range_m", 0.0)
        if range_m <= 0.0:
            # No usable range estimate (boat not making way). A zero estimate
            # with a critically low pack must still recommend -- "unknown" is
            # not "infinite". (Task 1 A5: rtl_recommended stayed False at
            # range_m: 0 even with the pack nearly flat.)
            soc = rt.battery_snapshot().get("soc_pct")
            if soc is not None and float(soc) <= 10.0:
                rt.state.rtl_recommended = True
                return True
            rt.state.rtl_recommended = False
            return False
        from ..core.geo import haversine_m

        dist_home = haversine_m(pos, launch)
        recommend = range_m <= dist_home + rt.config.safety.rtl_margin_m
        rt.state.rtl_recommended = recommend
        if recommend and rt.config.safety.auto_rtl and rt.state.mode != ControlModeName.WAYPOINT:
            logger.warning("auto_rtl: battery range near distance-home; engaging RTL")
            rt._schedule_auto_rtl()
        return recommend

    def _schedule_auto_rtl(self) -> None:
        """Engage auto-RTL WITHOUT blocking the event loop.

        ``return_to_launch`` -> ``plan_route`` is synchronous and CPU/IO-heavy
        (Overpass fetch, up to two 60 s timeouts) and documented as executor-only.
        Calling it inline from the periodic telemetry tick would stall every
        async loop, so run it in the default executor. A single in-flight guard
        stops the evaluator (called every telemetry tick) from launching a pile
        of duplicate concurrent RTL plans."""
        rt = self._rt
        if rt._rtl_in_flight:
            return
        rt._rtl_in_flight = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (e.g. a unit test off the live path) -> preserve
            # the old inline behaviour rather than silently doing nothing.
            try:
                rt.return_to_launch()
            finally:
                rt._rtl_in_flight = False
            return
        asyncio.ensure_future(self._run_auto_rtl(loop))

    async def _run_auto_rtl(self, loop) -> None:
        """Run the heavy RTL plan+engage in an executor; always clear the
        in-flight flag so a failure can't wedge future auto-RTL attempts."""
        rt = self._rt
        try:
            result = await loop.run_in_executor(None, rt.return_to_launch)
            if isinstance(result, dict) and not result.get("ok", True):
                logger.warning("auto_rtl planning failed: %s", result.get("message"))
        except Exception:
            logger.exception("auto_rtl planning failed")
        finally:
            rt._rtl_in_flight = False

    # ------------------------------------------------------------------ #
    # Low-battery thrust-derating ladder (#49)
    # ------------------------------------------------------------------ #
    def evaluate_battery_ladder(self) -> float:
        """Push a soft thrust cap into the governor from the battery SoC, and hand
        off to the existing RTL at the lowest stage. Returns the applied cap
        (1.0 = no derate).

        The cap is a magnitude-only ceiling, so STOP and every failsafe still take
        precedence and are never blocked. Only runs when there is a real battery
        reading (a simulated pack or a battery monitor); with no battery source
        the cap is left at 1.0 so a boat with no gauge is never spuriously derated
        by the zeros fallback in :meth:`battery_snapshot`."""
        rt = self._rt
        ladder = rt._battery_ladder
        gov = rt.controller.safety
        if not ladder.enabled or (
            getattr(rt, "battery_monitor", None) is None and rt.simulator is None
        ):
            gov.set_thrust_cap(1.0)
            return 1.0
        soc = rt.battery_snapshot().get("soc_pct")
        if soc is None:
            gov.set_thrust_cap(1.0)
            return 1.0
        soc = float(soc)
        cap = ladder.cap_for(soc)
        gov.set_thrust_cap(cap)
        if ladder.at_rtl(soc):
            self._battery_rtl_handoff(soc)
        else:
            # Recovered above the RTL stage (e.g. a battery swap) -> re-arm.
            rt._battery_rtl_engaged = False
        return cap

    def _battery_rtl_handoff(self, soc_pct: float) -> None:
        """At the lowest ladder stage, hand off to the EXISTING RTL/failsafe once.

        Idempotent via a one-shot flag (cleared when SoC recovers above the
        stage), and guarded so it only fires when a launch point exists to return
        to. The progressive derate above this stage still holds regardless."""
        rt = self._rt
        if rt._battery_rtl_engaged:
            return
        rt._battery_rtl_engaged = True
        # Recommend-only unless the operator opted into autonomous RTL (#7): with
        # auto_rtl off the boat must NOT self-drive -- mirror evaluate_rtl_recommend
        # and only raise the low-battery RTL recommendation for the UI/alarm. The
        # progressive derate cap already applied above still stands.
        if not rt.config.safety.auto_rtl:
            rt.state.rtl_recommended = True
            logger.warning(
                "battery critically low (%.0f%%); recommending Return-to-Launch "
                "(auto_rtl off -- not self-driving)", soc_pct)
            return
        if rt.state.launch is None:
            logger.warning(
                "battery critically low (%.0f%%) but no launch point recorded; "
                "holding the lowest derate cap (no RTL target)", soc_pct)
            return
        logger.warning(
            "battery critically low (%.0f%%); handing off to Return-to-Launch",
            soc_pct)
        rt._schedule_auto_rtl()

    # ------------------------------------------------------------------ #
    # Periodic safety supervisor (1 Hz) + host-supervisor status poller
    # ------------------------------------------------------------------ #
    async def _run_supervisor_client(self) -> None:
        """Poll the host-side supervisor's /v1/status and cache it in _supervisor_status.

        Exception-proof: logged errors back off and retry; task only exits on
        CancelledError at shutdown. Never mutates any safety-critical state."""
        rt = self._rt
        if rt.supervisor_link is None:
            return
        cfg = rt.config.supervisor
        while True:
            try:
                status = await asyncio.to_thread(rt.supervisor_link.status)
                rt._supervisor_status = status
                rt._supervisor_status_at = rt._mono_fn()
                if status is None:
                    await asyncio.sleep(cfg.unavailable_backoff_s)
                elif status.get("job") is not None:
                    # A job is running — poll fast so the UI stays responsive.
                    await asyncio.sleep(1.0)
                else:
                    await asyncio.sleep(cfg.poll_interval_s)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("supervisor client poll error -- will retry")
                await asyncio.sleep(cfg.poll_interval_s)

    async def _run_supervisor(self, period_s: float = 1.0) -> None:
        """~1 Hz task driving the periodic safety evaluations + depth persistence.

        Exception-proof: the whole body is guarded so a raise (from a step or a
        save) only logs and continues -- the task NEVER exits on its own; it ends
        only on cancellation at shutdown."""
        rt = self._rt
        while True:
            try:
                await asyncio.sleep(period_s)
                rt._supervise_once()
                await rt._depth._maybe_persist_depth()
            except asyncio.CancelledError:
                raise  # shutdown -> let the cancellation propagate
            except Exception:  # noqa: BLE001 - supervisor must never die
                logger.exception("supervisor loop error -- will continue")
