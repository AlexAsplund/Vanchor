/* Vanchor-NG — "Map mode" area-survey route planner (task #47, UI half).
 *
 * Flow:
 *   1. The user arms area drawing on the map — either a box (drag a rectangle)
 *      or a freehand polygon (pointer-draw the shape). The live shape is shown
 *      while drawing (handled by VA.map.startAreaSelect).
 *   2. After closing the area we reveal a pass-spacing input (metres) and a Plan
 *      button.
 *   3. Plan → POST /api/route/survey {polygon:[[lat,lon]...], spacing_m,
 *      angle_deg:null} → load the returned {waypoints} into the route editor
 *      UNSTARTED via VA.map.setPending(...) + VA.routeEditor.refresh() for review.
 *
 * Self-gates: if the endpoint 404s on first use the controls disable and the
 * card shows the feature is unavailable, so the rest of the UI degrades cleanly.
 */
"use strict";

(function () {
  if (!window.VA || !VA.map || !VA.map.startAreaSelect) return;
  const $ = (id) => document.getElementById(id);

  const card = $("survey-card");
  const boxBtn = $("survey-box");
  const freeBtn = $("survey-free");
  const clearBtn = $("survey-clear");
  const opts = $("survey-opts");
  const spacingInput = $("survey-spacing");
  const spacingOut = $("survey-spacing-val");
  const planBtn = $("survey-plan");
  const statusEl = $("survey-status");
  if (!card || !boxBtn || !planBtn) return;

  let area = null;       // { polygon:[[lat,lon]...], bbox, mode }
  let drawing = false;   // currently arming a draw gesture
  let available = null;  // null=unknown; false after a 404

  function setStatus(msg, kind) {
    if (!statusEl) return;
    statusEl.textContent = msg || "";
    statusEl.className = "hint" + (kind ? " " + kind : "");
  }

  function setDrawing(on, mode) {
    drawing = !!on;
    boxBtn.classList.toggle("active", drawing && mode === "box");
    freeBtn.classList.toggle("active", drawing && mode === "freehand");
  }

  function showOpts(on) {
    if (opts) opts.classList.toggle("hidden", !on);
    planBtn.disabled = !on || available === false;
    if (clearBtn) clearBtn.classList.toggle("hidden", !on);
  }

  function arm(mode) {
    if (available === false) { setStatus("Survey planning unavailable on this runtime.", "err"); return; }
    if (drawing) { VA.map.cancelAreaSelect(); setDrawing(false); setStatus("Draw cancelled.", ""); return; }
    setStatus(mode === "box" ? "Drag a box over the area to survey." : "Draw a shape around the area (release to close).", "busy");
    showOpts(false);
    setDrawing(true, mode);
    VA.map.startAreaSelect({
      mode,
      onDone(result) {
        setDrawing(false);
        if (!result || !result.polygon || result.polygon.length < 3) {
          setStatus("Area too small — try again.", "err");
          area = null;
          showOpts(false);
          return;
        }
        area = result;
        const km2 = approxAreaKm2(result.polygon);
        setStatus("Area selected (~" + km2.toFixed(2) + " km²). Set pass spacing and plan.", "ok");
        showOpts(true);
      },
    });
  }

  // Rough planar area (km²) just for a friendly readout.
  function approxAreaKm2(poly) {
    if (poly.length < 3) return 0;
    const lat0 = poly[0][0] * Math.PI / 180;
    const mPerDegLat = 111132;
    const mPerDegLon = 111320 * Math.cos(lat0);
    let a = 0;
    for (let i = 0; i < poly.length; i++) {
      const [y1, x1] = poly[i];
      const [y2, x2] = poly[(i + 1) % poly.length];
      a += (x1 * mPerDegLon) * (y2 * mPerDegLat) - (x2 * mPerDegLon) * (y1 * mPerDegLat);
    }
    return Math.abs(a / 2) / 1e6;
  }

  boxBtn.addEventListener("click", () => arm("box"));
  if (freeBtn) freeBtn.addEventListener("click", () => arm("freehand"));
  if (clearBtn) clearBtn.addEventListener("click", () => {
    VA.map.cancelAreaSelect();
    VA.map.clearAreaShape();
    setDrawing(false);
    area = null;
    showOpts(false);
    setStatus("", "");
  });

  if (spacingInput && spacingOut) {
    const sync = () => { spacingOut.textContent = spacingInput.value; };
    spacingInput.addEventListener("input", sync);
    sync();
  }

  async function plan() {
    if (available === false) { setStatus("Survey planning unavailable on this runtime.", "err"); return; }
    if (!area || !area.polygon || area.polygon.length < 3) { setStatus("Draw an area first.", "err"); return; }
    const spacing = spacingInput ? parseFloat(spacingInput.value) : 20;
    const body = { polygon: area.polygon, spacing_m: spacing, angle_deg: null };
    planBtn.disabled = true;
    setStatus("Planning survey…", "busy");
    VA.logLine("» route/survey " + JSON.stringify({ spacing_m: spacing, points: area.polygon.length }));
    let r;
    try {
      const resp = await fetch("/api/route/survey", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (resp.status === 404) { markUnavailable(); return; }
      r = await resp.json();
    } catch (err) {
      setStatus("Request failed: " + err, "err");
      planBtn.disabled = false;
      return;
    }
    if (!r || r.ok === false || !Array.isArray(r.waypoints) || !r.waypoints.length) {
      setStatus((r && r.message) || "No survey route produced — try a wider spacing or larger area.", "err");
      planBtn.disabled = false;
      return;
    }
    const wps = r.waypoints
      .filter((w) => w && Number.isFinite(w.lat) && Number.isFinite(w.lon))
      .map((w, i) => ({ name: w.name || ("S" + (i + 1)), lat: w.lat, lon: w.lon }));
    VA.map.setPending(wps);
    if (VA.routeEditor && VA.routeEditor.clearLoop) VA.routeEditor.clearLoop();
    if (VA.routeEditor && VA.routeEditor.refresh) VA.routeEditor.refresh();
    VA.map.clearAreaShape();
    area = null;
    showOpts(false);
    setStatus(wps.length + " survey waypoints loaded. Review above and press Start route.", "ok");
    planBtn.disabled = false;
  }
  planBtn.addEventListener("click", plan);

  function markUnavailable() {
    available = false;
    card.classList.add("unavailable");
    boxBtn.disabled = true;
    if (freeBtn) freeBtn.disabled = true;
    planBtn.disabled = true;
    setDrawing(false);
    setStatus("Survey endpoint not available on this runtime.", "err");
  }
})();
