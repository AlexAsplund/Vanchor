/* Vanchor-NG — Replace / Append delivery for "Take me here" destinations.
 *
 * Every take-me-here entry point (tap-map go-to, the map long-press menu, a
 * marker's route buttons, the smart-routing panel) funnels its new waypoints
 * through VA.routeChoice.deliver(). When a route is already ACTIVE (boat in
 * waypoint mode) or PENDING (unstarted pins in the editor), the user chooses
 * Replace or Append instead of the old silent replace:
 *
 *   - append to ACTIVE:  re-send the running route + the new waypoints with
 *     the current resume index, so the boat keeps navigating (same live-edit
 *     path a waypoint drag uses — loop/patrol/on-arrival flags survive).
 *   - append to PENDING: extend the unstarted route in the editor; the user
 *     still reviews and presses Start route.
 *   - replace:           the entry point's original behaviour runs (a pending
 *     route is cleared first so its pins don't linger under the new route).
 *
 * deliver() resolves to "replaced" | "appended-active" | "appended-pending" |
 * null (cancelled), so callers can decide whether to auto-start.
 */
"use strict";

(function () {
  if (!window.VA || !VA.map) return;

  // Current control mode from the latest telemetry frame: an "active route"
  // only counts while the boat is actually navigating it (mode waypoint), not
  // when a finished route's marks are still drawn on the map.
  const mode = () => (VA.last && typeof VA.last.mode === "string") ? VA.last.mode : "";

  const strip = (w) => ({
    name: w.name, lat: w.lat, lon: w.lon,
    throttle_pct: w.throttle_pct ?? null, speed_kn: w.speed_kn ?? null,
  });

  // Modal choice dialog -> Promise<"replace"|"append"|null>.
  function ask(title, sub, appendLabel) {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "route-choice-overlay";
      overlay.innerHTML =
        `<div class="route-choice glass">` +
        `<div class="rc-title">${title}</div>` +
        `<div class="rc-sub">${sub}</div>` +
        `<button type="button" data-act="append">${appendLabel}</button>` +
        `<button type="button" data-act="replace" class="danger">Replace route</button>` +
        `<button type="button" data-act="cancel" class="cancel">Cancel</button>` +
        `</div>`;
      const done = (v) => { overlay.remove(); resolve(v); };
      overlay.addEventListener("pointerdown", (e) => { if (e.target === overlay) done(null); });
      overlay.querySelectorAll("button").forEach((b) =>
        b.addEventListener("click", () => done(b.dataset.act === "cancel" ? null : b.dataset.act)));
      document.body.appendChild(overlay);
    });
  }

  async function deliver(newWps, replaceFn) {
    const wps = (newWps || []).map(strip);
    if (!wps.length) return null;
    const n = wps.length, plural = n === 1 ? "waypoint" : "waypoints";

    // 1) A route the boat is actively navigating.
    const cr = (mode() === "waypoint" && VA.map.committedRoute) ? VA.map.committedRoute() : null;
    if (cr && cr.waypoints.length) {
      const c = await ask("Route is running",
        `Add the ${n} new ${plural} to the end of the active route, or replace it?`,
        "Append to active route");
      if (!c) return null;
      if (c === "append") {
        VA.send({
          type: "goto",
          waypoints: cr.waypoints.map(strip).concat(wps),
          throttle: 0.6,
          active: cr.active,   // live-edit resume: keep navigating, don't restart
        });
        VA.logLine(`appended ${n} ${plural} to the active route`);
        return "appended-active";
      }
      replaceFn();
      return "replaced";
    }

    // 2) An unstarted (pending) route in the editor.
    const pending = VA.map.pending();
    if (pending.length) {
      const c = await ask("Pending route in editor",
        `Add the ${n} new ${plural} to the end of the pending route, or replace it?`,
        "Append to pending route");
      if (!c) return null;
      if (c === "append") {
        VA.map.setPending(pending.concat(wps));
        if (VA.routeEditor && VA.routeEditor.refresh) VA.routeEditor.refresh();
        VA.logLine(`appended ${n} ${plural} to the pending route`);
        return "appended-pending";
      }
      VA.map.setPending([]);   // Replace: drop the old pins before the new route lands
      if (VA.routeEditor && VA.routeEditor.refresh) VA.routeEditor.refresh();
      replaceFn();
      return "replaced";
    }

    // 3) Nothing to conflict with.
    replaceFn();
    return "replaced";
  }

  VA.routeChoice = { deliver };
})();
