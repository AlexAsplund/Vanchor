/* Vanchor-NG — guided "fishing" modes (#57 contour-follow, #58 orbit, #59 troll).
 *
 * These three guided modes live behind a "More" overflow flyout on the mode rail
 * (keeping the 8-button rail tidy). Each has its own contextual panel that
 * follows the existing .ctx-panel pattern. This module owns:
 *
 *  - the More flyout (open/close, item → panel activation),
 *  - the per-mode controls and the commands they send:
 *      contour_follow {target_depth_m, side:"deep"|"shallow", speed_knots}
 *      orbit          {center_lat, center_lon, radius_m, direction:"cw"|"ccw", speed_knots}
 *      trolling       {base_heading:null, amplitude_deg, period_s, speed_knots}
 *  - telemetry reflection:
 *      contour:{target_depth_m, depth_m, error_m}  → depth vs target + error bar
 *      orbit:{range_m, radius_m}                    → range to centre + circle
 *  - the live orbit circle drawn on the map (preview + while active).
 *
 * Degrades gracefully if a command/telemetry field is absent: the controls still
 * work and simply show "—" where telemetry is missing. STOP is handled by the
 * existing rail / nav bar (send {type:"stop"}) which returns to a safe mode.
 */
"use strict";

(function () {
  const send = VA.send;
  const $ = (id) => document.getElementById(id);

  // ---- small helpers -----------------------------------------------------
  // Mirror app.js's panel toggling for click-driven (intent) activation, so a
  // new guided mode's panel shows immediately before the backend enters it.
  function showPanel(mode) {
    document.querySelectorAll(".ctx-panel").forEach((p) =>
      p.classList.toggle("active", p.dataset.for === mode));
    if (VA.map && VA.map.clearGotoMarker) VA.map.clearGotoMarker();
  }

  // Segmented control: wire data-val buttons, return a getter for the chosen val.
  function segValue(id, dflt) {
    const seg = $(id);
    let val = dflt;
    if (seg) {
      seg.querySelectorAll("button[data-val]").forEach((b) => {
        if (b.classList.contains("on")) val = b.dataset.val;
        b.addEventListener("click", () => {
          val = b.dataset.val;
          seg.querySelectorAll("button[data-val]").forEach((x) => x.classList.toggle("on", x === b));
        });
      });
    }
    return () => val;
  }

  // bindSlider clone (app.js's is module-private): update output + optional fn.
  function bindSlider(id, outId, fn) {
    const el = $(id), out = $(outId);
    if (!el) return;
    const update = () => { if (out) out.textContent = el.value; if (fn) fn(parseFloat(el.value)); };
    el.addEventListener("input", update);
    update();
  }
  const fnum = (id) => parseFloat(($(id) || { value: "0" }).value);

  // ===== More flyout ======================================================
  const moreToggle = $("more-toggle");
  const moreMenu = $("more-menu");
  function setMore(open) {
    if (moreMenu) moreMenu.classList.toggle("hidden", !open);
    if (moreToggle) {
      moreToggle.classList.toggle("active", open);
      moreToggle.setAttribute("aria-expanded", open ? "true" : "false");
    }
  }
  if (moreToggle) moreToggle.addEventListener("click", (e) => {
    e.stopPropagation();
    setMore(moreMenu ? moreMenu.classList.contains("hidden") : false);
  });
  // Tap elsewhere closes the flyout.
  document.addEventListener("pointerdown", (e) => {
    if (!moreMenu || moreMenu.classList.contains("hidden")) return;
    if (moreMenu.contains(e.target) || (moreToggle && moreToggle.contains(e.target))) return;
    setMore(false);
  });

  // Flyout items: activate the mode's panel (intent) and send its start command.
  if (moreMenu) moreMenu.querySelectorAll(".more-item[data-mode]").forEach((b) =>
    b.addEventListener("click", () => {
      const m = b.dataset.mode;
      showPanel(m);
      setMore(false);
      if (m === "contour_follow") startContour();
      else if (m === "orbit") { /* needs a centre first — panel only */ }
      else if (m === "trolling") startTroll();
    }));

  // ===== #57 contour-follow ===============================================
  bindSlider("contour-depth", "contour-depth-val");
  bindSlider("contour-speed", "contour-speed-val");
  const contourSide = segValue("contour-side", "deep");
  let contourDragging = false;
  ["contour-depth", "contour-speed"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("pointerdown", () => { contourDragging = true; });
    ["pointerup", "pointercancel", "blur"].forEach((ev) => el.addEventListener(ev, () => { contourDragging = false; }));
  });
  function contourCmd() {
    return {
      type: "contour_follow",
      target_depth_m: fnum("contour-depth"),
      side: contourSide(),
      speed_knots: fnum("contour-speed"),
    };
  }
  function startContour() { send(contourCmd()); }
  const contourGo = $("contour-go");
  if (contourGo) contourGo.addEventListener("click", startContour);
  // Live re-send when adjusting while active.
  ["contour-depth", "contour-speed"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("change", () => { if (VA.last && VA.last.mode === "contour_follow") startContour(); });
  });

  function updateContour(t) {
    const c = t.contour || {};
    const now = VA.fin(c.depth_m);
    const tgt = VA.fin(c.target_depth_m);
    const err = VA.fin(c.error_m);
    VA.setText("contour-now", now === null ? "—" : now.toFixed(1));
    VA.setText("contour-tgt", tgt === null ? "—" : tgt.toFixed(1));
    VA.setText("contour-err", err === null ? "—" : (err > 0 ? "+" : "") + err.toFixed(1) + " m");
    // Reflect the active target onto the slider when the user isn't dragging.
    if (tgt !== null && !contourDragging && t.mode === "contour_follow") {
      const el = $("contour-depth");
      if (el && Math.abs(parseFloat(el.value) - tgt) > 0.05) {
        el.value = tgt; VA.setText("contour-depth-val", el.value);
      }
    }
    // Centred error bar: 0 = centre, clamp to ±5 m full-scale.
    const fill = $("contour-errfill");
    if (fill) {
      if (err === null) { fill.style.width = "0%"; fill.style.left = "50%"; fill.classList.remove("warn"); }
      else {
        const FS = 5;
        const f = Math.max(-1, Math.min(1, err / FS));
        const halfPct = Math.abs(f) * 50;
        fill.style.width = halfPct + "%";
        fill.style.left = f >= 0 ? "50%" : (50 - halfPct) + "%";
        fill.classList.toggle("warn", Math.abs(err) > FS * 0.6);
      }
    }
  }

  // ===== #58 orbit / circle ===============================================
  let orbitCenter = null;            // {lat, lon}
  let orbitArmed = false;
  let orbitCircle = null;            // Leaflet circle preview/active
  let orbitCenterMk = null;          // centre marker
  const leaflet = VA.map && VA.map.leaflet;
  const orbitDir = segValue("orbit-dir", "cw");
  bindSlider("orbit-radius", "orbit-radius-val", () => drawOrbit());
  bindSlider("orbit-speed", "orbit-speed-val");

  function setOrbitArmed(on) {
    orbitArmed = on;
    const btn = $("orbit-pick");
    if (btn) {
      btn.classList.toggle("active", on);
      btn.textContent = on ? "Tap the map… (cancel)" : "📍 Tap map to set centre";
    }
    const mapEl = $("map");
    if (mapEl) mapEl.classList.toggle("goto-arming", on);
  }
  const orbitPick = $("orbit-pick");
  if (orbitPick) orbitPick.addEventListener("click", () => setOrbitArmed(!orbitArmed));

  // Consume map taps while arming to set the centre.
  if (VA.map && VA.map.addClickConsumer) {
    VA.map.addClickConsumer((lat, lon) => {
      if (!orbitArmed) return false;
      orbitCenter = { lat, lon };
      setOrbitArmed(false);
      VA.setText("orbit-center", lat.toFixed(5) + ", " + lon.toFixed(5));
      const go = $("orbit-go"); if (go) go.disabled = false;
      drawOrbit();
      return true; // handled — don't fall through to route/go-to
    });
  }

  function drawOrbit() {
    if (!leaflet || typeof L === "undefined") return;
    const r = fnum("orbit-radius");
    if (!orbitCenter) {
      if (orbitCircle) { leaflet.removeLayer(orbitCircle); orbitCircle = null; }
      if (orbitCenterMk) { leaflet.removeLayer(orbitCenterMk); orbitCenterMk = null; }
      return;
    }
    const ll = [orbitCenter.lat, orbitCenter.lon];
    if (!orbitCircle) {
      orbitCircle = L.circle(ll, { radius: r, color: "#8b7bff", weight: 2, dashArray: "6,6", fill: false }).addTo(leaflet);
      orbitCenterMk = L.circleMarker(ll, { radius: 5, color: "#8b7bff", fillColor: "#8b7bff", fillOpacity: 0.8, weight: 2 }).addTo(leaflet).bindTooltip("Orbit centre");
    } else {
      orbitCircle.setLatLng(ll).setRadius(r);
      if (orbitCenterMk) orbitCenterMk.setLatLng(ll);
    }
  }

  function orbitCmd() {
    return {
      type: "orbit",
      center_lat: orbitCenter.lat,
      center_lon: orbitCenter.lon,
      radius_m: fnum("orbit-radius"),
      direction: orbitDir(),
      speed_knots: fnum("orbit-speed"),
    };
  }
  const orbitGo = $("orbit-go");
  if (orbitGo) orbitGo.addEventListener("click", () => { if (orbitCenter) send(orbitCmd()); });
  // Live re-send direction/speed/radius changes while active.
  ["orbit-radius", "orbit-speed"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("change", () => { if (orbitCenter && VA.last && VA.last.mode === "orbit") send(orbitCmd()); });
  });
  const orbitDirSeg = $("orbit-dir");
  if (orbitDirSeg) orbitDirSeg.querySelectorAll("button[data-val]").forEach((b) =>
    b.addEventListener("click", () => { if (orbitCenter && VA.last && VA.last.mode === "orbit") send(orbitCmd()); }));

  function updateOrbit(t) {
    const o = t.orbit || {};
    const range = VA.fin(o.range_m);
    const rtgt = VA.fin(o.radius_m);
    VA.setText("orbit-range", range === null ? "—" : range.toFixed(0));
    VA.setText("orbit-rtgt", rtgt === null ? "—" : rtgt.toFixed(0));
    // While orbiting, adopt the backend's centre/radius if telemetry provides one.
    if (t.mode === "orbit") {
      const clat = VA.fin(o.center_lat), clon = VA.fin(o.center_lon);
      if (clat !== null && clon !== null) {
        orbitCenter = { lat: clat, lon: clon };
        VA.setText("orbit-center", clat.toFixed(5) + ", " + clon.toFixed(5));
        const go = $("orbit-go"); if (go) go.disabled = false;
      }
      drawOrbit();
    }
  }

  // ===== #59 trolling pattern =============================================
  bindSlider("troll-amp", "troll-amp-val");
  bindSlider("troll-period", "troll-period-val");
  bindSlider("troll-speed", "troll-speed-val");
  function trollCmd() {
    return {
      type: "trolling",
      base_heading: null,                 // null → backend uses current heading
      amplitude_deg: fnum("troll-amp"),
      period_s: fnum("troll-period"),
      speed_knots: fnum("troll-speed"),
    };
  }
  function startTroll() { send(trollCmd()); }
  const trollGo = $("troll-go");
  if (trollGo) trollGo.addEventListener("click", startTroll);
  ["troll-amp", "troll-period", "troll-speed"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("change", () => { if (VA.last && VA.last.mode === "trolling") startTroll(); });
  });

  // Live weave indicator: drive a dot left↔right from telemetry. Prefer a
  // backend-supplied phase if present, else synthesize from period locally.
  function updateTroll(t) {
    const dot = $("troll-dot");
    if (!dot) return;
    if (t.mode !== "trolling") { dot.style.left = "50%"; return; }
    const amp = Math.max(1, fnum("troll-amp"));
    let frac;                            // -1..1 across the track
    const tr = t.trolling || {};
    if (VA.fin(tr.offset_deg) !== null) {
      frac = Math.max(-1, Math.min(1, tr.offset_deg / amp));
    } else if (VA.fin(t.target_heading) !== null && VA.fin(t.heading_deg) !== null) {
      let d = ((t.target_heading - t.heading_deg + 540) % 360) - 180;
      frac = Math.max(-1, Math.min(1, d / amp));
    } else {
      const period = Math.max(1, fnum("troll-period"));
      frac = Math.sin((Date.now() / 1000) * (2 * Math.PI / period));
    }
    dot.style.left = (50 + frac * 50) + "%";
  }

  // ===== telemetry fan-out ================================================
  VA.onTelemetry(function (t) {
    updateContour(t);
    updateOrbit(t);
    updateTroll(t);
    // Highlight the More button when the backend is in one of these modes.
    const inGuided = ["contour_follow", "orbit", "trolling"].includes(t.mode);
    if (moreToggle && !(moreMenu && !moreMenu.classList.contains("hidden"))) {
      moreToggle.classList.toggle("active", inGuided);
    }
  });
})();
