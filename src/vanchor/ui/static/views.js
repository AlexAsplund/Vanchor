/* Vanchor-NG — specialised, URL-addressable, customisable views (spec §3).
 *
 * A "view" is a PRESET ARRANGEMENT of the same live widgets (#hud, #mode-rail,
 * the ctx-manual thrust/steer controls, STOP, the top status bar) — some views
 * drop the chart. Views are composed purely via CSS keyed on
 * `body[data-view="<name>"]` (see style.css); this module owns:
 *   - URL routing: read `location.pathname` (`/view/<name>`, default chart for
 *     `/`), set `document.body.dataset.view`, pushState on switch, popstate back.
 *   - the view switcher (topbar chips + the Display > Views card seg control),
 *   - per-view HUD-widget visibility (`.vw-off` on the shared #hud widgets),
 *   - the view overlay dock buttons (Anchor here / RTL / MOB / STOP / back),
 *   - lightweight persistence of the last view + per-view widget toggles via the
 *     prefs KV (`GET`/`PUT /api/prefs`, key `views`) with a localStorage cache.
 *
 * A small VIEW REGISTRY keeps adding views/widgets cheap. IIFE, no build step.
 */
"use strict";

(function () {
  const VA = (window.VA = window.VA || {});
  const $ = (id) => document.getElementById(id);
  const body = document.body;

  // ---- view registry ------------------------------------------------------
  // label/icon drive the UI chrome; `widgets` (when present) declares which HUD
  // widgets this view can toggle + their defaults. Add a view here + a matching
  // `body[data-view=...]` CSS block and it "just works".
  const HUD_WIDGETS = ["speed", "heading", "depth", "anchor", "battery"];
  // icon fields removed: view-chips now display text labels, not emoji (WP10 item 32).
  const VIEWS = {
    chart:       { label: "Chart" },
    helm:        { label: "Helm",
                   widgets: { speed: true, heading: true, depth: true, anchor: false, battery: false } },
    instruments: { label: "Gauges",
                   widgets: { speed: true, heading: true, depth: true, anchor: true, battery: true } },
    manual:      { label: "Manual" },
  };
  const DEFAULT_VIEW = "chart";
  const isView = (n) => Object.prototype.hasOwnProperty.call(VIEWS, n);

  // ---- persisted state ----------------------------------------------------
  // { view: "<last used>", widgets: { instruments: {...}, helm: {...} } }
  const LS_KEY = "vanchor-views";
  const state = { view: DEFAULT_VIEW, widgets: {} };
  Object.keys(VIEWS).forEach((v) => {
    if (VIEWS[v].widgets) state.widgets[v] = Object.assign({}, VIEWS[v].widgets);
  });

  function mergeState(o) {
    if (!o || typeof o !== "object") return;
    if (typeof o.view === "string" && isView(o.view)) state.view = o.view;
    if (o.widgets && typeof o.widgets === "object") {
      Object.keys(state.widgets).forEach((v) => {
        const src = o.widgets[v];
        if (src && typeof src === "object") {
          HUD_WIDGETS.forEach((w) => { if (typeof src[w] === "boolean") state.widgets[v][w] = src[w]; });
        }
      });
    }
  }

  function loadLocal() {
    try { mergeState(JSON.parse(localStorage.getItem(LS_KEY) || "null")); } catch (e) { /* ignore */ }
  }
  function saveLocal() {
    try { localStorage.setItem(LS_KEY, JSON.stringify(state)); } catch (e) { /* ignore */ }
  }
  // Push the WHOLE `views` object (prefs.merge is a SHALLOW merge, so a partial
  // patch would drop sibling keys). Best-effort — the localStorage cache is the
  // instant-paint source; the server copy is the durable/cross-device one.
  function pushServer() {
    try {
      fetch("/api/prefs", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ views: state }),
      }).catch(() => {});
    } catch (e) { /* ignore */ }
  }
  function pullServer() {
    try {
      fetch("/api/prefs").then((r) => (r.ok ? r.json() : null)).then((p) => {
        if (p && p.views) {
          mergeState(p.views);
          syncToggles();
          applyWidgets();
        }
      }).catch(() => {});
    } catch (e) { /* ignore */ }
  }

  // ---- per-view HUD widget visibility -------------------------------------
  // `.vw-off` hides a shared #hud widget for the ACTIVE view only; the CSS for
  // instruments/helm forces widgets visible (overriding the global HUD prefs)
  // except those carrying `.vw-off`. Chart/manual clear it entirely so the
  // normal HUD-overlay prefs (settings.js) resume ownership.
  function applyWidgets() {
    const view = body.dataset.view;
    const cfg = VIEWS[view] && VIEWS[view].widgets ? state.widgets[view] : null;
    HUD_WIDGETS.forEach((w) => {
      const el = document.querySelector('.hud-widget[data-hud="' + w + '"]');
      if (el) el.classList.toggle("vw-off", !!(cfg && cfg[w] === false));
    });
  }

  // Reflect state into the Display > Views card checkboxes.
  function syncToggles() {
    document.querySelectorAll(".vw-toggle").forEach((cb) => {
      const cfg = state.widgets[cb.dataset.view];
      if (cfg && cb.dataset.widget in cfg) cb.checked = !!cfg[cb.dataset.widget];
    });
  }

  // ---- switcher chrome ----------------------------------------------------
  function highlightChrome(view) {
    document.querySelectorAll("#view-switcher .view-chip").forEach((b) =>
      b.classList.toggle("active", b.dataset.view === view));
    document.querySelectorAll("#views-seg button").forEach((b) =>
      b.classList.toggle("on", b.dataset.view === view));
    const badge = $("views-current");
    if (badge) badge.textContent = (VIEWS[view] && VIEWS[view].label) || view;
  }

  // ---- apply / switch -----------------------------------------------------
  function applyView(name, opts) {
    opts = opts || {};
    if (!isView(name)) name = DEFAULT_VIEW;
    body.dataset.view = name;
    state.view = name;
    highlightChrome(name);
    applyWidgets();
    // The chart's Leaflet map is display:none in other views; give it a size
    // recompute when it comes back so it doesn't paint as grey tiles.
    if (name === "chart") {
      setTimeout(() => {
        try { VA.map && VA.map.leaflet && VA.map.leaflet.invalidateSize(); } catch (e) { /* ignore */ }
      }, 60);
    }
    if (opts.push) {
      try { history.pushState({ view: name }, "", "/view/" + name); } catch (e) { /* ignore */ }
    }
    if (opts.persist !== false) { saveLocal(); if (opts.push) pushServer(); }
  }

  function switchTo(name) {
    if (!isView(name) || name === body.dataset.view) {
      if (name === body.dataset.view) return;
    }
    applyView(name, { push: true });
  }

  // pathname -> view name, or null for the bare root "/".
  function pathView() {
    const m = /^\/view\/([^/?#]+)/.exec(location.pathname || "");
    if (m) return decodeURIComponent(m[1]);
    return null;  // "/" — fall back to the persisted last view
  }

  // ---- wiring -------------------------------------------------------------
  // Topbar chips + the Display > Views card seg both switch the view.
  document.querySelectorAll("#view-switcher .view-chip, #views-seg button").forEach((b) =>
    b.addEventListener("click", () => switchTo(b.dataset.view)));

  // Per-view HUD-widget toggles (customisation).
  document.querySelectorAll(".vw-toggle").forEach((cb) =>
    cb.addEventListener("change", () => {
      const cfg = state.widgets[cb.dataset.view];
      if (!cfg) return;
      cfg[cb.dataset.widget] = cb.checked;
      saveLocal();
      pushServer();
      if (body.dataset.view === cb.dataset.view) applyWidgets();
    }));

  // View overlay dock. STOP uses the safety-critical dual-path (WS + POST);
  // quick actions reuse the existing command types. Back returns to the chart.
  const bind = (id, fn) => { const el = $(id); if (el) el.addEventListener("click", fn); };
  const send = (c) => { try { VA.send && VA.send(c); } catch (e) { /* ignore */ } };
  bind("view-stop", () => { try { VA.sendCritical ? VA.sendCritical({ type: "stop" }) : send({ type: "stop" }); } catch (e) { send({ type: "stop" }); } });
  bind("view-anchor", () => send({ type: "anchor_hold", radius_m: 5 }));
  bind("view-rtl", () => send({ type: "return_to_launch" }));
  bind("view-mob", () => send({ type: "mob" }));
  bind("view-exit", () => switchTo("chart"));

  // Back/forward buttons re-apply the view from the URL (no new history entry).
  window.addEventListener("popstate", () => {
    applyView(pathView() || state.view || DEFAULT_VIEW, { push: false });
  });

  // ---- boot ---------------------------------------------------------------
  loadLocal();
  syncToggles();
  // The URL wins when it names a view; a bare "/" falls back to the persisted
  // last-used view (default chart). Deep links (/view/helm) are unaffected.
  const initial = pathView() || state.view || DEFAULT_VIEW;
  applyView(initial, { push: false });
  pullServer();  // adopt the durable server copy (may refine widget prefs)

  // Small public surface so more views/widgets could be driven programmatically.
  VA.views = {
    registry: VIEWS,
    current: () => body.dataset.view,
    set: switchTo,
  };

  // ---- "tap the map" actions force the chart into view ----------------------
  // Marker drop, Add-waypoints, Go-to, Orbit centre, Work-area spots, GPS-
  // position adjust, offline-area select and Teleport all need the user to tap
  // the leaflet map. If they're triggered from a non-chart view (helm/manual/
  // instruments), from an open menu, or with the mobile sheet up, the map is
  // hidden — so show the chart, close the menu, and drop the sheet first. A
  // capture-phase listener runs BEFORE each module's own arm handler, so the arm
  // state is set with the map already visible.
  const MAP_TAP_BUTTONS = new Set([
    "marker-fab", "wp-arm", "orbit-pick", "wa-arm", "goto-arm",
    "gpscal-adjust", "offline-pick", "teleport-pick",
  ]);
  VA.showChart = function () {
    if (body.dataset.view !== "chart") switchTo("chart");
    const menu = document.getElementById("settings");
    if (menu && !menu.classList.contains("hidden")) {
      const close = document.getElementById("settings-close");
      if (close) close.click(); else menu.classList.add("hidden");
    }
    if (VA.sheet && VA.sheet.collapse) VA.sheet.collapse();
  };
  document.addEventListener("click", (e) => {
    const btn = e.target && e.target.closest && e.target.closest("button");
    if (btn && MAP_TAP_BUTTONS.has(btn.id)) VA.showChart();
  }, true);
})();
