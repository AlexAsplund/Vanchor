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
    // Expand to FULL (as tall as dragging the sheet all the way up) so the
    // mode's options get the whole sheet, then scroll the mode rail off-screen.
    VA.sheet.reveal("full");
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
    bindHold,
    applyModePanels,
    highlightRail,
    panelFor,
    modeCommands,
    revealModeOptions,
    get currentMode() { return currentMode; },
  };
  // Expose helpers globally for cross-module use.
  VA.bindHold = bindHold;
  VA.toast = toast;

  // ---- device-availability gating -----------------------------------------
  // A "Not connected" device disables the modes that need it (backend
  // `mode_availability`). Grey the buttons out with the reason, and block a
  // click before any mode handler fires (capture phase covers appcore + the
  // guided/mobile handlers).
  let modeAvail = {};
  function applyModeAvailability(ma) {
    modeAvail = ma || {};
    document.querySelectorAll(".mode-btn[data-mode], .more-item[data-mode]").forEach((b) => {
      const info = modeAvail[b.dataset.mode];
      const blocked = !!(info && info.available === false);
      b.classList.toggle("mode-unavailable", blocked);
      if (blocked) {
        b.setAttribute("aria-disabled", "true");
        b.title = info.reason || "Unavailable";
        b.dataset.reason = info.reason || "Unavailable";
      } else {
        b.removeAttribute("aria-disabled");
        if (b.dataset.reason) { b.removeAttribute("title"); delete b.dataset.reason; }
      }
    });
  }
  // ---- bindHold: hold-to-engage ring for drive-away actions ---------------
  // Attaches a progress ring (CSS animation on .sb-hold) and fires `fire` after
  // `ms` ms of continuous hold. Releases on pointerup/cancel.
  function bindHold(el, ms, fire) {
    if (!el) return;
    let t = null;
    let justFired = false;
    function start(e) {
      e.preventDefault();
      el.classList.add("sb-hold");
      el.style.setProperty("--hold-ms", ms + "ms");
      clearTimeout(t);
      t = setTimeout(() => {
        el.classList.remove("sb-hold");
        justFired = true;
        try { if (navigator.vibrate) navigator.vibrate(30); } catch (_) {}
        fire();
      }, ms);
    }
    function cancel() {
      clearTimeout(t); t = null;
      el.classList.remove("sb-hold");
    }
    el.addEventListener("pointerdown", start);
    el.addEventListener("pointerup", cancel);
    el.addEventListener("pointercancel", cancel);
    el.addEventListener("pointerleave", cancel);
    // A completed hold still emits a click on release — swallow it (capture
    // phase) so tap-hint handlers don't fire right after the real action.
    el.addEventListener("click", (e) => {
      if (justFired) {
        justFired = false;
        e.preventDefault();
        e.stopImmediatePropagation();
      }
    }, true);
    // Prevent context-menu on long-press (mobile).
    el.addEventListener("contextmenu", (e) => e.preventDefault());
  }

  // ---- VA.motorActive: true when the motor can be moving -------------------
  // Any non-manual mode counts (an autopilot may command thrust at any moment);
  // manual counts only with real commanded thrust. Drives #cm-stop visibility.
  let _motorActive = false;
  VA.onTelemetry(function (t) {
    _motorActive = !!t && (t.mode !== "manual"
      || Math.abs((t.motor && t.motor.thrust) || 0) > 0.05);
  });
  Object.defineProperty(VA, "motorActive", { get: () => _motorActive });

  // ---- VA.modeSuffix: honest degraded-state suffix for mode labels ---------
  // Driven ONLY by the safety governor's auto-stop flags — never inferred from
  // zero thrust (a station-keeper at rest idles at zero thrust and is NOT
  // stopped). Single source for sheet-mode + map badge so the copy never forks.
  VA.modeSuffix = function (t) {
    const s = t && t.safety;
    if (!s) return "";
    if (s.shallow_stop) return " — STOPPED (shallow)";
    if (s.nogo_stop) return " — STOPPED (no-go zone)";
    return "";
  };

  function toast(msg, opts) {
    let el = $("va-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "va-toast";
      el.className = "va-toast";
      document.body.appendChild(el);
    }
    // Clear old children.
    el.textContent = "";
    const o = (typeof opts === "object" && opts) || {};
    const ttl = Number.isFinite(o.ttl) ? o.ttl : 2600;
    const msgNode = document.createElement("span");
    msgNode.textContent = msg;
    el.appendChild(msgNode);
    if (o.actionLabel && o.onAction) {
      const btn = document.createElement("button");
      btn.textContent = o.actionLabel;
      btn.style.cssText =
        "margin-left:12px;border:0;background:rgba(255,255,255,0.18);color:#fff;" +
        "border-radius:999px;padding:3px 11px;font:700 13px inherit;cursor:pointer;";
      btn.addEventListener("click", () => {
        el.classList.remove("show");
        o.onAction();
      });
      el.appendChild(btn);
    }
    el.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.remove("show"), ttl);
  }
  // Capture-phase block: an unavailable mode click never reaches a mode handler.
  document.addEventListener("click", (e) => {
    const btn = e.target && e.target.closest &&
      e.target.closest(".mode-btn[data-mode], .more-item[data-mode]");
    if (btn && btn.classList.contains("mode-unavailable")) {
      e.stopPropagation();
      e.preventDefault();
      toast(btn.dataset.reason || "Unavailable — device not connected");
    }
  }, true);

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
    if (t.mode_availability) applyModeAvailability(t.mode_availability);
    highlightRail();
  });
})();
