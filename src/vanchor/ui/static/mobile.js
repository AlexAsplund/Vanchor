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

  // ---- advanced details: close all .mini <details> in the dock on first
  // mobile entry so the options panels are compact on phones. Desktop keeps
  // them open (the <details> elements have the `open` attribute in HTML).
  function closeAdvancedDetails() {
    document.querySelectorAll(".dock-panels details.mini").forEach(function (d) {
      d.removeAttribute("open");
    });
  }

  // ---- mobile on/off --------------------------------------------------------
  function applyMobile() {
    const on = mq.matches;
    const was = body.classList.contains("mobile");
    // Landscape sub-mode: a short viewport in a wide (landscape) aspect. Derived
    // from the actual viewport dimensions rather than the CSS `orientation`
    // media feature, which some headless/embedded browsers evaluate
    // inconsistently (e.g. reporting portrait at an 844×390 viewport). Robust
    // and identical to the feature's definition (orientation:landscape ⇔ w≥h).
    const ls = on && window.innerHeight <= 480 && window.innerWidth > window.innerHeight;
    if (on) {
      ensureScrollWrap();
      // Fix 5: hudframe.js adds dock-collapsed on ≤760px viewports; that class
      // is irrelevant (and visually benign but semantically stale) in mobile
      // sheet mode, so clear it. Also clear panel-active on mobile init to start
      // with the rail visible (applyModePanels will set it when a mode fires).
      body.classList.remove("dock-collapsed");
    }
    body.classList.toggle("mobile", on);
    // body.ls gates all landscape CSS; cleared when portrait or desktop.
    body.classList.toggle("ls", ls);
    if (on) {
      if (!body.dataset.sheet) setSheet("peek");
      // Remeasure peek height (font/content may have changed) and publish as
      // CSS custom property so the sheet transform + FAB bottom calc are exact.
      invalidatePeekCache();
      document.body.style.setProperty("--peek-h", peekPx() + "px");
      // On first entry to mobile mode close all advanced/secondary <details>
      // so the primary CTA is immediately visible without scrolling.
      if (!was) closeAdvancedDetails();
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
  // Bottom edge of the fixed topbar (accounts for safe-area-inset-top on
  // notched phones, where the topbar is taller than a bare viewport).
  function topbarBottom() {
    const tb = document.querySelector("#topbar, .topbar");
    return tb ? Math.ceil(tb.getBoundingClientRect().bottom) : 0;
  }
  // FULL sheet height. Was a fixed 88vh, which on a notched device pushed the
  // sheet TOP (grip + collapse ⌄) up UNDER the taller topbar so they couldn't
  // be reached — the boat was stuck maximized. Clamp so the sheet top never
  // rises above the topbar's bottom (leave a 6px sliver), so the drag grip and
  // collapse control stay visible in every state.
  function fullH() {
    return Math.max(vh(0.5), window.innerHeight - topbarBottom() - 6);
  }
  function heights() {
    const full = fullH(), mid = vh(0.46), peek = peekPx();
    return { full, mid, peek };
  }
  // translateY (px, downward) for a given visible height.
  function offsetFor(h) { return heights().full - h; }
  const STATE_HEIGHT = { peek: peekPx, mid: () => vh(0.46), full: fullH };

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
    grip.addEventListener("mousedown", (e) => {
      onStart(e);
      const mm = (ev) => onMove(ev);
      const mu = () => { onEnd(); window.removeEventListener("mousemove", mm); window.removeEventListener("mouseup", mu); };
      window.addEventListener("mousemove", mm);
      window.addEventListener("mouseup", mu);
    });

    // Also drag from the ALWAYS-VISIBLE sticky head, so the natural "swipe the
    // sheet down" gesture works from the visible band (the grip can be a thin
    // target and, before the height clamp, was hidden under a notched topbar).
    // STOP and MAN OVERBOARD are excluded so a safety press is never turned
    // into a drag; the mode text and empty head areas ARE drag handles. A real
    // drag (moved) collapses and swallows the trailing click so it can't also
    // fire "change mode"; a plain tap falls through to the element's own click.
    const head = document.getElementById("sheet-head");
    if (head) {
      const safeTarget = (e) => !(e.target.closest &&
        e.target.closest("#sheet-stop, #sheet-mob"));
      let headMoved = false;
      const hStart = (e) => { if (!safeTarget(e)) return; headMoved = false; onStart(e); };
      const hMove = (e) => { if (!dragging) return; if (Math.abs((e.touches ? e.touches[0].clientY : e.clientY) - startY) > 4) headMoved = true; onMove(e); };
      head.addEventListener("touchstart", hStart, { passive: true });
      head.addEventListener("touchmove", hMove, { passive: false });
      head.addEventListener("touchend", (e) => { onEnd(); }, false);
      head.addEventListener("touchcancel", onEnd);
      head.addEventListener("mousedown", (e) => {
        if (!safeTarget(e)) return;
        headMoved = false; onStart(e);
        const mm = (ev) => { if (Math.abs(ev.clientY - startY) > 4) headMoved = true; onMove(ev); };
        const mu = () => { onEnd(); window.removeEventListener("mousemove", mm); window.removeEventListener("mouseup", mu); };
        window.addEventListener("mousemove", mm);
        window.addEventListener("mouseup", mu);
      });
      // Swallow the click that follows a real head-drag so it can't also
      // trigger the mode-chip's change-mode handler.
      head.addEventListener("click", (e) => {
        if (headMoved) { headMoved = false; e.preventDefault(); e.stopImmediatePropagation(); }
      }, true);
    }
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

  // Mode chip tap: change-mode affordance (portrait) or expand (landscape/peek).
  // When a mode panel is already active on portrait mobile, tapping the chip
  // reveals the mode-rail so the user can pick a different mode — it temporarily
  // clears panel-active and scrolls the rail into view. Any mode-rail tap will
  // re-set panel-active via applyModePanels(). In all other cases, expand sheet.
  const sheetMode = document.getElementById("sheet-mode");
  if (sheetMode) {
    sheetMode.addEventListener("click", () => {
      if (body.classList.contains("panel-active") &&
          !body.classList.contains("ls")) {
        // Show the rail so the user can switch modes.
        body.classList.remove("panel-active");
        const scrollEl = document.querySelector(".dock-scroll");
        if (scrollEl) scrollEl.scrollTop = 0;
      }
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
      // F3 fix: in landscape the #map-pills (SIM pill) is hidden to prevent
      // occlusion of the .sheet-instruments numerals.  Append a compact "· SIM"
      // tag to the safety-band mode text so a fisherman can still tell it's a
      // simulated boat even when rotated sideways.
      const simTag = (t.sim_enabled && document.body.classList.contains("ls")) ? " · SIM" : "";
      VA.setText("sheet-mode", sentence + simTag);
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
  // Re-evaluate once the viewport has settled.  applyMobile() above runs at
  // script-execution time; if it fires before the layout viewport is final
  // (observed under headless Chromium and possible on slow first paints) it
  // reads a stale width and leaves body.mobile unset — the whole mobile layout
  // then depends on a later resize that may never come.  A rAF and the load
  // event both re-run it against the settled viewport; both are idempotent.
  requestAnimationFrame(applyMobile);
  window.addEventListener("load", applyMobile);
  // A viewport that settles to its final size *after* first paint without
  // firing a 'resize' (observed under headless Chromium, where the emulated
  // size lands late) would otherwise leave body.mobile stuck on whatever the
  // transient early width implied.  Observe the root element's box so
  // applyMobile re-runs the moment the layout viewport actually changes.
  if (window.ResizeObserver) {
    new ResizeObserver(applyMobile).observe(document.documentElement);
  }
  // matchMedia change + resize + orientation all re-evaluate mobile state.
  if (mq.addEventListener) mq.addEventListener("change", applyMobile);
  else if (mq.addListener) mq.addListener(applyMobile);
  // Landscape sub-query also re-evaluates on rotation.
  if (mqLand.addEventListener) mqLand.addEventListener("change", applyMobile);
  else if (mqLand.addListener) mqLand.addListener(applyMobile);
  window.addEventListener("resize", () => { invalidatePeekCache(); applyMobile(); });
  window.addEventListener("orientationchange", () => setTimeout(() => { invalidatePeekCache(); applyMobile(); refitMap(); }, 60));
})();
