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
  const mq = window.matchMedia("(max-width: 760px), (max-height: 480px)");
  // Landscape sub-query: phone rotated sideways (height ≤ 480 px and landscape).
  // In landscape body.mobile.ls is toggled; all landscape CSS keys on that pair.
  const mqLand = window.matchMedia("(max-height: 480px) and (orientation: landscape)");
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
    const ls = on && mqLand.matches;  // landscape sub-mode
    if (on) ensureScrollWrap();
    body.classList.toggle("mobile", on);
    // body.ls gates all landscape CSS; cleared when portrait or desktop.
    body.classList.toggle("ls", ls);
    if (on) {
      if (!body.dataset.sheet) setSheet("peek");
      // Remeasure peek height (font/content may have changed) and publish as
      // CSS custom property so the sheet transform + FAB bottom calc are exact.
      invalidatePeekCache();
      document.body.style.setProperty("--peek-h", peekPx() + "px");
    }
    if (on !== was) refitMap();
  }

  // ===========================================================================
  // Bottom sheet: three snap states driven by body[data-sheet].
  // Heights (used for drag math + to mirror the CSS):
  //   peek = 76px, mid = 46vh, full = 88vh.
  // The CSS positions the sheet at full height and slides it down by
  // (fullPx - statePx); during a live drag we override translateY inline.
  // ===========================================================================
  const PEEK_FALLBACK = 152;   // fallback if measurement unavailable
  // Measure the actual sheet-head height (grip + instruments + peekbar) and
  // memoize until the next resize so layout-thrash is bounded to one rAF.
  let _peekPxCache = null;
  function peekPx() {
    if (_peekPxCache !== null) return _peekPxCache;
    const el = document.getElementById("sheet-head");
    if (el) {
      const h = Math.ceil(el.getBoundingClientRect().height) + 4;
      _peekPxCache = h > 60 ? h : PEEK_FALLBACK;
    } else {
      _peekPxCache = PEEK_FALLBACK;
    }
    return _peekPxCache;
  }
  function invalidatePeekCache() { _peekPxCache = null; }
  function vh(frac) { return window.innerHeight * frac; }
  function heights() {
    const full = vh(0.88), mid = vh(0.46), peek = peekPx();
    return { full, mid, peek };
  }
  // translateY (px, downward) for a given visible height.
  function offsetFor(h) { return heights().full - h; }
  const STATE_HEIGHT = { peek: peekPx, mid: () => vh(0.46), full: () => vh(0.88) };

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
      // Landscape: no vertical drag — grip tap-to-cycle only (handled in onEnd).
      if (body.classList.contains("ls")) { dragging = true; moved = false; return; }
      const y = e.touches ? e.touches[0].clientY : e.clientY;
      dragging = true; moved = false; startY = y; startH = currentHeight();
      dock.style.transition = "none";
    }
    function onMove(e) {
      if (!dragging) return;
      if (body.classList.contains("ls")) return;  // landscape: no drag
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
      // Landscape: only tap-to-cycle (moved is always false in ls).
      if (body.classList.contains("ls")) { cycleSheet(); return; }
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

  // ---- peek STOP + MOB + recenter FAB -------------------------------------
  const sheetStop = document.getElementById("sheet-stop");
  if (sheetStop) sheetStop.addEventListener("click", () => { try { VA.sendCritical({ type: "stop" }); } catch (_) {} });

  // MAN OVERBOARD — 600ms hold-to-engage; single tap shows a hint.
  const sheetMob = document.getElementById("sheet-mob");
  if (sheetMob) {
    if (VA.bindHold) {
      VA.bindHold(sheetMob, 600, () => {
        try { VA.send({ type: "mob" }); } catch (_) {}
      });
    }
    // Tap-without-hold hint.
    sheetMob.addEventListener("click", () => {
      if (VA.toast) VA.toast("Hold MAN OVERBOARD to engage", { ttl: 2000 });
    });
  }

  // Mode chip tap: open the sheet to mid.
  const sheetMode = document.getElementById("sheet-mode");
  if (sheetMode) {
    sheetMode.addEventListener("click", () => {
      ensureAtLeast("mid");
    });
  }

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
    // ---- ctx cell (SOG by default; DRIFT/ANCHOR when alarming/anchored) ----
    const ctxTile = document.getElementById("si-ctx");
    const ctxLabel = document.getElementById("si-ctx-label");
    const ctxUnit = document.getElementById("si-ctx-unit");
    const ctxSub = document.getElementById("si-ctx-sub");
    const sheetHead = document.getElementById("sheet-head");
    const aa = t && t.anchor_alarm;
    const sog = VA.fin(t.sog_knots);
    if (aa && aa.firing) {
      // Drag alarm — red DRAGGING cell (peek-echo of the alarm strip, item 21)
      if (ctxTile) ctxTile.dataset.ctx = "alarm";
      if (ctxLabel) ctxLabel.textContent = "DRAGGING";
      const dist = Number.isFinite(t.distance_to_anchor_m) ? Math.round(t.distance_to_anchor_m).toString() : "—";
      VA.setText("m-sog", dist);
      if (ctxUnit) ctxUnit.textContent = "m";
      const rm = Number.isFinite(aa.radius_m) ? Math.round(aa.radius_m) : "?";
      if (ctxSub) { ctxSub.textContent = "RING " + rm + " m"; ctxSub.classList.remove("hidden"); }
      if (sheetHead) sheetHead.classList.add("alarm");
    } else if (t.mode && (t.mode.startsWith("anchor") || (aa && aa.armed))) {
      // Anchor mode — "Anchor — holding · d m / r m" (item 21)
      if (ctxTile) ctxTile.dataset.ctx = "anchor";
      if (ctxLabel) ctxLabel.textContent = "ANCHOR";
      const dStr = Number.isFinite(t.distance_to_anchor_m) ? t.distance_to_anchor_m.toFixed(1) : "—";
      VA.setText("m-sog", dStr);
      if (ctxUnit) ctxUnit.textContent = "m";
      const rStr = Number.isFinite(t.anchor_radius_m) ? Math.round(t.anchor_radius_m) : null;
      if (ctxSub) {
        if (rStr !== null) { ctxSub.textContent = "holding · " + dStr + " m / " + rStr + " m"; ctxSub.classList.remove("hidden"); }
        else ctxSub.classList.add("hidden");
      }
      if (sheetHead) sheetHead.classList.remove("alarm");
    } else {
      // Default — SOG
      if (ctxTile) ctxTile.dataset.ctx = "sog";
      if (ctxLabel) ctxLabel.textContent = "SOG";
      VA.setText("m-sog", sog === null ? "—" : sog.toFixed(1));
      if (ctxUnit) ctxUnit.textContent = "kn";
      if (ctxSub) ctxSub.classList.add("hidden");
      if (sheetHead) sheetHead.classList.remove("alarm");
    }

    // ---- HDG (modulo 0-359, never "360") ----
    const hdg = VA.fin(t.heading_deg);
    VA.setText("m-hdg", hdg === null ? "—" : String(((Math.round(hdg) % 360) + 360) % 360));

    // ---- DEPTH ----
    const depth = VA.fin(t.depth_m);
    VA.setText("m-depth", depth === null ? "—" : depth.toFixed(1));

    // ---- BATT: % + voltage + level color ----
    const b = (t && t.battery) || null;
    const soc = b && Number.isFinite(b.soc_pct) ? b.soc_pct : null;
    VA.setText("m-batt", soc === null ? "—" : Math.round(soc).toString());
    const voltV = b && Number.isFinite(b.voltage_v) ? b.voltage_v : null;
    VA.setText("m-batt-volts", voltV === null ? "— V" : voltV.toFixed(1) + " V");
    const siBatt = document.getElementById("si-batt");
    if (siBatt) {
      const lvl = VA.battLevel ? VA.battLevel(soc) : "ok";
      siBatt.dataset.level = lvl;
    }

    // ---- Mode sentence (VA.modeSentence defined in appcore.js) ----
    if (t.mode !== undefined) {
      const sentence = VA.modeSentence ? VA.modeSentence(t) : (t.mode || "—");
      VA.setText("sheet-mode", sentence);
      const modeEl = document.getElementById("sheet-mode");
      const suffix = VA.modeSuffix ? VA.modeSuffix(t) : "";
      if (modeEl) modeEl.classList.toggle("stopped", suffix !== "");
    }
  });

  // ---- public: let other modules reveal the sheet ---------------------------
  // Selecting a mode should slide the sheet up so that mode's options come into
  // view, instead of forcing a manual drag (mobile only). Consumed by
  // appcore.js (mode rail) and guided.js (the "More" flyout).
  VA.sheet = {
    reveal(min) {
      if (!mq.matches) return;
      // In landscape the rail is a side panel; mid/full both = rail open. Use "mid".
      ensureAtLeast(body.classList.contains("ls") ? "mid" : (min || "mid"));
    },
    collapse() {
      if (!mq.matches) return;
      setSheet("peek");  // peek = hidden in landscape (collapsed rail), standard in portrait
    },
    active() { return mq.matches; },
  };

  // ---- boot -----------------------------------------------------------------
  applyMobile();
  // matchMedia change + resize + orientation all re-evaluate mobile state.
  if (mq.addEventListener) mq.addEventListener("change", applyMobile);
  else if (mq.addListener) mq.addListener(applyMobile);
  // Landscape sub-query also re-evaluates on rotation.
  if (mqLand.addEventListener) mqLand.addEventListener("change", applyMobile);
  else if (mqLand.addListener) mqLand.addListener(applyMobile);
  window.addEventListener("resize", () => { invalidatePeekCache(); applyMobile(); });
  window.addEventListener("orientationchange", () => setTimeout(() => { invalidatePeekCache(); applyMobile(); refitMap(); }, 60));
})();
