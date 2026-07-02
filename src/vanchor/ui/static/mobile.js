/* Vanchor-NG — mobile mode.
 *
 * Turns the desktop layout into a phone navigation app: full-bleed map, a slim
 * top bar, a compact instrument strip, and a draggable three-state bottom
 * sheet built from the existing control dock. Everything visual lives in CSS
 * under `body.mobile` / `@media (max-width: 760px)`; this file only:
 *   - toggles `body.mobile` on load + resize/orientationchange,
 *   - drives the sheet snap state (peek / mid / full) via `body[data-sheet]`,
 *   - handles grip drag + tap-to-cycle without fighting the map or inner scroll,
 *   - mirrors the HUD's key numbers into the always-visible instrument strip,
 *   - wires the recenter/follow FAB,
 *   - calls VA.map.leaflet.invalidateSize() after every layout change.
 *
 * Desktop is untouched: when `body.mobile` is absent every rule here no-ops and
 * the CSS additions are inert.
 */
"use strict";

(function () {
  const mq = window.matchMedia("(max-width: 760px)");
  const body = document.body;

  // ---- map re-fit (debounced to the next frame so CSS has settled) ----------
  let rafPending = false;
  function refitMap() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      try { VA.map && VA.map.leaflet && VA.map.leaflet.invalidateSize(); } catch (_) {}
    });
  }

  // ---- scroll wrapper -------------------------------------------------------
  // The bottom sheet keeps the peek header fixed while the rest of the dock
  // scrolls. The markup has those as plain siblings, so on first entry to
  // mobile we move every child after the head into a single `.dock-scroll`
  // wrapper. We never unwrap (the wrapper is inert + harmless on desktop, and
  // re-flowing the DOM on each resize would be wasteful and risk losing JS
  // listeners) — desktop CSS simply ignores `.dock-scroll`.
  function ensureScrollWrap() {
    const d = document.getElementById("dock");
    if (!d || d.querySelector(":scope > .dock-scroll")) return;
    const head = document.getElementById("sheet-head");
    const wrap = document.createElement("div");
    wrap.className = "dock-scroll";
    const kids = Array.from(d.children).filter(
      (c) => c !== head && c.id !== "dock-handle"
    );
    kids.forEach((c) => wrap.appendChild(c));
    d.appendChild(wrap);
  }

  // ---- mobile on/off --------------------------------------------------------
  function applyMobile() {
    const on = mq.matches;
    const was = body.classList.contains("mobile");
    if (on) ensureScrollWrap();
    body.classList.toggle("mobile", on);
    if (on && !body.dataset.sheet) setSheet("peek");
    if (on !== was) refitMap();
  }

  // ===========================================================================
  // Bottom sheet: three snap states driven by body[data-sheet].
  // Heights (used for drag math + to mirror the CSS):
  //   peek = 76px, mid = 46vh, full = 88vh.
  // The CSS positions the sheet at full height and slides it down by
  // (fullPx - statePx); during a live drag we override translateY inline.
  // ===========================================================================
  const PEEK_PX = 76;
  function vh(frac) { return window.innerHeight * frac; }
  function heights() {
    const full = vh(0.88), mid = vh(0.46), peek = PEEK_PX;
    return { full, mid, peek };
  }
  // translateY (px, downward) for a given visible height.
  function offsetFor(h) { return heights().full - h; }
  const STATE_HEIGHT = { peek: () => PEEK_PX, mid: () => vh(0.46), full: () => vh(0.88) };

  const dock = document.getElementById("dock");

  function setSheet(state) {
    if (!STATE_HEIGHT[state]) state = "peek";
    body.dataset.sheet = state;
    if (dock) dock.style.transform = "";   // clear any live-drag inline offset
    refitMap();
  }

  function cycleSheet() {
    const order = ["peek", "mid", "full"];
    const i = order.indexOf(body.dataset.sheet || "peek");
    setSheet(order[(i + 1) % order.length]);
  }

  // Ensure the sheet is at least `min` tall (peek < mid < full).
  function ensureAtLeast(min) {
    const rank = { peek: 0, mid: 1, full: 2 };
    const cur = body.dataset.sheet || "peek";
    if (rank[cur] < rank[min]) setSheet(min);
  }

  // ---- grip drag + tap ------------------------------------------------------
  const grip = document.getElementById("sheet-grip");
  if (grip && dock) {
    let dragging = false, startY = 0, startH = 0, moved = false;

    const currentHeight = () => STATE_HEIGHT[body.dataset.sheet || "peek"]();

    function onStart(e) {
      if (!body.classList.contains("mobile")) return;
      const y = e.touches ? e.touches[0].clientY : e.clientY;
      dragging = true; moved = false; startY = y; startH = currentHeight();
      dock.style.transition = "none";
    }
    function onMove(e) {
      if (!dragging) return;
      const y = e.touches ? e.touches[0].clientY : e.clientY;
      const dy = y - startY;               // down = positive
      if (Math.abs(dy) > 4) moved = true;
      const { full, peek } = heights();
      // New visible height = start height minus the downward drag.
      let h = startH - dy;
      h = Math.max(peek, Math.min(full, h));
      dock.style.transform = `translateY(${offsetFor(h)}px)`;
      if (e.cancelable) e.preventDefault();   // we own this gesture
    }
    function onEnd() {
      if (!dragging) return;
      dragging = false;
      dock.style.transition = "";
      if (!moved) { cycleSheet(); return; }   // a tap, not a drag
      // Snap to the nearest of peek / mid / full by current height.
      const { full, mid, peek } = heights();
      const m = dock.style.transform.match(/translateY\(([-\d.]+)px\)/);
      const off = m ? parseFloat(m[1]) : offsetFor(currentHeight());
      const h = full - off;
      const dPeek = Math.abs(h - peek), dMid = Math.abs(h - mid), dFull = Math.abs(h - full);
      const nearest = dPeek <= dMid && dPeek <= dFull ? "peek"
                    : dMid <= dFull ? "mid" : "full";
      setSheet(nearest);
    }

    grip.addEventListener("touchstart", onStart, { passive: true });
    grip.addEventListener("touchmove", onMove, { passive: false });
    grip.addEventListener("touchend", onEnd);
    grip.addEventListener("touchcancel", onEnd);
    // Mouse fallback (desktop testing at narrow widths).
    grip.addEventListener("mousedown", (e) => {
      onStart(e);
      const mm = (ev) => onMove(ev);
      const mu = () => { onEnd(); window.removeEventListener("mousemove", mm); window.removeEventListener("mouseup", mu); };
      window.addEventListener("mousemove", mm);
      window.addEventListener("mouseup", mu);
    });
  }

  // ---- mode buttons: switching a mode reveals its panel ---------------------
  // Tapping a mode (or a "More" guided mode) should open the sheet to at least
  // mid so the panel is visible immediately. Listeners are added in capture so
  // they run alongside app.js's own click handlers regardless of order.
  document.querySelectorAll(".mode-btn[data-mode], .more-item[data-mode]").forEach((b) => {
    b.addEventListener("click", () => {
      if (!body.classList.contains("mobile")) return;
      // "stop" / "remote" don't need the panel opened in the user's face.
      if (b.dataset.mode === "stop") return;
      ensureAtLeast("mid");
    });
  });

  // ---- peek STOP + recenter FAB --------------------------------------------
  const sheetStop = document.getElementById("sheet-stop");
  if (sheetStop) sheetStop.addEventListener("click", () => { try { VA.sendCritical({ type: "stop" }); } catch (_) {} });

  const followFab = document.getElementById("follow-fab");
  if (followFab) followFab.addEventListener("click", () => {
    try { VA.map && VA.map.recenter && VA.map.recenter(); } catch (_) {}
  });

  // ---- "back to map": collapse the sheet -----------------------------------
  // Once the sheet is up (especially scrolled into a tall panel like Route),
  // there was no obvious way back to the map. Fixes:
  //   1. An explicit "⌄ Map" button in the header collapses to peek.
  //   2. Touching the map itself (pan or tap) dismisses the sheet to peek, the
  //      way every navigation app does it.
  function collapseSheet() {
    if (body.classList.contains("mobile") && (body.dataset.sheet || "peek") !== "peek") {
      setSheet("peek");
    }
  }
  const collapseBtn = document.getElementById("sheet-collapse");
  if (collapseBtn) collapseBtn.addEventListener("click", collapseSheet);
  try {
    const lf = VA.map && VA.map.leaflet;
    if (lf && lf.on) lf.on("dragstart click", collapseSheet);
  } catch (_) {}

  // ---- instrument strip mirror ---------------------------------------------
  // Reuses the same telemetry the HUD consumes; small mirror elements so we
  // never fight hud.js for the canonical #hud-* nodes.
  VA.onTelemetry(function mirrorInstruments(t) {
    const sog = VA.fin(t.sog_knots);
    VA.setText("m-sog", sog === null ? "—" : sog.toFixed(1));
    const hdg = VA.fin(t.heading_deg);
    VA.setText("m-hdg", hdg === null ? "—" : Math.round(hdg).toString());
    const depth = VA.fin(t.depth_m);
    VA.setText("m-depth", depth === null ? "—" : depth.toFixed(1));
    const b = (t && t.battery) || null;
    const soc = b && Number.isFinite(b.soc_pct) ? b.soc_pct : null;
    VA.setText("m-batt", soc === null ? "—" : Math.round(soc).toString());
    if (t.mode) VA.setText("sheet-mode", prettyMode(t.mode));
  });

  function prettyMode(m) {
    const names = {
      manual: "Manual", anchor_hold: "Anchor", anchor_ml: "Anchor (Smart)", heading_hold: "Heading",
      waypoint: "Route", follow_apb: "Follow APB", drift: "Drift",
      stop: "Stopped", remote: "Remote", contour_follow: "Contour",
      orbit: "Orbit", trolling: "Trolling",
    };
    return names[m] || (m ? m.replace(/_/g, " ") : "—");
  }

  // ---- public: let other modules reveal the sheet ---------------------------
  // Selecting a mode should slide the sheet up so that mode's options come into
  // view, instead of forcing a manual drag (mobile only). Consumed by
  // appcore.js (mode rail) and guided.js (the "More" flyout).
  VA.sheet = {
    reveal(min) { if (mq.matches) ensureAtLeast(min || "mid"); },
    collapse() { if (mq.matches) setSheet("peek"); },  // drop the sheet to reveal the map
    active() { return mq.matches; },
  };

  // ---- boot -----------------------------------------------------------------
  applyMobile();
  // matchMedia change + resize + orientation all re-evaluate mobile state.
  if (mq.addEventListener) mq.addEventListener("change", applyMobile);
  else if (mq.addListener) mq.addListener(applyMobile);
  window.addEventListener("resize", applyMobile);
  window.addEventListener("orientationchange", () => setTimeout(() => { applyMobile(); refitMap(); }, 60));
})();
