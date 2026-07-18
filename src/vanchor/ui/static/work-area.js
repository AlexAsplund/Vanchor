/* Vanchor-NG — "Work Area" guided mode (hop-and-hold across fishing/work spots).
 *
 * Lives behind the "More" overflow flyout (data-mode="work_area"). The boat
 * stations on each spot — optionally holding a per-spot heading — and advances
 * to the next either when the user taps the big "Go to next spot" button
 * (manual) or after a dwell time (timed). Spots are defined two ways:
 *
 *   (a) by tapping the map / editing the spot list — reuses the route editor's
 *       pending-waypoint API (VA.map.pending/setPending/addPending) so the
 *       waypoint markers + route line are shared, not forked. Per-spot heading
 *       is carried as an optional `heading` field hung on the pending objects.
 *   (b) by drawing an area + spacing → POST /api/route/work_area → the returned
 *       waypoints are loaded as the spots (reuses survey.js's polygon-draw UX).
 *
 * Start command (backend contract):
 *   {type:"work_area", waypoints:[{name,lat,lon,heading?}...],
 *    advance:"manual"|"timed", dwell_s, loop, patrol, throttle}
 * The big button sends {type:"next_spot"}.
 *
 * Telemetry: mode=="work_area", work_holding (bool), work_dwell_remaining_s,
 * work_spot_count, active_waypoint, waypoints[i].heading.
 *
 * Degrades gracefully: if /api/route/work_area 404s the area card disables; the
 * tap-to-place flow + Start still work without it.
 */
"use strict";

(function () {
  const send = VA.send;
  const $ = (id) => document.getElementById(id);
  if (!VA.map) return;

  // Mirror appcore's panel toggle for click-driven (intent) activation.
  function showPanel(mode) {
    document.querySelectorAll(".ctx-panel").forEach((p) =>
      p.classList.toggle("active", p.dataset.for === mode));
    if (VA.map.clearGotoMarker) VA.map.clearGotoMarker();
  }

  function setStatus(el, msg, kind) {
    if (!el) return;
    el.textContent = msg || "";
    el.className = "hint" + (kind ? " " + kind : "");
  }

  // ===== spot placement (reuses the shared pending-waypoint editor) =========
  let armed = false;
  function setArmed(on) {
    armed = !!on;
    const b = $("wa-arm");
    if (b) {
      b.classList.toggle("active", armed);
      b.textContent = armed ? "✓ Tap map to add — done" : "＋ Add spots";
    }
    const mapEl = $("map");
    if (mapEl) mapEl.classList.toggle("goto-arming", armed);
  }
  const armBtn = $("wa-arm");
  if (armBtn) armBtn.addEventListener("click", () => setArmed(!armed));

  // Consume map taps only while arming, so an ordinary tap doesn't litter spots
  // and we don't fight the route editor's own map-click handler.
  if (VA.map.addClickConsumer) {
    VA.map.addClickConsumer((lat, lon) => {
      if (!armed) return false;
      VA.map.addPending(lat, lon);
      renderList();
      return true; // handled — don't fall through to route/go-to
    });
  }

  // Per-spot heading lives directly on the pending objects ({name,lat,lon,
  // heading?}); the map module ignores extra fields, so drags/redraws preserve
  // it. Blank input => heading omitted (no hold heading).
  function renderList() {
    const list = $("wa-list");
    if (!list) return;
    list.innerHTML = "";
    const spots = VA.map.pending();
    if (!spots.length) {
      const li = document.createElement("li");
      li.className = "wp-empty";
      li.textContent = "No spots yet.";
      list.appendChild(li);
      VA.map.redrawWaypoints();
      return;
    }
    spots.forEach((w, i) => {
      const li = document.createElement("li");
      const ix = document.createElement("span");
      ix.className = "ix";
      ix.textContent = (i + 1) + ".";

      const name = document.createElement("input");
      name.className = "wp-name";
      name.type = "text";
      name.value = w.name;
      name.setAttribute("aria-label", "spot name");
      name.addEventListener("input", () => { w.name = name.value; });

      const hdg = document.createElement("input");
      hdg.className = "wa-hdg";
      hdg.type = "number";
      hdg.min = "0";
      hdg.max = "359";
      hdg.step = "1";
      hdg.placeholder = "hdg°";
      hdg.title = "Optional hold heading (deg). Blank = none.";
      hdg.setAttribute("aria-label", "spot heading degrees");
      hdg.value = Number.isFinite(w.heading) ? Math.round(w.heading) : "";
      hdg.addEventListener("input", () => {
        const v = parseFloat(hdg.value);
        if (Number.isFinite(v)) w.heading = ((v % 360) + 360) % 360;
        else delete w.heading;
      });

      const del = document.createElement("button");
      del.className = "del";
      del.textContent = "✕";
      del.setAttribute("aria-label", "delete spot");
      del.addEventListener("click", () => { spots.splice(i, 1); renderList(); });

      li.append(ix, name, hdg, del);
      list.appendChild(li);
    });
    VA.map.redrawWaypoints();
  }

  const clearBtn = $("wa-clear");
  if (clearBtn) clearBtn.addEventListener("click", () => {
    VA.map.setPending([]);
    renderList();
    setArmed(false);
    if (VA.routeEditor && VA.routeEditor.clearLoop) VA.routeEditor.clearLoop();
  });

  // ===== config controls ====================================================
  // advance: manual | timed (segmented control; mirrors guided.js's segValue).
  function segValue(id, dflt) {
    const seg = $(id);
    let val = dflt;
    if (seg) {
      seg.querySelectorAll("button[data-val]").forEach((b) => {
        if (b.classList.contains("on")) val = b.dataset.val;
        b.addEventListener("click", () => {
          val = b.dataset.val;
          seg.querySelectorAll("button[data-val]").forEach((x) => x.classList.toggle("on", x === b));
          syncAdvance();
        });
      });
    }
    return () => val;
  }
  const advance = segValue("wa-advance", "manual");
  function syncAdvance() {
    const row = $("wa-dwell-row");
    if (row) row.classList.toggle("hidden", advance() !== "timed");
  }

  function bindSlider(id, outId, fn) {
    const el = $(id), out = $(outId);
    if (!el) return;
    const update = () => { if (out) out.textContent = el.value; if (fn) fn(parseFloat(el.value)); };
    el.addEventListener("input", update);
    update();
  }
  bindSlider("wa-dwell", "wa-dwell-val");
  bindSlider("wa-throttle", "wa-throttle-val");
  syncAdvance();

  const loopBox = $("wa-loop");
  const patrolBox = $("wa-patrol");
  const holdHdgBox = $("wa-hold-hdg");

  function buildCmd() {
    const spots = VA.map.pending();
    const waypoints = spots.map((w) => {
      const o = { name: w.name, lat: w.lat, lon: w.lon };
      if (Number.isFinite(w.heading)) o.heading = w.heading;
      else if (holdHdgBox && holdHdgBox.checked && Number.isFinite(VA.last && VA.last.heading_deg)) {
        // "Hold heading at each spot" with no per-spot value: hold the heading the
        // boat had when starting (a single sensible default). Per-spot inputs win.
        o.heading = VA.last.heading_deg;
      }
      return o;
    });
    const cmd = {
      type: "work_area",
      waypoints,
      advance: advance(),
      dwell_s: parseFloat(($("wa-dwell") || { value: "60" }).value),
      loop: !!(loopBox && loopBox.checked),
      patrol: !!(patrolBox && patrolBox.checked),
      throttle: parseFloat(($("wa-throttle") || { value: "60" }).value) / 100,
    };
    return cmd;
  }

  function startWorkArea() {
    const spots = VA.map.pending();
    if (!spots.length) { setStatus($("wa-status"), "Add at least one spot first.", "err"); return; }
    send(buildCmd());
    // The spots are now ACTIVE — they come back via telemetry as the coloured
    // route. Clear the editable pending pins so they don't sit on top.
    VA.map.setPending([]);
    renderList();
    setArmed(false);
    setStatus($("wa-status"), spots.length + " spots started.", "ok");
  }
  const goBtn = $("wa-go");
  if (goBtn) goBtn.addEventListener("click", startWorkArea);

  // Activating the mode from the More flyout just shows the panel (setup-style;
  // the boat doesn't enter work_area until Start is pressed). guided.js owns the
  // flyout open/close + click wiring for all .more-item[data-mode] buttons, so we
  // only need a panel-show hook for our item without double-binding the click.
  const moreMenu = $("more-menu");
  if (moreMenu) {
    const item = moreMenu.querySelector('.more-item[data-mode="work_area"]');
    if (item) item.addEventListener("click", () => { showPanel("work_area"); });
  }
  // Make the rail/telemetry dispatch aware of the mode (appcore reads this map);
  // a no-op command so clicking elsewhere that resolves to work_area is safe.
  if (VA.ui && VA.ui.modeCommands) VA.ui.modeCommands.work_area = () => { showPanel("work_area"); };

  // ===== area generation (flavor C) — reuses survey.js's draw UX ============
  let area = null;
  let drawing = false;
  let areaAvailable = null; // null=unknown; false after a 404
  const areaCard = $("wa-area-card");
  const boxBtn = $("wa-area-box");
  const freeBtn = $("wa-area-free");
  const areaClearBtn = $("wa-area-clear");
  const areaOpts = $("wa-area-opts");
  const spacingInput = $("wa-spacing");
  const genBtn = $("wa-generate");
  const areaStatus = $("wa-area-status");
  const canDraw = !!(VA.map.startAreaSelect && boxBtn && genBtn);

  function setDrawing(on, mode) {
    drawing = !!on;
    if (boxBtn) boxBtn.classList.toggle("active", drawing && mode === "box");
    if (freeBtn) freeBtn.classList.toggle("active", drawing && mode === "freehand");
  }
  function showOpts(on) {
    if (areaOpts) areaOpts.classList.toggle("hidden", !on);
    if (genBtn) genBtn.disabled = !on || areaAvailable === false;
    if (areaClearBtn) areaClearBtn.classList.toggle("hidden", !on);
  }
  function armDraw(mode) {
    if (!canDraw) return;
    if (areaAvailable === false) { setStatus(areaStatus, "Spot generation unavailable on this runtime.", "err"); return; }
    if (drawing) { VA.map.cancelAreaSelect(); setDrawing(false); setStatus(areaStatus, "Draw cancelled.", ""); return; }
    setStatus(areaStatus, mode === "box" ? "Drag a box over the work area." : "Draw a shape around the work area (release to close).", "busy");
    showOpts(false);
    setDrawing(true, mode);
    VA.map.startAreaSelect({
      mode,
      onDone(result) {
        setDrawing(false);
        if (!result || !result.polygon || result.polygon.length < 3) {
          setStatus(areaStatus, "Area too small — try again.", "err");
          area = null;
          showOpts(false);
          return;
        }
        area = result;
        setStatus(areaStatus, "Area selected. Set spacing and generate.", "ok");
        showOpts(true);
      },
    });
  }
  if (boxBtn) boxBtn.addEventListener("click", () => armDraw("box"));
  if (freeBtn) freeBtn.addEventListener("click", () => armDraw("freehand"));
  if (areaClearBtn) areaClearBtn.addEventListener("click", () => {
    VA.map.cancelAreaSelect();
    VA.map.clearAreaShape();
    setDrawing(false);
    area = null;
    showOpts(false);
    setStatus(areaStatus, "", "");
  });
  if (spacingInput) {
    const out = $("wa-spacing-val");
    const sync = () => { if (out) out.textContent = spacingInput.value; };
    spacingInput.addEventListener("input", sync);
    sync();
  }

  async function generate() {
    if (areaAvailable === false) { setStatus(areaStatus, "Spot generation unavailable on this runtime.", "err"); return; }
    if (!area || !area.polygon || area.polygon.length < 3) { setStatus(areaStatus, "Draw an area first.", "err"); return; }
    const spacing = spacingInput ? parseFloat(spacingInput.value) : 40;
    const body = { polygon: area.polygon, spacing_m: spacing };
    if (genBtn) genBtn.disabled = true;
    setStatus(areaStatus, "Generating spots…", "busy");
    VA.logLine("» route/work_area " + JSON.stringify({ spacing_m: spacing, points: area.polygon.length }));
    let r;
    try {
      const resp = await fetch("/api/route/work_area", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (resp.status === 404) { markAreaUnavailable(); return; }
      r = await resp.json();
    } catch (err) {
      setStatus(areaStatus, "Request failed: " + err, "err");
      if (genBtn) genBtn.disabled = false;
      return;
    }
    if (!r || r.ok === false || !Array.isArray(r.waypoints) || !r.waypoints.length) {
      setStatus(areaStatus, (r && r.message) || "No spots produced — try wider spacing or a larger area.", "err");
      if (genBtn) genBtn.disabled = false;
      return;
    }
    const spots = r.waypoints
      .filter((w) => w && Number.isFinite(w.lat) && Number.isFinite(w.lon))
      .map((w, i) => ({ name: w.name || ("Spot " + (i + 1)), lat: w.lat, lon: w.lon }));
    VA.map.setPending(spots);
    if (VA.routeEditor && VA.routeEditor.clearLoop) VA.routeEditor.clearLoop();
    renderList();
    VA.map.clearAreaShape();
    area = null;
    showOpts(false);
    setStatus(areaStatus, spots.length + " spots loaded. Review above and press Start Work Area.", "ok");
    if (genBtn) genBtn.disabled = false;
  }
  if (genBtn) genBtn.addEventListener("click", generate);

  function markAreaUnavailable() {
    areaAvailable = false;
    if (areaCard) areaCard.classList.add("unavailable");
    if (boxBtn) boxBtn.disabled = true;
    if (freeBtn) freeBtn.disabled = true;
    if (genBtn) genBtn.disabled = true;
    setDrawing(false);
    setStatus(areaStatus, "Spot-generation endpoint not available on this runtime.", "err");
  }

  // ===== the big "Go to next spot" button ===================================
  const nextBtn = $("wa-next");
  if (nextBtn) nextBtn.addEventListener("click", () => send({ type: "next_spot" }));

  function fmtCountdown(s) {
    if (!Number.isFinite(s) || s < 0) return null;
    s = Math.round(s);
    const m = Math.floor(s / 60), ss = s % 60;
    return m > 0 ? m + ":" + String(ss).padStart(2, "0") : ss + "s";
  }

  VA.onTelemetry(function renderWorkArea(t) {
    if (!nextBtn) return;
    const holding = t.mode === "work_area" && t.work_holding === true;
    nextBtn.classList.toggle("hidden", !holding);
    if (!holding) return;

    const count = Number.isFinite(t.work_spot_count)
      ? t.work_spot_count
      : (Array.isArray(t.waypoints) ? t.waypoints.length : 0);
    const ix = Number.isInteger(t.active_waypoint) ? t.active_waypoint : 0;
    let sub = "Spot " + (count ? (ix + 1) : 0) + " / " + count;
    // In timed mode, show the dwell countdown alongside the spot index.
    const cd = fmtCountdown(t.work_dwell_remaining_s);
    if (cd !== null) sub += " · " + cd;
    VA.setText("wa-next-sub", sub);
  });

  // Render the (empty) spot list once at startup.
  renderList();
})();
