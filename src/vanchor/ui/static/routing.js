/* Vanchor-NG — "Take me here" smart routing UI (task #43, UI half).
 *
 * Flow: the user arms destination-pick, taps the map, chooses Fastest or Along
 * shoreline (with a metres offset), and we POST /api/route/plan. The returned
 * waypoints are loaded into the existing route editor *unstarted* via
 * VA.map.setPending(...), so the user reviews/edits and presses the existing
 * Start route button.
 *
 * Backend contract:
 *   POST /api/route/plan {dest_lat, dest_lon, mode:"fastest"|"shoreline",
 *                         shoreline_offset_m}
 *     -> { ok, waypoints:[{name,lat,lon}], message }
 *
 * The whole feature self-gates: if the endpoint 404s (or errors at probe time)
 * the controls are disabled and the panel shows that smart routing is
 * unavailable, so the rest of the UI degrades cleanly.
 */
"use strict";

(function () {
  if (!window.VA || !VA.map) return;
  const $ = (id) => document.getElementById(id);

  const card = $("route-plan-card");
  const armBtn = $("route-pick");
  const modeSel = $("route-mode");
  const offsetRow = $("route-offset-row");
  const offsetInput = $("route-offset");
  const offsetOut = $("route-offset-val");
  const planBtn = $("route-plan-go");
  const statusEl = $("route-plan-status");
  if (!card || !armBtn || !planBtn) return;

  let dest = null;      // {lat, lon}
  let armed = false;
  let available = null; // null=unknown, true/false after probe

  function setStatus(msg, kind) {
    if (!statusEl) return;
    statusEl.textContent = msg || "";
    statusEl.className = "hint" + (kind ? " " + kind : "");
  }

  function setArmed(on) {
    armed = !!on && available !== false;
    armBtn.classList.toggle("active", armed);
    armBtn.textContent = armed ? "Tap destination… (cancel)" : (dest ? "Re-pick destination" : "Pick destination");
    const mapEl = $("map");
    if (mapEl) mapEl.classList.toggle("goto-arming", armed);
  }

  function updateOffsetVisibility() {
    if (offsetRow) offsetRow.classList.toggle("hidden", !modeSel || modeSel.value !== "shoreline");
  }
  if (modeSel) modeSel.addEventListener("change", updateOffsetVisibility);
  updateOffsetVisibility();

  if (offsetInput && offsetOut) {
    const sync = () => { offsetOut.textContent = offsetInput.value; };
    offsetInput.addEventListener("input", sync);
    sync();
  }

  // Destination pick: layer onto the map-click pipeline (consumes the click
  // only while armed, so it never fights the route/go-to handlers).
  if (VA.map.addClickConsumer) {
    VA.map.addClickConsumer((lat, lon) => {
      if (!armed) return false;
      dest = { lat, lon };
      VA.map.setGotoMarker(lat, lon);
      setArmed(false);
      setStatus("Destination set. Choose a mode and plan.", "");
      planBtn.disabled = false;
      return true;
    });
  }

  armBtn.addEventListener("click", () => setArmed(!armed));

  // Plan a smart route to an explicit destination/mode and load it into the
  // editor *unstarted*. Reused by the route panel AND by a marker's "Take me
  // here" buttons. Returns true on success.
  async function planTo(destLat, destLon, mode, offsetM) {
    if (available === false) { setStatus("Smart routing unavailable on this runtime.", "err"); return false; }
    if (!Number.isFinite(destLat) || !Number.isFinite(destLon)) { setStatus("No destination.", "err"); return false; }
    const body = {
      dest_lat: destLat, dest_lon: destLon,
      mode: mode || "fastest",
      shoreline_offset_m: offsetM != null ? offsetM : (offsetInput ? parseFloat(offsetInput.value) : 30),
    };
    if (planBtn) planBtn.disabled = true;
    setStatus("Planning route…", "busy");
    VA.logLine("» route/plan " + JSON.stringify(body));
    let r;
    try {
      const resp = await fetch("/api/route/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (resp.status === 404) { markUnavailable(); return false; }
      r = await resp.json();
    } catch (err) {
      setStatus("Request failed: " + err, "err");
      if (planBtn) planBtn.disabled = false;
      return false;
    }
    if (!r || r.ok === false || !Array.isArray(r.waypoints) || !r.waypoints.length) {
      setStatus((r && r.message) || "No route found.", "err");
      if (planBtn) planBtn.disabled = false;
      return false;
    }
    const wps = r.waypoints
      .filter((w) => w && Number.isFinite(w.lat) && Number.isFinite(w.lon))
      .map((w, i) => ({ name: w.name || ("WP" + (i + 1)), lat: w.lat, lon: w.lon }));
    VA.map.setPending(wps);
    // A smart route is a normal point-to-point route, not a loop: drop any
    // lingering island-loop flag so it doesn't circle.
    if (VA.routeEditor && VA.routeEditor.clearLoop) VA.routeEditor.clearLoop();
    // Refresh the route editor list (app.js owns it; expose a hook).
    if (VA.routeEditor && VA.routeEditor.refresh) VA.routeEditor.refresh();
    // Surface the route panel so the user can review + press Start.
    document.querySelectorAll(".ctx-panel").forEach((p) => p.classList.toggle("active", p.dataset.for === "waypoint"));
    setStatus((r.message ? r.message + " — " : "") + wps.length + " waypoints loaded. Review and press Start route.", "ok");
    if (planBtn) planBtn.disabled = false;
    return true;
  }
  async function plan() {
    if (!dest) { setStatus("Pick a destination on the map first.", "err"); return; }
    return planTo(dest.lat, dest.lon, modeSel ? modeSel.value : "fastest",
                  offsetInput ? parseFloat(offsetInput.value) : 30);
  }
  planBtn.addEventListener("click", plan);

  // Expose for marker "Take me here" buttons (markers.js).
  window.VA = window.VA || {};
  VA.routing = Object.assign(VA.routing || {}, { planTo });

  function markUnavailable() {
    available = false;
    card.classList.add("unavailable");
    armBtn.disabled = true;
    if (modeSel) modeSel.disabled = true;
    if (offsetInput) offsetInput.disabled = true;
    planBtn.disabled = true;
    setArmed(false);
    setStatus("Smart routing endpoint not available on this runtime.", "err");
  }
  // No proactive probe: we don't want to spam the backend (built in parallel)
  // with a fake request on load. Instead plan() detects a 404 on first use and
  // disables the controls, which is the graceful-degradation the contract asks
  // for. `available` stays null (unknown) until then.
})();
