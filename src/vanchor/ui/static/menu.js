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

  window.VA = window.VA || {};
  window.VA.menu = { showHome: showHome, showCategory: showCategory };
})();
