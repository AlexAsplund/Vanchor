/* Vanchor-NG — always-on nav control bar (tasks #49 + #50).
 *
 * Lives in the dock and is shown whenever the boat is in a guided mode
 * (waypoint / heading_hold / follow_apb / drift). Provides:
 *
 *  - A big, touch-friendly SPEED control with a knots ⇄ % engine toggle.
 *      knots → `cruise {knots}`        (reflects telemetry cruise.target_knots)
 *      %     → `set_throttle {percent}` (reflects telemetry throttle_override.percent)
 *  - Pause / Resume / Stop:
 *      Pause  → `pause_nav`     Resume → `resume_nav`     Stop → `stop`
 *    State reflected from telemetry `nav.paused` / `nav.suspended_mode` with a
 *    "PAUSED — route held" banner and a highlighted Resume button while paused.
 *
 * Degrades gracefully if the backend lacks the new commands/telemetry.
 */
"use strict";

(function () {
  const send = VA.send;
  const $ = (id) => document.getElementById(id);

  const GUIDED = new Set([
    "waypoint", "heading_hold", "follow_apb", "drift",
    "contour_follow", "orbit", "trolling",
  ]);

  const bar = $("dock-navbar");
  const slider = $("speed-slider");
  const valEl = $("speed-val");
  const unitBtn = $("speed-unit");
  const unitLbl = $("speed-unit-lbl");
  const upBtn = $("speed-up");
  const dnBtn = $("speed-dn");
  const pauseBtn = $("nav-pause");
  const resumeBtn = $("nav-resume");
  const stopBtn = $("nav-stop");
  const banner = $("nav-paused-banner");
  if (!bar) return;

  // ---- speed control -----------------------------------------------------
  // Two units share one slider; we keep an independent value per unit and a
  // sensible range/step for each.
  const UNIT_KEY = "vanchor-speed-unit";
  const RANGE = { kn: { min: 0, max: 3.5, step: 0.1, dflt: 2.0 }, pct: { min: 0, max: 100, step: 5, dflt: 50 } };
  let unit = "kn";
  try { const u = localStorage.getItem(UNIT_KEY); if (u === "pct" || u === "kn") unit = u; } catch (e) { /* ignore */ }
  let value = { kn: RANGE.kn.dflt, pct: RANGE.pct.dflt };
  let dragging = false;

  function fmtVal(v) { return unit === "kn" ? v.toFixed(1) : String(Math.round(v)); }

  function applyUnitToSlider() {
    const r = RANGE[unit];
    slider.min = r.min; slider.max = r.max; slider.step = r.step;
    slider.value = value[unit];
    if (unitBtn) unitBtn.textContent = unit === "kn" ? "kn" : "%";
    if (unitLbl) unitLbl.textContent = unit === "kn" ? "kn" : "%";
    if (valEl) valEl.textContent = fmtVal(value[unit]);
  }

  function clampUnit(v) {
    const r = RANGE[unit];
    return Math.max(r.min, Math.min(r.max, v));
  }

  // Send the current speed for whichever unit is active.
  function sendSpeed() {
    if (unit === "kn") send({ type: "cruise", knots: value.kn });
    else send({ type: "set_throttle", percent: value.pct });
  }

  function setValue(v, doSend) {
    value[unit] = clampUnit(v);
    if (valEl) valEl.textContent = fmtVal(value[unit]);
    slider.value = value[unit];
    if (doSend) sendSpeed();
  }

  slider.addEventListener("input", () => { setValue(parseFloat(slider.value), false); });
  slider.addEventListener("change", () => { setValue(parseFloat(slider.value), true); });
  slider.addEventListener("pointerdown", () => { dragging = true; });
  ["pointerup", "pointercancel", "blur"].forEach((ev) => slider.addEventListener(ev, () => { dragging = false; }));
  if (upBtn) upBtn.addEventListener("click", () => setValue(value[unit] + RANGE[unit].step, true));
  if (dnBtn) dnBtn.addEventListener("click", () => setValue(value[unit] - RANGE[unit].step, true));

  if (unitBtn) unitBtn.addEventListener("click", () => {
    unit = unit === "kn" ? "pct" : "kn";
    try { localStorage.setItem(UNIT_KEY, unit); } catch (e) { /* ignore */ }
    applyUnitToSlider();
    sendSpeed();   // apply the active unit's target on switch
  });

  applyUnitToSlider();

  // ---- pause / resume / stop --------------------------------------------
  if (pauseBtn) pauseBtn.addEventListener("click", () => send({ type: "pause_nav" }));
  if (resumeBtn) resumeBtn.addEventListener("click", () => send({ type: "resume_nav" }));
  if (stopBtn) stopBtn.addEventListener("click", () => send({ type: "stop" }));

  // Remote-helm pause/resume mirror.
  const rmPause = $("rm-pause"), rmResume = $("rm-resume"), rmBanner = $("rm-paused-banner");
  if (rmPause) rmPause.addEventListener("click", () => send({ type: "pause_nav" }));
  if (rmResume) rmResume.addEventListener("click", () => send({ type: "resume_nav" }));

  // ---- telemetry reflection ---------------------------------------------
  // Which contextual panel is currently open (drives visibility during setup,
  // before the backend has actually entered the guided mode — e.g. route).
  function activePanelMode() {
    const p = document.querySelector(".ctx-panel.active");
    return p ? p.dataset.for : null;
  }

  VA.onTelemetry(function (t) {
    // Show the bar in guided modes (and while paused-with-suspended-mode), and
    // also while the user is in a guided mode's panel during setup.
    const nav = t.nav || {};
    const paused = !!nav.paused;
    const guided = GUIDED.has(t.mode) || GUIDED.has(activePanelMode()) ||
      (paused && GUIDED.has(nav.suspended_mode));
    bar.classList.toggle("hidden", !guided);

    // paused banner + resume highlight (both main + remote)
    [banner, rmBanner].forEach((b) => { if (b) b.classList.toggle("hidden", !paused); });
    if (resumeBtn) resumeBtn.classList.toggle("hot", paused);
    if (pauseBtn) pauseBtn.classList.toggle("hot", !paused && guided);
    if (rmResume) rmResume.classList.toggle("hot", paused);
    if (rmPause) rmPause.classList.toggle("hot", !paused);

    // reflect throttle override % when not dragging
    const th = t.throttle_override || {};
    if (unit === "pct" && !dragging && Number.isFinite(th.percent)) {
      value.pct = clampUnit(th.percent);
      slider.value = value.pct;
      if (valEl) valEl.textContent = fmtVal(value.pct);
    }
    // reflect cruise target knots when not dragging
    const cr = t.cruise || {};
    if (unit === "kn" && !dragging && cr.enabled && Number.isFinite(cr.target_knots)) {
      value.kn = clampUnit(cr.target_knots);
      slider.value = value.kn;
      if (valEl) valEl.textContent = fmtVal(value.kn);
    }
  });
})();
