/* Vanchor-NG — map orientation: north-up (default) or HEADING-UP.
 *
 * Heading-up rotates the chart so "up" is where the bow points, like a car
 * navigator. Implemented natively (no third-party rotation plugin — the only
 * maintained one is GPL-licensed, which this MIT project can't vendor):
 *
 *   - #map is wrapped in a clipping viewport; entering heading-up OVERSIZES
 *     the map square to the viewport diagonal (corners never show) and
 *     CSS-rotates it. North-up restores the plain full-bleed layout, so the
 *     default mode costs nothing.
 *   - Two seams are patched while rotated:
 *       1. map.mouseEventToContainerPoint — un-rotates pointer coordinates
 *          around the container centre, which fixes click/tap/long-press,
 *          wheel-zoom-to-cursor, pinch and box zoom in one place (every
 *          Leaflet handler funnels through this method).
 *       2. L.Draggable._onMove — de-rotates drag DELTAS for elements inside
 *          the rotated map (map panning and marker drags), by feeding the
 *          original handler a shim event with un-rotated client coords.
 *   - Markers/tooltips/menus stay UPRIGHT via a CSS counter-rotation variable
 *     (--map-derot) — except the boat icon, which must rotate with the chart
 *     (so in heading-up it naturally points up). Leaflet's control container
 *     is reparented into the viewport so zoom/layers/attribution stay put.
 *
 * STABILIZATION — a jittery compass must not wobble the whole chart:
 *   1. the heading is low-passed (exponential, shortest-path, tau ~1.2 s),
 *   2. a deadband with hysteresis: the chart only starts chasing when the
 *      smoothed heading is >4° away from what's shown, and chases until
 *      within 0.5°, so at-rest jitter rotates nothing,
 *   3. rotation is slew-limited (~60°/s) and eased, so corrections glide.
 *
 * The compass toggle button (top-left, under zoom) switches modes; its
 * needle always points at map-north. Persisted in localStorage.
 */
"use strict";

(function () {
  const VA = window.VA;
  if (!VA || !VA.mapCtx || !VA.mapCtx.map || !window.L) return;
  const L = window.L;
  const map = VA.mapCtx.map;
  const mapEl = document.getElementById("map");
  if (!mapEl || !mapEl.parentNode) return;

  const KEY = "vanchor-map-orient";
  let mode = "north";                       // "north" | "head"
  try { if (localStorage.getItem(KEY) === "head") mode = "head"; } catch (e) { /* ignore */ }

  const wrap180 = (d) => ((d + 180) % 360 + 360) % 360 - 180;

  // ---- viewport wrapper -----------------------------------------------------
  // #map-viewport takes #map's place (full-bleed, clips); #map becomes its
  // child. In north-up #map keeps inset:0; in heading-up it becomes an
  // oversized centred square so the rotated chart always covers the screen.
  const viewport = document.createElement("div");
  viewport.id = "map-viewport";
  mapEl.parentNode.insertBefore(viewport, mapEl);
  viewport.appendChild(mapEl);
  // Controls (zoom/layers/measure/attribution) must not rotate or ride the
  // oversized square: reparent Leaflet's control container into the viewport.
  if (map._controlContainer) viewport.appendChild(map._controlContainer);

  let bearing = 0;          // rotation applied to the chart (deg, CSS clockwise)

  function applyLayout() {
    if (mode === "head") {
      const w = viewport.clientWidth, h = viewport.clientHeight;
      const d = Math.ceil(Math.hypot(w, h));
      mapEl.style.inset = "auto";
      mapEl.style.width = d + "px";
      mapEl.style.height = d + "px";
      mapEl.style.left = Math.round((w - d) / 2) + "px";
      mapEl.style.top = Math.round((h - d) / 2) + "px";
    } else {
      mapEl.style.width = ""; mapEl.style.height = "";
      mapEl.style.left = ""; mapEl.style.top = ""; mapEl.style.inset = "0";
    }
    map.invalidateSize({ pan: false });
  }
  window.addEventListener("resize", () => { if (mode === "head") applyLayout(); });

  function applyRotation(b) {
    bearing = b;
    mapEl.style.transform = b ? `rotate(${b}deg)` : "";
    mapEl.style.setProperty("--map-derot", `${-b}deg`);
    mapEl.classList.toggle("map-rotated", !!b);
    if (btnNeedle) btnNeedle.style.transform = `rotate(${b}deg)`;
  }

  // ---- seam 1: pointer -> container point (un-rotate around the centre) -----
  const origM2C = map.mouseEventToContainerPoint.bind(map);
  map.mouseEventToContainerPoint = function (e) {
    if (!bearing) return origM2C(e);
    const rect = mapEl.getBoundingClientRect();          // AABB of the rotated square
    const cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2;
    const rad = -bearing * Math.PI / 180;
    const dx = e.clientX - cx, dy = e.clientY - cy;
    return new L.Point(
      dx * Math.cos(rad) - dy * Math.sin(rad) + mapEl.clientWidth / 2,
      dx * Math.sin(rad) + dy * Math.cos(rad) + mapEl.clientHeight / 2
    );
  };

  // ---- seam 2: drag deltas (map pan + marker drags) --------------------------
  // Feed the original handler a shim event whose client coords are the drag
  // START plus the de-rotated delta, so dragging feels screen-true.
  const origOnMove = L.Draggable.prototype._onMove;
  L.Draggable.prototype._onMove = function (e) {
    if (!bearing || !this._element || !mapEl.contains(this._element) ||
        (e.touches && e.touches.length > 1) || !this._startPoint) {
      return origOnMove.call(this, e);
    }
    const src = (e.touches && e.touches.length === 1) ? e.touches[0] : e;
    const rad = -bearing * Math.PI / 180;
    const dx = src.clientX - this._startPoint.x, dy = src.clientY - this._startPoint.y;
    const rx = dx * Math.cos(rad) - dy * Math.sin(rad);
    const ry = dx * Math.sin(rad) + dy * Math.cos(rad);
    const cx = this._startPoint.x + rx, cy = this._startPoint.y + ry;
    const pt = { clientX: cx, clientY: cy };
    const shim = {
      type: e.type, target: e.target,
      clientX: cx, clientY: cy,
      touches: e.touches ? [pt] : undefined,
      changedTouches: e.changedTouches ? [pt] : undefined,
      preventDefault: () => { try { e.preventDefault(); } catch (err) { /* passive */ } },
      stopPropagation: () => { try { e.stopPropagation(); } catch (err) { /* ignore */ } },
    };
    return origOnMove.call(this, shim);
  };

  // ---- stabilized rotation loop ----------------------------------------------
  let smooth = null;        // low-passed heading (deg)
  let chasing = false;
  let lastTs = null;
  VA.onTelemetry((t) => {
    if (!t || !Number.isFinite(t.heading_deg)) return;
    if (smooth === null) { smooth = t.heading_deg; return; }
    // 5 Hz frames, tau ~1.2 s: alpha = dt/(tau+dt)
    smooth += 0.143 * wrap180(t.heading_deg - smooth);
  });

  function tick(ts) {
    const dt = lastTs === null ? 0.016 : Math.min(0.1, (ts - lastTs) / 1000);
    lastTs = ts;
    const target = (mode === "head" && smooth !== null) ? wrap180(-smooth) : 0;
    const err = wrap180(target - bearing);
    // Hysteresis: start chasing past 4° (heading-up), always chase back to 0
    // in north-up; stop once within half a degree.
    if (!chasing && Math.abs(err) > (mode === "head" ? 4 : 0.2)) chasing = true;
    if (chasing) {
      if (Math.abs(err) < 0.5) {
        chasing = false;
        if (mode === "north") applyRotation(0);   // land exactly on north-up
      } else {
        // Proportional ease (tau ~0.4 s) capped at 60°/s.
        const step = Math.max(-60 * dt, Math.min(60 * dt, err * 2.5 * dt));
        applyRotation(wrap180(bearing + (Math.abs(step) < 0.02 * dt ? err : step)));
      }
    }
    requestAnimationFrame(tick);
  }

  // ---- mode switch + control button ------------------------------------------
  let btnNeedle = null;
  function setMode(m, persist) {
    mode = m === "head" ? "head" : "north";
    applyLayout();
    chasing = true;                     // animate toward the new target
    if (btn) {
      btn.classList.toggle("on", mode === "head");
      btn.title = mode === "head" ? "Heading-up (tap for north-up)" : "North-up (tap for heading-up)";
      btn.setAttribute("aria-pressed", mode === "head" ? "true" : "false");
    }
    if (persist) { try { localStorage.setItem(KEY, mode); } catch (e) { /* ignore */ } }
  }

  let btn = null;
  const Ctl = L.Control.extend({
    options: { position: "topleft" },
    onAdd() {
      const wrap = L.DomUtil.create("div", "leaflet-bar maprot-ctl");
      btn = L.DomUtil.create("a", "maprot-btn", wrap);
      btn.href = "#";
      btn.setAttribute("role", "button");
      btn.setAttribute("aria-label", "Map orientation: north-up / heading-up");
      btn.innerHTML =
        `<svg viewBox="0 0 24 24" width="18" height="18" class="maprot-needle" aria-hidden="true">` +
        `<path d="M12 2 L15.2 12 L12 10.4 L8.8 12 Z" fill="#ff5d6e"/>` +
        `<path d="M12 22 L8.8 12 L12 13.6 L15.2 12 Z" fill="#dfeaf2"/>` +
        `<circle cx="12" cy="12" r="1.6" fill="#1be4ff"/></svg>`;
      btnNeedle = btn.querySelector(".maprot-needle");
      L.DomEvent.disableClickPropagation(wrap);
      L.DomEvent.on(btn, "click", (e) => {
        L.DomEvent.stop(e);
        setMode(mode === "head" ? "north" : "head", true);
      });
      return wrap;
    },
  });
  map.addControl(new Ctl());

  VA.mapRot = { mode: () => mode, bearing: () => bearing, setMode: (m) => setMode(m, true) };

  setMode(mode, false);
  requestAnimationFrame(tick);
})();
