"""Routing + anchor-alarm + fusion-calibration cluster extracted from Runtime (issue #75).

The 11 methods that handle fusion calibration reads/saves, anchor-alarm recovery,
auto-APB evaluation, anchor-alarm evaluation, route loading, route planning,
route cancellation, island loop planning, and survey planning live here.
``NavGlue`` holds a back-reference to ``Runtime`` via ``self._rt`` for shared
state that remains on Runtime (state, config, bus, navigator, controller,
compass, anchor_alarm, safety_geometry, depth_map, and the private attrs
``_fusion_cal``, ``_route_plan_cancelled``, ``_auto_apb_latched``).

Private state ownership:
  ``_fusion_cal`` — stays on Runtime (also used by set_interference_compensation).
  ``_route_plan_cancelled`` — stays on Runtime (accessed directly by tests).
  ``_auto_apb_latched`` — stays on Runtime (read by TelemetryBuilder via rt._auto_apb_latched).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("vanchor.app")


class NavGlue:
    """Routing + anchor-alarm + fusion-calibration -- split out of Runtime."""

    def __init__(self, rt) -> None:
        self._rt = rt   # back-reference to Runtime for shared state

    # ------------------------------------------------------------------ #
    # Online fetches (Overpass) through the client relay when offline
    # ------------------------------------------------------------------ #
    def _relay_post(self):
        """The relay-backed ``http_post`` for ``water.fetch_overpass`` (tries the
        server's own internet first, then a connected client), or ``None`` when
        no relay is attached (tests / headless) so the direct default applies."""
        from .fetch_relay import relay_http_post
        return relay_http_post(getattr(self._rt, "fetch_relay", None))

    @staticmethod
    def _fetch_fail_message(exc: Exception) -> str:
        """Operator-facing message for a failed water fetch. A FetchRelayError
        already says exactly what went wrong (no internet + no client, client
        couldn't fetch, timeout); anything else gets the generic offline hint."""
        if type(exc).__name__ == "FetchRelayError":
            return str(exc)
        return "No offline chart for this area; connect once to download it."

    # ------------------------------------------------------------------ #
    # Fusion calibration (still-capture system-ID; see nav.calibration)
    # ------------------------------------------------------------------ #

    def fusion_calibration(self) -> dict:
        """Saved calibration + live capture status (for GET)."""
        from ..nav.calibration import interference_recommendations
        rt = self._rt
        capturing, samples, seconds = rt.navigator.capture_status()
        cal = rt._fusion_cal
        score = cal.motor_interference_score if cal else None
        return {
            "calibration": cal.to_dict() if cal else None,
            "capturing": capturing,
            "capture_samples": samples,
            "capture_seconds": seconds,
            "enabled": rt.navigator.fusion is not None,
            "recommendations": interference_recommendations(score),
            # experimental motor-interference remedy state
            "interference_comp_enabled": bool(cal.interference_comp_enabled) if cal else False,
            "has_interference_model": bool(cal and cal.motor_interference_slope is not None),
        }

    def save_fusion_calibration(self, data: dict) -> dict:
        from ..nav.calibration import FusionCalibration, save_calibration
        rt = self._rt
        # Merge into the existing calibration so each capture mode updates only
        # what it measured (still -> gains, align -> offset, ...).
        incoming = FusionCalibration.from_dict(data)
        merged = (rt._fusion_cal or FusionCalibration()).merged_with(incoming)
        save_calibration(rt.config.data_dir, merged)
        rt._fusion_cal = merged
        rt.navigator.apply_calibration(merged)
        return {"ok": True}

    def reset_fusion_calibration(self) -> dict:
        from ..nav.calibration import FusionCalibration, clear_calibration
        rt = self._rt
        clear_calibration(rt.config.data_dir)
        rt._fusion_cal = None
        rt.navigator.apply_calibration(FusionCalibration())
        return {"ok": True}

    # ------------------------------------------------------------------ #
    # Passive anchor alarm recover (adoption #10)
    # ------------------------------------------------------------------ #

    def _anchor_alarm_recover(self) -> dict:
        """One-tap recover (adoption #10): engage the NORMAL anchor_hold at
        the alarm anchor point via the standard command path — device gating,
        governor, every failsafe apply unchanged. Disarms the passive alarm
        only if the hold actually engaged (the governor's drag alarm then
        covers the hold); if the controller refused (e.g. motor not
        connected), the passive watch stays armed."""
        from ..core.models import ControlModeName
        rt = self._rt
        st = rt.anchor_alarm.store
        if not st.armed or st.lat is None or st.lon is None:
            logger.warning("anchor_alarm_recover ignored: alarm not armed")
            return {"ok": False, "message": "alarm not armed"}
        rt.controller.handle_command({
            "type": "anchor_hold",
            "anchor": {"lat": st.lat, "lon": st.lon},
        })
        engaged = rt.state.mode in (
            ControlModeName.ANCHOR_HOLD,
            ControlModeName.ANCHOR_ML,
            ControlModeName.ANCHOR_LEIF,
        )
        if engaged:
            rt.anchor_alarm.clear()
            logger.info("anchor alarm recover: anchor_hold engaged at alarm point")
            return {"ok": True}
        logger.warning("anchor alarm recover: anchor_hold refused; alarm stays armed")
        return {"ok": False, "message": "anchor_hold refused (device gating?)"}

    # ------------------------------------------------------------------ #
    # Auto Follow-APB (opt-in, safety.auto_follow_apb)
    # ------------------------------------------------------------------ #

    def evaluate_auto_apb(self, now: float | None = None) -> bool:
        """Auto-engage Follow-APB when an external autopilot's APB feed is live
        (opt-in, ``safety.auto_follow_apb``). Engages ONLY from idle MANUAL --
        never hijacks an anchor hold / route / a hand on the throttle. Latched:
        leaving the mode by hand isn't overridden until the feed has been
        silent for >10 s and returns. Returns True when it engaged."""
        from ..core.models import ControlModeName
        rt = self._rt
        if not rt.config.safety.auto_follow_apb:
            rt._auto_apb_latched = False
            return False
        if now is None:
            now = rt._mono_fn()
        st = rt.state
        fresh = (st.apb_received_mono is not None
                 and now - st.apb_received_mono < 10.0)
        if not fresh:
            rt._auto_apb_latched = False       # feed gone -> re-arm
            return False
        if st.mode == ControlModeName.FOLLOW_APB:
            rt._auto_apb_latched = True        # (covers manual engagement too)
            return False
        if rt._auto_apb_latched:
            return False                          # user left the mode on purpose
        if st.mode != ControlModeName.MANUAL or abs(st.motor_command.thrust) > 0.05:
            return False
        logger.warning("APB feed detected -- auto-engaging Follow-APB "
                       "(safety.auto_follow_apb)")
        rt.controller.handle_command({"type": "follow_apb"})
        rt._auto_apb_latched = True
        return True

    # ------------------------------------------------------------------ #
    # Passive anchor alarm evaluation (adoption #10)
    # ------------------------------------------------------------------ #

    def evaluate_anchor_alarm(self) -> dict:
        """1 Hz passive anchor-alarm step (adoption #10). Reads position +
        fix age off the shared state; NEVER issues motor commands or mode
        changes — breach only latches telemetry + fires the breach hooks."""
        rt = self._rt
        st = rt.state
        age = (
            (rt._mono_fn() - st.fix_received_mono)
            if st.fix_received_mono is not None else None
        )
        return rt.anchor_alarm.evaluate(st.position, age)

    # ------------------------------------------------------------------ #
    # Route loading
    # ------------------------------------------------------------------ #

    def _load_route(self, command: dict) -> None:
        from ..nav.routes import parse_gpx
        rt = self._rt
        try:
            if "gpx" in command:
                text = str(command["gpx"])
            elif "path" in command:
                with open(command["path"], "r", encoding="utf-8") as fh:
                    text = fh.read()
            else:
                logger.warning("load_route requires 'gpx' or 'path'")
                return
            waypoints = parse_gpx(text)
        except (ValueError, OSError) as exc:
            logger.warning("load_route failed: %s", exc)
            return
        rt.state.waypoints = waypoints
        rt.controller.handle_command(
            {"type": "load_route", "throttle": command.get("throttle")}
            if command.get("throttle") is not None
            else {"type": "load_route"}
        )
        logger.info("loaded route with %d waypoints", len(waypoints))

    # ------------------------------------------------------------------ #
    # Smart "Take me here" water routing (task #43)
    # ------------------------------------------------------------------ #

    def plan_route(
        self, dest_lat: float, dest_lon: float, mode: str = "fastest",
        offset_m: float = 25.0, depth_aware: bool = True,
    ) -> dict:
        """Plan a water-only route from the boat's current position.

        Synchronous and CPU/IO-heavy (Overpass fetch + shapely/networkx); the UI
        endpoint calls it in an executor. Returns the API contract dict. Does NOT
        start navigation.

        When ``depth_aware`` is set (default) and a ``min_depth_m`` safety
        threshold is configured, imported depth data (contours + soundings) is
        turned into a shallow no-go mask so the route proactively goes AROUND
        shoals instead of relying on the reactive shallow-stop. Falls back
        transparently to plain routing when there is no depth data.
        """
        from ..nav import routing, water
        rt = self._rt

        # Fresh plan: clear any stale cancel request so a normal plan runs.
        rt._route_plan_cancelled = False
        pos = rt.state.position
        if pos is not None and not pos.is_null():
            start_lat, start_lon = pos.lat, pos.lon
        else:
            start_lat, start_lon = rt.config.sim.start_lat, rt.config.sim.start_lon

        cache = water.WaterCache(rt.config.data_dir)
        bbox = water.bbox_around(start_lat, start_lon, dest_lat, dest_lon)
        water_ll = cache.find_covering(bbox)
        if water_ll is None:
            try:
                elements = water.fetch_overpass(*bbox, http_post=self._relay_post())
            except Exception as exc:  # network / endpoint failure
                logger.warning("water fetch failed: %s", exc)
                return {
                    "ok": False,
                    "waypoints": [],
                    "message": self._fetch_fail_message(exc),
                }
            water_ll = water.assemble_water(elements)
            if water_ll.is_empty:
                return {
                    "ok": False,
                    "waypoints": [],
                    "message": "No mapped water found around the route.",
                }
            try:
                cache.store(bbox, water_ll)
            except OSError as exc:  # pragma: no cover - disk failure
                logger.warning("water cache store failed: %s", exc)

        # Depth-aware routing: build a shallow no-go mask from imported depth
        # (contours + soundings) so routes avoid shoals by default. Cheap and
        # bounded (bbox-windowed, capped); yields None when no depth data exists,
        # in which case routing is byte-identical to before.
        avoid_shallow_ll = None
        min_depth_m = rt.config.safety.min_depth_m
        if depth_aware and min_depth_m and min_depth_m > 0.0:
            try:
                # bbox is (south, west, north, east); depth windowing wants
                # (west, south, east, north).
                s, w, n, e = bbox
                avoid_shallow_ll = rt.depth_map.shallow_polygons((w, s, e, n), min_depth_m)
            except Exception as exc:  # pragma: no cover - defensive; never block a plan
                logger.warning("shallow mask build failed: %s", exc)
                avoid_shallow_ll = None

        result = routing.plan_route(
            start_lat=start_lat,
            start_lon=start_lon,
            dest_lat=dest_lat,
            dest_lon=dest_lon,
            water_ll=water_ll,
            mode=mode,
            shoreline_offset_m=offset_m,
            cancelled=lambda: rt._route_plan_cancelled,
            avoid_shallow_ll=avoid_shallow_ll,
        )
        return {
            "ok": result.ok,
            "waypoints": result.waypoints,
            "message": result.message,
        }

    def cancel_route_plan(self) -> None:
        """Request that an in-progress route plan abort ASAP (#54)."""
        self._rt._route_plan_cancelled = True

    # ------------------------------------------------------------------ #
    # "Around island" loop route (#77)
    # ------------------------------------------------------------------ #

    def plan_island_loop(
        self, click_lat: float, click_lon: float, offset_m: float = 20.0
    ) -> dict:
        """Plan a closed loop route encircling the island under ``(lat, lon)``.

        Uses the same offline water chart/cache as :meth:`plan_route` (fetches
        once if not cached). The boat's current position (or the sim start)
        decides which water body is the basin. Does NOT start navigation -- it
        returns waypoints for the route editor. Synchronous + CPU/IO-heavy; the
        UI endpoint calls it in an executor. Returns
        ``{ok, waypoints, loop, message}``.
        """
        from ..nav import routing, water
        rt = self._rt

        pos = rt.state.position
        if pos is not None and not pos.is_null():
            boat_lat, boat_lon = pos.lat, pos.lon
        else:
            boat_lat, boat_lon = rt.config.sim.start_lat, rt.config.sim.start_lon

        cache = water.WaterCache(rt.config.data_dir)
        bbox = water.bbox_around(boat_lat, boat_lon, click_lat, click_lon)
        water_ll = cache.find_covering(bbox)
        if water_ll is None:
            try:
                elements = water.fetch_overpass(*bbox, http_post=self._relay_post())
            except Exception as exc:  # network / endpoint failure
                logger.warning("water fetch failed: %s", exc)
                return {
                    "ok": False,
                    "waypoints": [],
                    "loop": True,
                    "message": self._fetch_fail_message(exc),
                }
            water_ll = water.assemble_water(elements)
            if water_ll.is_empty:
                return {
                    "ok": False,
                    "waypoints": [],
                    "loop": True,
                    "message": "No mapped water found around the island.",
                }
            try:
                cache.store(bbox, water_ll)
            except OSError as exc:  # pragma: no cover - disk failure
                logger.warning("water cache store failed: %s", exc)

        result = routing.plan_island_loop(
            click_lat,
            click_lon,
            water_ll,
            boat_lat=boat_lat,
            boat_lon=boat_lon,
            offset_m=offset_m,
        )
        return {
            "ok": result.ok,
            "waypoints": result.waypoints,
            "loop": result.loop,
            "message": result.message,
        }

    # ------------------------------------------------------------------ #
    # Area survey "map mode" route (#47)
    # ------------------------------------------------------------------ #

    def plan_survey(
        self, polygon_latlon: list, spacing_m: float, angle_deg: float | None = None
    ) -> dict:
        """Plan a boustrophedon coverage route over a closed area polygon.

        Pure CPU work (shapely); the UI endpoint calls it in an executor. Does
        NOT start navigation -- it returns waypoints for the route editor.
        """
        from ..nav import survey, water as water_mod
        rt = self._rt

        # Fetch the cached water polygon covering the drawn area so the survey
        # is clipped to water and connecting legs stay off land. No cached
        # water for the area -> plan against the polygon alone (survey.py still
        # repairs legs that exit the drawn polygon itself).
        water_geom = None
        try:
            lats = [float(p[0]) for p in polygon_latlon]
            lons = [float(p[1]) for p in polygon_latlon]
            if lats and lons:
                cache = water_mod.WaterCache(rt.config.data_dir)
                bbox = water_mod.bbox_around(min(lats), min(lons), max(lats), max(lons))
                geom = cache.find_covering(bbox)
                if geom is not None and not geom.is_empty:
                    water_geom = geom
        except Exception as exc:  # noqa: BLE001 - clipping is best-effort
            logger.warning("survey water lookup failed (planning unclipped): %s", exc)

        try:
            result = survey.plan_survey(
                polygon_latlon, float(spacing_m), angle_deg, water=water_geom
            )
        except (ValueError, TypeError) as exc:
            logger.warning("survey plan failed: %s", exc)
            return {"ok": False, "waypoints": [], "message": f"Bad survey request: {exc}"}
        return {
            "ok": result.ok,
            "waypoints": result.waypoints,
            "message": result.message,
        }
