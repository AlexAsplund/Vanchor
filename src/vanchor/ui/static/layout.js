/* Vanchor-NG — draggable HUD panels (task #39) + layout profiles (task #40).
 *
 * #39: makes the floating instrument panels (#hud, #steering-gauge, and the
 * #dock) draggable to anywhere on screen via pointer events (drag from a small
 * handle or the panel chrome). Positions are clamped on-screen and persisted in
 * localStorage, restored on load. A "Reset layout" action clears custom
 * positions and returns to the CSS default (which keeps the responsive mobile
 * layout working when no custom position is set).
 *
 * #40: layout profiles capture the current layout — panel positions, marker +
 * depth + sea-mark visibility, theme, and HUD widget prefs — under a name. A
 * small Profiles control lets you Save as…, apply, delete, and there is a
 * built-in Default profile. The active profile is remembered.
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);

  // Panels that can be repositioned. `handle` is the selector for the drag grip
  // inside the panel; clicking elsewhere in the panel still works normally.
  const PANELS = [
    { id: "hud", key: "hud" },
    { id: "steering-gauge", key: "steering-gauge" },
    { id: "dock", key: "dock" },
  ];
  const POS_KEY = "vanchor-panel-pos";
  const PROFILE_KEY = "vanchor-profiles";
  const ACTIVE_KEY = "vanchor-profile-active";

  // ---- position persistence ----------------------------------------------
  let positions = {};       // { id: {left, top} }
  function loadPositions() {
    try { const raw = localStorage.getItem(POS_KEY); if (raw) positions = JSON.parse(raw) || {}; }
    catch (e) { positions = {}; }
  }
  function savePositions() {
    try { localStorage.setItem(POS_KEY, JSON.stringify(positions)); } catch (e) { /* ignore */ }
  }

  function clamp(v, min, max) { return Math.max(min, Math.min(max, v)); }

  function applyPosition(id) {
    const el = $(id);
    if (!el) return;
    const p = positions[id];
    if (!p || !Number.isFinite(p.left) || !Number.isFinite(p.top)) {
      // No custom position: clear inline styles so the CSS (incl. responsive
      // mobile rules) takes over.
      el.style.left = el.style.top = el.style.right = el.style.bottom = "";
      el.classList.remove("dragged");
      return;
    }
    const r = el.getBoundingClientRect();
    const maxL = Math.max(0, window.innerWidth - r.width);
    const maxT = Math.max(0, window.innerHeight - r.height);
    el.classList.add("dragged");
    el.style.left = clamp(p.left, 0, maxL) + "px";
    el.style.top = clamp(p.top, 0, maxT) + "px";
    el.style.right = "auto";
    el.style.bottom = "auto";
  }
  function applyAllPositions() { PANELS.forEach((p) => applyPosition(p.id)); }

  // ---- drag wiring --------------------------------------------------------
  // A panel is grabbed from its drag handle (added below). On phones the panels
  // double as bottom-sheet/strips, so we only enable free-drag on pointers that
  // start on the handle, and we clamp to the viewport.
  function makeDraggable(panel) {
    const el = $(panel.id);
    if (!el) return;
    const handle = el.querySelector(".panel-drag-handle");
    if (!handle) return;
    let startX, startY, baseL, baseT, dragging = false;

    handle.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      // switch to absolute top/left positioning at the current spot
      el.classList.add("dragged");
      el.style.left = r.left + "px";
      el.style.top = r.top + "px";
      el.style.right = "auto";
      el.style.bottom = "auto";
      baseL = r.left; baseT = r.top;
      startX = e.clientX; startY = e.clientY;
      dragging = true;
      try { handle.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
      el.classList.add("dragging");
    });
    handle.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const r = el.getBoundingClientRect();
      const maxL = Math.max(0, window.innerWidth - r.width);
      const maxT = Math.max(0, window.innerHeight - r.height);
      const left = clamp(baseL + (e.clientX - startX), 0, maxL);
      const top = clamp(baseT + (e.clientY - startY), 0, maxT);
      el.style.left = left + "px";
      el.style.top = top + "px";
    });
    const end = (e) => {
      if (!dragging) return;
      dragging = false;
      el.classList.remove("dragging");
      try { handle.releasePointerCapture(e.pointerId); } catch (err) { /* ignore */ }
      const r = el.getBoundingClientRect();
      positions[panel.id] = { left: r.left, top: r.top };
      savePositions();
    };
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);
  }

  // Inject a small drag handle into each panel (kept theme-consistent in CSS).
  function injectHandle(panel) {
    const el = $(panel.id);
    if (!el || el.querySelector(".panel-drag-handle")) return;
    const h = document.createElement("div");
    h.className = "panel-drag-handle";
    h.title = "Drag to move";
    h.setAttribute("aria-label", "Drag panel");
    h.innerHTML = '<span class="pdh-grip"></span>';
    el.appendChild(h);
  }

  // ---- reset layout -------------------------------------------------------
  function resetLayout() {
    positions = {};
    savePositions();
    PANELS.forEach((p) => {
      const el = $(p.id);
      if (el) { el.style.left = el.style.top = el.style.right = el.style.bottom = ""; el.classList.remove("dragged"); }
    });
  }

  // ---- layout profiles (#40) ---------------------------------------------
  // A profile snapshots: panel positions, overlay/marker visibility, theme,
  // and HUD widget prefs (read straight from the relevant localStorage keys
  // the other modules already own, so we stay the single source of truth).
  function readVisibility() {
    return {
      markers: !!(VA.markers && VA.markers.isVisible && VA.markers.isVisible()),
      depth: !!($("depth-show") && $("depth-show").checked),
    };
  }
  function snapshot() {
    let hud = null, theme = null;
    try { hud = localStorage.getItem("vanchor-hud"); } catch (e) { /* ignore */ }
    try { theme = localStorage.getItem("vanchor-theme"); } catch (e) { /* ignore */ }
    return {
      positions: JSON.parse(JSON.stringify(positions)),
      visibility: readVisibility(),
      theme: theme || "dark",
      hud: hud,
    };
  }
  function applyProfile(prof) {
    if (!prof) return;
    positions = prof.positions ? JSON.parse(JSON.stringify(prof.positions)) : {};
    savePositions();
    applyAllPositions();
    // theme
    if (prof.theme) {
      const box = $("theme-toggle-box");
      if (box) { box.checked = prof.theme !== "light"; box.dispatchEvent(new Event("change")); }
    }
    // hud prefs: write the key then let app.js re-read by toggling its checkbox.
    if (prof.hud) {
      try { localStorage.setItem("vanchor-hud", prof.hud); } catch (e) { /* ignore */ }
      // nudge app.js to re-apply by firing change on the master toggle
      const showBox = $("hud-show");
      try {
        const o = JSON.parse(prof.hud);
        if (showBox && typeof o.show === "boolean") { showBox.checked = o.show; showBox.dispatchEvent(new Event("change")); }
        document.querySelectorAll(".hud-toggle").forEach((cb) => {
          const w = cb.dataset.widget;
          if (o && typeof o[w] === "boolean" && cb.checked !== o[w]) { cb.checked = o[w]; cb.dispatchEvent(new Event("change")); }
        });
      } catch (e) { /* ignore */ }
    }
    // visibility
    if (prof.visibility) {
      if (VA.markers && VA.markers.setVisible) VA.markers.setVisible(!!prof.visibility.markers);
      const depthBox = $("depth-show");
      if (depthBox && depthBox.checked !== !!prof.visibility.depth) { depthBox.checked = !!prof.visibility.depth; depthBox.dispatchEvent(new Event("change")); }
    }
  }

  let profiles = { Default: null }; // Default = "no custom layout"
  let activeProfile = "Default";
  function loadProfiles() {
    try { const raw = localStorage.getItem(PROFILE_KEY); if (raw) { const o = JSON.parse(raw); if (o && typeof o === "object") profiles = o; } }
    catch (e) { /* ignore */ }
    if (!("Default" in profiles)) profiles.Default = null;
    try { activeProfile = localStorage.getItem(ACTIVE_KEY) || "Default"; } catch (e) { activeProfile = "Default"; }
    if (!(activeProfile in profiles)) activeProfile = "Default";
  }
  function saveProfiles() {
    try { localStorage.setItem(PROFILE_KEY, JSON.stringify(profiles)); } catch (e) { /* ignore */ }
    try { localStorage.setItem(ACTIVE_KEY, activeProfile); } catch (e) { /* ignore */ }
  }
  function renderProfileSelect() {
    const sel = $("profile-select");
    if (!sel) return;
    sel.innerHTML = "";
    Object.keys(profiles).forEach((name) => {
      const o = document.createElement("option");
      o.value = name; o.textContent = name;
      if (name === activeProfile) o.selected = true;
      sel.appendChild(o);
    });
    const delBtn = $("profile-delete");
    if (delBtn) delBtn.disabled = activeProfile === "Default";
  }

  function wireProfiles() {
    const sel = $("profile-select");
    const saveAsBtn = $("profile-saveas");
    const delBtn = $("profile-delete");
    const resetBtn = $("layout-reset");

    if (sel) sel.addEventListener("change", () => {
      activeProfile = sel.value;
      if (activeProfile === "Default") resetLayout();
      else applyProfile(profiles[activeProfile]);
      saveProfiles();
      renderProfileSelect();
    });
    if (saveAsBtn) saveAsBtn.addEventListener("click", () => {
      const name = (window.prompt("Save layout profile as:", activeProfile === "Default" ? "My layout" : activeProfile) || "").trim();
      if (!name) return;
      profiles[name] = snapshot();
      activeProfile = name;
      saveProfiles();
      renderProfileSelect();
    });
    if (delBtn) delBtn.addEventListener("click", () => {
      if (activeProfile === "Default") return;
      delete profiles[activeProfile];
      activeProfile = "Default";
      saveProfiles();
      renderProfileSelect();
    });
    if (resetBtn) resetBtn.addEventListener("click", () => {
      resetLayout();
      activeProfile = "Default";
      saveProfiles();
      renderProfileSelect();
    });
  }

  // ---- init --------------------------------------------------------------
  loadPositions();
  PANELS.forEach(injectHandle);
  PANELS.forEach(makeDraggable);
  applyAllPositions();
  window.addEventListener("resize", applyAllPositions);

  loadProfiles();
  wireProfiles();
  renderProfileSelect();
  // Apply the remembered active profile on load (Default = CSS layout).
  if (activeProfile !== "Default" && profiles[activeProfile]) applyProfile(profiles[activeProfile]);

  VA.layout = { resetLayout, applyAllPositions };
})();
