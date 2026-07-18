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
  // Perspective TILT (heading-up only): lean the chart away so more of it is
  // visible AHEAD of the boat, navigator-style. 0 = flat (off). Persisted.
  const TILT_KEY = "vanchor-map-tilt";
  let tilt = 0;
  try {
    const tv = parseFloat(localStorage.getItem(TILT_KEY));
    if (Number.isFinite(tv)) tilt = Math.max(0, Math.min(60, tv));
  } catch (e) { /* ignore */ }
  const PERSPECTIVE_PX = 1100;

  // Side of the oversized square the chart needs so no blank corner shows at
  // ANY bearing: 2x the map-plane distance of the farthest viewport corner,
  // unprojected through perspective(p) rotateX(tilt). Exact, replacing the old
  // 1+0.9*sin(tilt) fudge that oversized low tilts (wasted raster/composite
  // area) and UNDERsized 45°+ (clipped top corners while turning). Capped at
  // 2x the viewport diagonal so extreme tilt can't explode the tile count. (perf)
  function neededSide(w, h) {
    const diag = Math.hypot(w, h);
    if (!tilt) return diag;
    const th = (tilt * Math.PI) / 180, p = PERSPECTIVE_PX;
    const cos = Math.cos(th), sin = Math.sin(th);
    let r = 0;
    for (const Y of [-h / 2, h / 2]) {          // screen corners, centre origin
      const denom = p * cos + Y * sin;          // -> 0 at the on-screen horizon
      if (denom <= p * 0.1) { r = Infinity; break; }
      const y = (Y * p) / denom;                // map-plane row for that corner
      const x = (w / 2) * (p - y * sin) / p;    // and its column half-extent
      r = Math.max(r, Math.hypot(x, y));
    }
    return Math.min(2 * r * 1.02, 2 * diag);    // 2% margin, hard cap
  }

  function applyLayout() {
    if (mode === "head") {
      const w = viewport.clientWidth, h = viewport.clientHeight;
      const d = Math.ceil(neededSide(w, h));
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
    const t = mode === "head" ? tilt : 0;
    const parts = [];
    if (t) parts.push(`perspective(${PERSPECTIVE_PX}px)`, `rotateX(${t}deg)`);
    if (b) parts.push(`rotate(${b}deg)`);
    mapEl.style.transform = parts.join(" ");
    mapEl.style.setProperty("--map-derot", `${-b}deg`);
    mapEl.classList.toggle("map-rotated", !!(b || t));
    if (btnNeedle) btnNeedle.style.transform = `rotate(${b}deg)`;
  }

  function setTilt(deg, persist) {
    tilt = Math.max(0, Math.min(60, deg));
    if (persist) { try { localStorage.setItem(TILT_KEY, String(tilt)); } catch (e) { /* ignore */ } }
    applyLayout();
    applyRotation(bearing);
  }

  // ---- client -> container-point unprojection (rotation AND tilt) -----------
  // With perspective in play a screen point maps to a RAY; invert the full CSS
  // matrix and intersect the ray with the map's z=0 plane. Falls back to plain
  // subtraction when no transform is applied.
  function clientToLocal(clientX, clientY) {
    const vrect = viewport.getBoundingClientRect();
    const w = mapEl.clientWidth, h = mapEl.clientHeight;
    // The UNTRANSFORMED box of #map inside the viewport (we set left/top/size).
    const ox = (parseFloat(mapEl.style.left) || 0) + w / 2;
    const oy = (parseFloat(mapEl.style.top) || 0) + h / 2;
    const vx = clientX - vrect.left - ox, vy = clientY - vrect.top - oy;
    const tr = getComputedStyle(mapEl).transform;
    if (!tr || tr === "none") return new L.Point(vx + w / 2, vy + h / 2);
    const inv = new DOMMatrix(tr).inverse();
    const p0 = inv.transformPoint(new DOMPoint(vx, vy, 0, 1));
    const p1 = inv.transformPoint(new DOMPoint(vx, vy, 1, 1));
    const dz = p0.z - p1.z;
    const f = Math.abs(dz) < 1e-9 ? 0 : p0.z / dz;
    return new L.Point(
      p0.x + (p1.x - p0.x) * f + w / 2,
      p0.y + (p1.y - p0.y) * f + h / 2
    );
  }

  // ---- seam 1: pointer -> container point (un-rotate around the centre) -----
  const origM2C = map.mouseEventToContainerPoint.bind(map);
  map.mouseEventToContainerPoint = function (e) {
    if (!bearing && !(mode === "head" && tilt)) return origM2C(e);
    return clientToLocal(e.clientX, e.clientY);
  };

  // ---- seam 2: drag deltas (map pan + marker drags) --------------------------
  // Feed the original handler a shim event whose client coords are the drag
  // START plus the de-rotated delta, so dragging feels screen-true.
  const origOnMove = L.Draggable.prototype._onMove;
  L.Draggable.prototype._onMove = function (e) {
    if ((!bearing && !(mode === "head" && tilt)) || !this._element || !mapEl.contains(this._element) ||
        (e.touches && e.touches.length > 1) || !this._startPoint) {
      return origOnMove.call(this, e);
    }
    const src = (e.touches && e.touches.length === 1) ? e.touches[0] : e;
    // Exact under rotation AND tilt: convert both points to map-local space
    // and re-express the delta in screen coords for the original handler.
    const a = clientToLocal(this._startPoint.x, this._startPoint.y);
    const b = clientToLocal(src.clientX, src.clientY);
    const cx = this._startPoint.x + (b.x - a.x), cy = this._startPoint.y + (b.y - a.y);
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
    // Update the N-UP/HDG label on the map control button.
    if (btnLabel) btnLabel.textContent = mode === "head" ? "HDG" : "N-UP";
    // Toast on user-initiated mode changes.
    if (persist && VA.toast) {
      VA.toast(mode === "head" ? "Heading-up — chart turns with the bow" : "North-up", { ttl: 2400 });
    }
    // Keep the settings segmented control in sync.
    if (segNorth) segNorth.classList.toggle("on", mode === "north");
    if (segHead)  segHead.classList.toggle("on",  mode === "head");
    if (persist) { try { localStorage.setItem(KEY, mode); } catch (e) { /* ignore */ } }
  }

  let btn = null, btnLabel = null, segNorth = null, segHead = null;
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
        `<circle cx="12" cy="12" r="1.6" fill="#1be4ff"/></svg>` +
        `<span class="maprot-label">N-UP</span>`;
      btnNeedle = btn.querySelector(".maprot-needle");
      btnLabel = btn.querySelector(".maprot-label");
      L.DomEvent.disableClickPropagation(wrap);
      L.DomEvent.on(btn, "click", (e) => {
        L.DomEvent.stop(e);
        setMode(mode === "head" ? "north" : "head", true);
      });
      return wrap;
    },
  });
  map.addControl(new Ctl());

  // Settings: heading-up segmented control (North-up | Heading-up) + tilt slider.
  const orientCard = document.getElementById("map-orient-card");
  if (orientCard) {
    const seg = document.createElement("div");
    seg.className = "seg maprot-seg";
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "Map orientation");
    segNorth = document.createElement("button");
    segNorth.type = "button";
    segNorth.textContent = "North-up";
    segHead = document.createElement("button");
    segHead.type = "button";
    segHead.textContent = "Heading-up";
    seg.appendChild(segNorth);
    seg.appendChild(segHead);
    // Insert before the hint text (first child)
    const hint = orientCard.querySelector(".hint");
    orientCard.insertBefore(seg, hint || orientCard.querySelector("summary").nextSibling);
    segNorth.addEventListener("click", () => setMode("north", true));
    segHead.addEventListener("click", () => setMode("head", true));
  }

  // Settings: tilt slider (Map & charts card).
  const tiltSlider = document.getElementById("map-tilt");
  const tiltOut = document.getElementById("map-tilt-val");
  if (tiltSlider) {
    tiltSlider.value = String(tilt);
    if (tiltOut) tiltOut.textContent = String(Math.round(tilt));
    tiltSlider.addEventListener("input", () => {
      const v = parseFloat(tiltSlider.value) || 0;
      if (tiltOut) tiltOut.textContent = String(Math.round(v));
      setTilt(v, true);
    });
  }

  VA.mapRot = {
    mode: () => mode, bearing: () => bearing, setMode: (m) => setMode(m, true),
    tilt: () => tilt, setTilt: (d) => setTilt(d, true),
  };

  setMode(mode, false);
  requestAnimationFrame(tick);
})();
