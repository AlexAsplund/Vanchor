/* Vanchor-NG — command menu navigation.
 *
 * The ☰ button opens a centred command modal (id="settings", repurposed from the
 * old right drawer by settings.js's setDrawer). This module owns the navigation
 * INSIDE that modal: a home grid of category tiles ⇄ a per-category panel of the
 * existing settings cards. settings.js still owns open/close (setDrawer) + the
 * scrim/✕/☰ wiring; on open it resets us to the home tile grid.
 *
 * IIFE, matching hud.js/settings.js style. Exposes VA.menu = { showHome,
 * showCategory } so settings.js can reset to home when the modal opens.
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const root = $("settings");
  if (!root) return;

  const home = $("cm-home");
  const back = $("cm-back");
  const title = $("cm-title");
  const body = root.querySelector(".cm-body");

  // Tile label ↔ app-bar title. Keep in sync with the tiles in index.html.
  const TITLES = {
    boat: "Boat & tuning",
    map: "Map & charts",
    display: "Display",
    feedback: "Sound & touch",
    safety: "Safety",
    fishing: "Fishing",
    devices: "Devices",
    data: "Data & system",
    sim: "Simulator",
  };

  let current = null; // active category id, or null on the home grid

  function panels() { return root.querySelectorAll(".cm-panel"); }

  function showHome() {
    current = null;
    if (home) home.classList.remove("hidden");
    panels().forEach((p) => p.classList.add("hidden"));
    if (back) back.classList.add("hidden");
    if (title) title.textContent = "Menu";
    if (body) body.scrollTop = 0;
  }

  function showCategory(cat) {
    const panel = root.querySelector('.cm-panel[data-cat="' + cat + '"]');
    if (!panel) return;
    current = cat;
    if (home) home.classList.add("hidden");
    panels().forEach((p) => p.classList.toggle("hidden", p !== panel));
    if (back) back.classList.remove("hidden");
    if (title) title.textContent = TITLES[cat] || "Menu";
    if (body) body.scrollTop = 0;
  }

  // Tile taps → open that category's panel.
  root.querySelectorAll(".cm-tile").forEach((tile) =>
    tile.addEventListener("click", () => showCategory(tile.dataset.cat))
  );

  // Back chevron → return to the tile grid.
  if (back) back.addEventListener("click", showHome);

  // Esc: step back to home if inside a panel; otherwise let settings.js close
  // the whole modal (reuse its ✕ handler so all close paths stay identical).
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (root.classList.contains("hidden")) return;
    if (current) { showHome(); e.stopPropagation(); }
    else { const c = $("settings-close"); if (c) c.click(); }
  });

  // ---- #cm-stop: compact STOP pill in the command menu header ---------------
  const cmStop = $("cm-stop");
  if (cmStop) {
    cmStop.addEventListener("click", () => {
      try { VA.sendCritical({ type: "stop" }); } catch (_) {}
      // Close the menu so the user can see the confirmation banner.
      const closeBtn = $("settings-close");
      if (closeBtn) closeBtn.click();
    });
    // Visible whenever a motor mode is active (VA.motorActive predicate in
    // appcore.js); hidden when idle-manual. Instant, no-confirm.
    VA.onTelemetry(function () {
      cmStop.classList.toggle("hidden", !VA.motorActive);
    });
  }

  // ---- Dock STOP + MOB wiring (desktop) ----
  const dockStop = $("dock-stop");
  if (dockStop) {
    dockStop.addEventListener("click", () => {
      try { VA.sendCritical({ type: "stop" }); } catch (_) {}
    });
  }
  const dockMob = $("dock-mob");
  if (dockMob) {
    if (VA.bindHold) {
      VA.bindHold(dockMob, 600, () => {
        try { VA.send({ type: "mob" }); } catch (_) {}
      });
    }
    dockMob.addEventListener("click", () => {
      if (VA.toast) VA.toast("Hold MAN OVERBOARD to engage", { ttl: 2000 });
    });
  }

  window.VA = window.VA || {};
  window.VA.menu = { showHome: showHome, showCategory: showCategory };
})();
