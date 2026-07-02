/* Vanchor-NG — app core / mode rail module.
 *
 * Shared UI plumbing for the hand-wired control modules: the `$` id helper, the
 * `send` shortcut, the `bindSlider` helper, and the mode rail + contextual
 * panel dispatch (rail clicks, telemetry-driven panel sync, rail highlight).
 *
 * Everything shared between the split control modules is hung off `VA.ui` so the
 * classic <script> files (loaded in order) can cooperate without a build step.
 * Mode-rail "enter mode" commands are looked up from a registry the control
 * modules populate (VA.ui.modeCommands), so this file owns the dispatch without
 * depending on DOM refs that live in those modules.
 */
"use strict";

(function () {
  const send = VA.send;
  const $ = (id) => document.getElementById(id);

  // ---- slider helper ------------------------------------------------------
  function bindSlider(id, outId, fn) {
    const el = $(id), out = $(outId);
    if (!el) return;
    // Bind time: refresh the DISPLAY only — never invoke `fn`. Calling the
    // callback here would fire a real command (e.g. a manual thrust/steer send)
    // on page load / service-worker reload before any user interaction, which
    // the backend treats as intent and would cancel anchor-hold/route. Only a
    // genuine user `input` event may invoke the callback.
    if (out) out.textContent = el.value;
    el.addEventListener("input", () => {
      if (out) out.textContent = el.value;
      if (fn) fn(parseFloat(el.value));
    });
  }

  // ---- mode rail + contextual panels --------------------------------------
  let currentMode = null;

  function applyModePanels(mode) {
    if (mode === currentMode) return;
    currentMode = mode;
    document.querySelectorAll(".ctx-panel").forEach((p) =>
      p.classList.toggle("active", p.dataset.for === mode));
    // a new mode supersedes any pending go-to destination
    VA.map.clearGotoMarker();
  }

  // stop has no panel of its own; show the manual panel when stopped.
  const panelFor = (m) => (m === "stop" ? "manual" : m === "anchor_ml" ? "anchor_hold" : m);

  function highlightRail() {
    document.querySelectorAll(".mode-btn[data-mode]").forEach((b) =>
      b.classList.toggle("active", b.dataset.mode === currentMode));
  }

  // On mobile, picking a mode should slide the bottom sheet up AND scroll the
  // mode rail out of view, so the mode's options fill the sheet — instead of
  // forcing the user to drag it up and then scroll past the mode buttons.
  function revealModeOptions() {
    if (!(VA.sheet && VA.sheet.active())) return;
    VA.sheet.reveal("mid");
    // Wait for the sheet's expand transition, then scroll the rail off-screen:
    // target the guided nav bar if it's showing (keeps Speed/Pause visible),
    // else the active panel — either way the mode buttons scroll away.
    setTimeout(() => {
      const dock = document.getElementById("dock");
      if (!dock) return;
      const navbar = document.querySelector("#dock-navbar:not(.hidden)");
      const target = navbar || document.querySelector(".ctx-panel.active");
      if (target && target.scrollIntoView) {
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }, 170);
  }

  // Registry of "enter mode" command builders, populated by the control modules
  // (manual/heading/anchor/drift/route). Keyed by the rail button's data-mode.
  const modeCommands = {};

  // Shared surface for the split control modules. `currentMode` is exposed as a
  // getter so modules always read the live value.
  VA.ui = {
    $,
    send,
    bindSlider,
    applyModePanels,
    highlightRail,
    panelFor,
    modeCommands,
    revealModeOptions,
    get currentMode() { return currentMode; },
  };

  document.querySelectorAll(".mode-btn[data-mode]").forEach((b) =>
    b.addEventListener("click", () => {
      const m = b.dataset.mode;
      const cmd = modeCommands[m];
      if (cmd) cmd();
      // Show the selected mode's panel immediately (user intent). This is the key
      // fix for setup-style modes like Route/Waypoint: the boat doesn't enter
      // "waypoint" until a route exists, so the panel must follow the click — not
      // the backend mode — or you could never reach the route-building controls.
      applyModePanels(panelFor(m));
      highlightRail();
      if (m !== "stop") revealModeOptions();
    }));

  // Telemetry reflects the live mode: only switch the panel when the BACKEND mode
  // genuinely changes (e.g. arrival -> manual), so it never fights a user who is
  // mid-setup in a panel. The rail highlight tracks the shown panel.
  let lastTelemetryMode = null;
  VA.onTelemetry(function renderModes(t) {
    if (t.mode !== lastTelemetryMode) {
      lastTelemetryMode = t.mode;
      applyModePanels(panelFor(t.mode));
    }
    highlightRail();
  });
})();
