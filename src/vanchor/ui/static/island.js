/* Vanchor-NG — "Loop around island" routing (task #77, UI half).
 *
 * Flow: the user arms an island-pick, taps a landmass on the map, and we POST
 * /api/route/island {lat, lon, offset_m}. The backend returns a ring of
 * waypoints offset from the shore. They are loaded into the existing route
 * editor *unstarted* via VA.map.setPending(...), and the pending route is
 * flagged as a LOOP (VA.routeEditor.setLoop(true)) so that pressing the
 * existing "Start route" button sends `loop:true` and the boat circles
 * continuously.
 *
 * Backend contract:
 *   POST /api/route/island {lat, lon, offset_m?}
 *     -> { ok, waypoints:[{name,lat,lon}], loop:true, message }
 *   The route-start command then carries `loop:true` (handled in app.js).
 *
 * Self-gates: a 404 at first use disables the controls and degrades cleanly,
 * exactly like the smart-routing / survey cards.
 */
"use strict";

(function () {
  if (!window.VA || !VA.map) return;
  const $ = (id) => document.getElementById(id);

  const card = $("island-card");
  const pickBtn = $("island-pick");
  const offsetInput = $("island-offset");
  const offsetOut = $("island-offset-val");
  const statusEl = $("island-status");
  const badge = $("island-loop-badge");
  if (!card || !pickBtn) return;

  let armed = false;
  let available = null; // null=unknown, true/false after first probe
  let drag = null;      // active press while armed; see the down/move/up handlers

  // Drag convention: dragging the pointer NORTH (up the screen) requests a
  // CLOCKWISE loop; dragging SOUTH requests COUNTER-CLOCKWISE. A plain tap
  // (negligible vertical travel) keeps the default direction (clockwise).
  const DEFAULT_CW = true;
  const DRAG_PX = 14; // vertical pixels before a press counts as a direction drag

  // ---- geo helpers -------------------------------------------------------
  function boatPos() {
    const t = VA.last;
    if (!t) return null;
    const p = t.position || t.truth || t;
    const lat = p && Number(p.lat), lon = p && Number(p.lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
    return { lat, lon };
  }

  // Squared planar distance, longitude scaled by cos(lat) so it stays metric
  // enough for nearest-neighbour comparison at a single locale. Good enough; we
  // only ever compare distances, never report them.
  function dist2(aLat, aLon, bLat, bLon) {
    const k = Math.cos((aLat * Math.PI) / 180) || 1;
    const dLat = aLat - bLat, dLon = (aLon - bLon) * k;
    return dLat * dLat + dLon * dLon;
  }

  // Rotate the ring so it STARTS at the waypoint nearest the boat, preserving
  // ring order otherwise. Returns a new array (or the input if no boat fix).
  function rotateToNearest(wps, pos) {
    if (!pos || wps.length < 2) return wps;
    let best = 0, bestD = Infinity;
    for (let i = 0; i < wps.length; i++) {
      const d = dist2(pos.lat, pos.lon, wps[i].lat, wps[i].lon);
      if (d < bestD) { bestD = d; best = i; }
    }
    if (best === 0) return wps;
    return wps.slice(best).concat(wps.slice(0, best));
  }

  // Signed shoelace area in (lon=x, lat=y) space. >0 is counter-clockwise in a
  // standard x-east/y-north frame; <0 is clockwise.
  function signedArea(wps) {
    let a = 0;
    for (let i = 0; i < wps.length; i++) {
      const p = wps[i], q = wps[(i + 1) % wps.length];
      a += p.lon * q.lat - q.lon * p.lat;
    }
    return a / 2;
  }

  // Force the ring to the requested winding, then (since reversing moves the
  // nearest point off index 0) re-rotate so it still starts nearest the boat.
  function enforceDirection(wps, wantCW, pos) {
    if (wps.length < 3) return wps;
    const isCW = signedArea(wps) < 0;
    if (isCW !== wantCW) {
      wps = wps.slice().reverse();
      wps = rotateToNearest(wps, pos);
    }
    return wps;
  }

  // ---- live direction hint (built dynamically, themed inline) -------------
  let hintEl = null;
  function ensureHint() {
    if (hintEl) return hintEl;
    const el = document.createElement("div");
    el.style.cssText = [
      "position:fixed", "z-index:9999", "pointer-events:none",
      "padding:6px 10px", "border-radius:10px",
      "font:600 12px/1.2 system-ui,sans-serif", "white-space:nowrap",
      "color:var(--text,#eaf6fb)",
      "background:var(--glass,rgba(10,16,28,0.86))",
      "border:1px solid rgba(var(--accent-rgb,47,243,255),0.5)",
      "box-shadow:0 0 0 1px rgba(var(--accent-rgb,47,243,255),0.35),0 6px 20px rgba(0,0,0,0.45)",
      "transform:translate(14px,-50%)", "transition:opacity .1s", "opacity:0",
    ].join(";");
    document.body.appendChild(el);
    hintEl = el;
    return el;
  }
  function showHint(x, y, cw) {
    const el = ensureHint();
    el.textContent = cw
      ? "↻ Clockwise — drag N/S to flip"
      : "↺ Counter-clockwise — drag N/S to flip";
    el.style.left = x + "px";
    el.style.top = y + "px";
    el.style.opacity = "1";
  }
  function hideHint() {
    if (hintEl) hintEl.style.opacity = "0";
  }

  function setStatus(msg, kind) {
    if (!statusEl) return;
    statusEl.textContent = msg || "";
    statusEl.className = "hint" + (kind ? " " + kind : "");
  }

  function setArmed(on) {
    armed = !!on && available !== false;
    pickBtn.classList.toggle("active", armed);
    pickBtn.textContent = armed ? "Tap or drag N/S on the island… (cancel)" : "Tap the island";
    const mapEl = $("map");
    if (mapEl) mapEl.classList.toggle("goto-arming", armed);
    if (!armed) {
      // Disarming mid-press: drop any pending drag + restore Leaflet panning.
      if (drag && VA.map.leaflet) VA.map.leaflet.dragging.enable();
      drag = null;
      hideHint();
    }
  }

  // Keep the slider's readout in sync.
  if (offsetInput && offsetOut) {
    const sync = () => { offsetOut.textContent = offsetInput.value; };
    offsetInput.addEventListener("input", sync);
    sync();
  }

  function currentOffset() {
    const v = offsetInput ? parseFloat(offsetInput.value) : 20;
    return Number.isFinite(v) ? v : 20;
  }

  // Click-and-drag island pick. While armed we grab the map's mousedown and
  // track the press: the VERTICAL travel (screen Δy → Δlat) picks the loop
  // direction, while the *down* point is the island tap location. A negligible
  // drag is a plain tap and keeps the default direction.
  //
  // We still register a click-consumer as a fallback (taps that Leaflet routes
  // straight to its `click` event, e.g. without a measurable drag), consuming
  // only while armed so we never fight the route/go-to handlers.
  const leaflet = VA.map.leaflet;

  function directionFromDy(dy) {
    // Screen y grows downward, so dy<0 means dragged NORTH → clockwise.
    if (Math.abs(dy) < DRAG_PX) return DEFAULT_CW;
    return dy < 0; // up/north => clockwise(true); down/south => CCW(false)
  }

  function onDown(e) {
    if (!armed || !leaflet) return;
    const ev = e.originalEvent;
    const ll = e.latlng;
    drag = { startLL: ll, startY: ev.clientY, startX: ev.clientX, moved: false, cw: DEFAULT_CW };
    showHint(ev.clientX, ev.clientY, DEFAULT_CW);
    // Suppress Leaflet's pan while we measure the drag.
    leaflet.dragging.disable();
  }

  function onMove(e) {
    if (!drag) return;
    const ev = e.originalEvent;
    const dy = ev.clientY - drag.startY;
    if (Math.abs(dy) >= DRAG_PX || Math.abs(ev.clientX - drag.startX) >= DRAG_PX) drag.moved = true;
    drag.cw = directionFromDy(dy);
    showHint(ev.clientX, ev.clientY, drag.cw);
  }

  function onUp() {
    if (!drag) return;
    const d = drag; drag = null;
    if (leaflet) leaflet.dragging.enable();
    hideHint();
    setArmed(false);
    planIsland(d.startLL.lat, d.startLL.lng, d.cw);
  }

  if (leaflet) {
    leaflet.on("mousedown", onDown);
    leaflet.on("mousemove", onMove);
    leaflet.on("mouseup", onUp);
  }

  // Fallback: a bare click while armed with no drag captured above.
  if (VA.map.addClickConsumer) {
    VA.map.addClickConsumer((lat, lon) => {
      if (!armed) return false;
      setArmed(false);
      planIsland(lat, lon, DEFAULT_CW);
      return true;
    });
  }

  pickBtn.addEventListener("click", () => setArmed(!armed));

  async function planIsland(lat, lon, wantCW) {
    if (typeof wantCW !== "boolean") wantCW = DEFAULT_CW;
    if (available === false) { setStatus("Island loops unavailable on this runtime.", "err"); return; }
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) { setStatus("No tap location.", "err"); return; }
    const body = { lat, lon, offset_m: currentOffset() };
    pickBtn.disabled = true;
    setStatus("Planning loop around the island…", "busy");
    VA.logLine("» route/island " + JSON.stringify(body));
    let r;
    try {
      const resp = await fetch("/api/route/island", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (resp.status === 404) { markUnavailable(); return; }
      r = await resp.json();
    } catch (err) {
      setStatus("Request failed: " + err, "err");
      pickBtn.disabled = false;
      return;
    } finally {
      if (available !== false) pickBtn.disabled = false;
    }
    if (!r || r.ok === false || !Array.isArray(r.waypoints) || !r.waypoints.length) {
      setStatus((r && r.message) || "Tap an island surrounded by water.", "err");
      return;
    }
    let wps = r.waypoints
      .filter((w) => w && Number.isFinite(w.lat) && Number.isFinite(w.lon))
      .map((w, i) => ({ name: w.name || ("IS" + (i + 1)), lat: w.lat, lon: w.lon }));
    if (!wps.length) { setStatus("Tap an island surrounded by water.", "err"); return; }

    // ---- client-side post-processing of the returned closed ring ----------
    // 1) Rotate so the loop STARTS at the waypoint nearest the boat (degrade
    //    gracefully — skip rotation entirely if we have no boat fix).
    // 2) Enforce the chosen winding (reverse if needed, then re-rotate so the
    //    nearest point is still index 0). Re-number after, since the boat reads
    //    them in order.
    const pos = boatPos();
    wps = rotateToNearest(wps, pos);
    wps = enforceDirection(wps, wantCW, pos);
    wps = wps.map((w, i) => ({ name: "IS" + (i + 1), lat: w.lat, lon: w.lon }));
    const dirLabel = wantCW ? "clockwise" : "counter-clockwise";

    VA.map.setPending(wps);
    // Flag the pending route as a continuous loop so Start route sends loop:true.
    // setLoop() also lights the "↻ loop" indicator + this card's badge (app.js).
    if (VA.routeEditor && VA.routeEditor.setLoop) VA.routeEditor.setLoop(true);
    else if (badge) badge.classList.remove("hidden");
    if (VA.routeEditor && VA.routeEditor.refresh) VA.routeEditor.refresh();
    // Surface the route panel so the user can review + press Start.
    document.querySelectorAll(".ctx-panel").forEach((p) => p.classList.toggle("active", p.dataset.for === "waypoint"));
    setStatus(
      (r.message ? r.message + " — " : "") + wps.length + " loop waypoints (" + dirLabel +
      (pos ? ", starting nearest the boat" : "") + "). Review and press Start route.",
      "ok"
    );
  }

  function markUnavailable() {
    available = false;
    card.classList.add("unavailable");
    pickBtn.disabled = true;
    if (offsetInput) offsetInput.disabled = true;
    setArmed(false);
    setStatus("Island loop endpoint not available on this runtime.", "err");
  }

  // No proactive probe (don't spam the parallel backend on load); the first tap
  // detects a 404 and self-gates, mirroring the smart-routing card.
})();
