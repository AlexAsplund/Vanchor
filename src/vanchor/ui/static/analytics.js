/* Vanchor-NG — Catch analytics + catch heatmap (task #65).
 *
 * Mines the catch log (VA.catchLog, persisted in localStorage `vanchor-catches`)
 * into insights shown in a modal opened from the catch panel or Settings:
 *   - Totals + per-species breakdown (count, avg/max length, avg/max weight).
 *   - "Best time of day": catches binned by hour-of-day (a CSS bar histogram).
 *   - "Best depth": catches binned by depth band, using the `depth` captured at
 *     save time by catch.js (old catches without depth are skipped here).
 *
 * Also provides a toggleable "catch heatmap" map overlay: a lightweight CUSTOM
 * CANVAS density layer (no extra dependency — avoids pulling in leaflet.heat).
 * Each catch position contributes a radial blob; overlapping blobs accumulate,
 * and the summed density is mapped to a cold→warm palette (warmer = more
 * catches). Recomputed whenever the catch set changes and on pan/zoom.
 *
 * All reads are guarded; empty state is handled gracefully. Persistence is
 * read-only here — the canonical store stays in catch.js.
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const map = window.VA && VA.map && VA.map.leaflet;
  const L = window.L;
  const HEAT_VIS_KEY = "vanchor-catch-heatmap-visible";

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function fmt(v, d) {
    return v === null || v === undefined || !Number.isFinite(Number(v)) ? "—" : Number(v).toFixed(d);
  }
  function catches() {
    return (VA.catchLog && typeof VA.catchLog.getAll === "function") ? VA.catchLog.getAll() : [];
  }

  // ======================================================================
  // Heatmap canvas density layer (warmer = more catches)
  // ======================================================================
  // Coalesce redraws to one per animation frame and skip when the map view
  // hasn't shifted by >VIEW_EPS_PX. The boat-follow pan fires moveend ~5 Hz; a
  // per-frame heatmap repaint (getImageData/putImageData over the whole canvas)
  // is very expensive, so this prevents the heatmap from causing frame jank.
  const VIEW_EPS_PX = 6;
  const HeatLayer = L && L.Layer ? L.Layer.extend({
    onAdd(m) {
      this._map = m;
      const c = this._canvas = L.DomUtil.create("canvas", "leaflet-catch-heat");
      c.style.position = "absolute";
      c.style.pointerEvents = "none";
      m.getPanes().overlayPane.appendChild(c);
      m.on("moveend zoomend resize", this._scheduleReset, this);
      this._forceDraw = true;
      this._reset();
    },
    onRemove(m) {
      m.off("moveend zoomend resize", this._scheduleReset, this);
      if (this._resetRAF) { cancelAnimationFrame(this._resetRAF); this._resetRAF = 0; }
      this._lastView = null;
      if (this._canvas && this._canvas.parentNode) this._canvas.parentNode.removeChild(this._canvas);
      this._canvas = null;
    },
    _scheduleReset() {
      if (this._resetRAF) return;
      this._resetRAF = requestAnimationFrame(() => {
        this._resetRAF = 0;
        const m = this._map;
        if (!m || !this._canvas) return;
        if (!this._forceDraw) {
          const z = m.getZoom();
          const cp = m.latLngToContainerPoint(m.getCenter());
          const size = m.getSize();
          const lv = this._lastView;
          if (lv && lv.z === z && lv.sx === size.x && lv.sy === size.y &&
              Math.abs(lv.cx - cp.x) < VIEW_EPS_PX && Math.abs(lv.cy - cp.y) < VIEW_EPS_PX) {
            L.DomUtil.setPosition(this._canvas, m.containerPointToLayerPoint([0, 0]));
            return;
          }
          this._lastView = { z, cx: cp.x, cy: cp.y, sx: size.x, sy: size.y };
        }
        this._forceDraw = false;
        this._reset();
      });
    },
    setPoints(pts) { this._pts = Array.isArray(pts) ? pts : []; this._forceDraw = true; this._scheduleReset(); },
    _reset() {
      const c = this._canvas, m = this._map;
      if (!c || !m) return;
      const size = m.getSize();
      const topLeft = m.containerPointToLayerPoint([0, 0]);
      L.DomUtil.setPosition(c, topLeft);
      const dpr = window.devicePixelRatio || 1;
      if (c.width !== size.x * dpr || c.height !== size.y * dpr) {
        c.width = size.x * dpr; c.height = size.y * dpr;
        c.style.width = size.x + "px"; c.style.height = size.y + "px";
      }
      this._draw(size, dpr);
    },
    _draw(size, dpr) {
      const c = this._canvas, m = this._map;
      const ctx = c.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, size.x, size.y);
      const pts = this._pts || [];
      if (!pts.length) return;
      const bounds = m.getBounds().pad(0.3);
      // Pass 1: accumulate density into an intensity buffer via additive blobs.
      const buf = document.createElement("canvas");
      buf.width = c.width; buf.height = c.height;
      const bctx = buf.getContext("2d");
      bctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      bctx.globalCompositeOperation = "lighter";
      const R = 28; // blob radius (px)
      for (let i = 0; i < pts.length; i++) {
        const p = pts[i];
        if (!p || !Number.isFinite(p.lat) || !Number.isFinite(p.lon)) continue;
        if (p.lat < bounds.getSouth() || p.lat > bounds.getNorth() ||
            p.lon < bounds.getWest() || p.lon > bounds.getEast()) continue;
        const xy = m.latLngToContainerPoint([p.lat, p.lon]);
        const g = bctx.createRadialGradient(xy.x, xy.y, 0, xy.x, xy.y, R);
        g.addColorStop(0, "rgba(0,0,0,0.5)");
        g.addColorStop(1, "rgba(0,0,0,0)");
        bctx.fillStyle = g;
        bctx.beginPath(); bctx.arc(xy.x, xy.y, R, 0, Math.PI * 2); bctx.fill();
      }
      // Pass 2: colorise the accumulated alpha through a cold→warm gradient.
      const img = bctx.getImageData(0, 0, buf.width, buf.height);
      const data = img.data;
      const ramp = makeRamp();
      for (let i = 0; i < data.length; i += 4) {
        const a = data[i + 3];
        if (a === 0) continue;
        const idx = Math.min(255, a) * 4;
        data[i] = ramp[idx]; data[i + 1] = ramp[idx + 1];
        data[i + 2] = ramp[idx + 2]; data[i + 3] = ramp[idx + 3];
      }
      bctx.putImageData(img, 0, 0);
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.globalAlpha = 0.8;
      ctx.drawImage(buf, 0, 0);
      ctx.globalAlpha = 1;
    },
  }) : null;

  // 256-entry RGBA lookup for the heat ramp (cold blue → warm red).
  let _ramp = null;
  function makeRamp() {
    if (_ramp) return _ramp;
    const stops = [
      [0.0, [0, 0, 0, 0]],
      [0.2, [33, 102, 172, 120]],
      [0.4, [103, 169, 207, 170]],
      [0.6, [253, 224, 100, 210]],
      [0.8, [244, 165, 60, 235]],
      [1.0, [215, 48, 60, 255]],
    ];
    const out = new Uint8ClampedArray(256 * 4);
    for (let i = 0; i < 256; i++) {
      const f = i / 255;
      let a = stops[0], b = stops[stops.length - 1];
      for (let s = 0; s < stops.length - 1; s++) {
        if (f >= stops[s][0] && f <= stops[s + 1][0]) { a = stops[s]; b = stops[s + 1]; break; }
      }
      const tt = b[0] === a[0] ? 0 : (f - a[0]) / (b[0] - a[0]);
      for (let ch = 0; ch < 4; ch++) out[i * 4 + ch] = Math.round(a[1][ch] + (b[1][ch] - a[1][ch]) * tt);
    }
    _ramp = out;
    return out;
  }

  const heatLayer = (HeatLayer && map) ? new HeatLayer() : null;
  let heatVisible = false;
  try { heatVisible = localStorage.getItem(HEAT_VIS_KEY) === "1"; } catch (e) { /* ignore */ }

  function refreshHeat() {
    if (!heatLayer) return;
    const pts = catches().filter((c) => Number.isFinite(c.lat) && Number.isFinite(c.lon))
      .map((c) => ({ lat: c.lat, lon: c.lon }));
    heatLayer.setPoints(pts);
  }
  // Guard so the shared layers-control's add/remove handler doesn't recurse
  // while setHeatVisible toggles the layer on the map itself.
  let heatSyncing = false;
  function setHeatVisible(on) {
    heatVisible = !!on;
    if (heatLayer && map) {
      heatSyncing = true;
      if (heatVisible) { if (!map.hasLayer(heatLayer)) heatLayer.addTo(map); refreshHeat(); }
      else if (map.hasLayer(heatLayer)) map.removeLayer(heatLayer);
      heatSyncing = false;
    }
    try { localStorage.setItem(HEAT_VIS_KEY, heatVisible ? "1" : "0"); } catch (e) { /* ignore */ }
    const box = $("catch-heatmap-show");
    if (box) box.checked = heatVisible;
  }
  const heatBox = $("catch-heatmap-show");
  if (heatBox) heatBox.addEventListener("change", () => setHeatVisible(heatBox.checked));

  // ======================================================================
  // Analytics computation
  // ======================================================================
  function computeStats(list) {
    const total = list.length;
    // per-species
    const byS = new Map();
    let withPos = 0;
    list.forEach((c) => {
      if (Number.isFinite(c.lat) && Number.isFinite(c.lon)) withPos++;
      const key = (c.species || "—");
      let s = byS.get(key);
      if (!s) { s = { species: key, n: 0, lenSum: 0, lenN: 0, lenMax: -Infinity, wtSum: 0, wtN: 0, wtMax: -Infinity }; byS.set(key, s); }
      s.n++;
      if (Number.isFinite(c.length)) { s.lenSum += c.length; s.lenN++; if (c.length > s.lenMax) s.lenMax = c.length; }
      if (Number.isFinite(c.weight)) { s.wtSum += c.weight; s.wtN++; if (c.weight > s.wtMax) s.wtMax = c.weight; }
    });
    const species = Array.from(byS.values()).map((s) => ({
      species: s.species, n: s.n,
      avgLen: s.lenN ? s.lenSum / s.lenN : null, maxLen: s.lenMax > -Infinity ? s.lenMax : null,
      avgWt: s.wtN ? s.wtSum / s.wtN : null, maxWt: s.wtMax > -Infinity ? s.wtMax : null,
    })).sort((a, b) => b.n - a.n);

    // hour-of-day histogram (parse "YYYY-MM-DD HH:MM")
    const hours = new Array(24).fill(0);
    list.forEach((c) => {
      const m = /\b(\d{2}):(\d{2})\b/.exec(String(c.date || ""));
      if (m) { const h = parseInt(m[1], 10); if (h >= 0 && h < 24) hours[h]++; }
    });

    // depth bands (2 m bins) — only catches that recorded a depth
    const depthVals = list.map((c) => c.depth).filter((d) => Number.isFinite(d) && d >= 0);
    const bandW = 2;
    let depthBands = [];
    if (depthVals.length) {
      const maxD = Math.max.apply(null, depthVals);
      const nBands = Math.max(1, Math.ceil((maxD + 1e-6) / bandW));
      const counts = new Array(nBands).fill(0);
      depthVals.forEach((d) => { let ix = Math.floor(d / bandW); if (ix >= nBands) ix = nBands - 1; counts[ix]++; });
      depthBands = counts.map((n, i) => ({ label: (i * bandW) + "–" + ((i + 1) * bandW) + " m", n }));
    }
    return { total, withPos, species, hours, depthBands, depthCount: depthVals.length };
  }

  // ======================================================================
  // Rendering
  // ======================================================================
  function barRow(label, n, max) {
    const pct = max > 0 ? Math.round((n / max) * 100) : 0;
    const li = document.createElement("div");
    li.className = "ca-bar-row";
    li.innerHTML =
      `<span class="ca-bar-label">${escapeHtml(label)}</span>` +
      `<span class="ca-bar-track"><span class="ca-bar-fill" style="width:${pct}%"></span></span>` +
      `<span class="ca-bar-n">${n}</span>`;
    return li;
  }

  function render() {
    const list = catches();
    const empty = $("ca-empty"), content = $("ca-content");
    if (!list.length) {
      if (empty) empty.classList.remove("hidden");
      if (content) content.classList.add("hidden");
      return;
    }
    if (empty) empty.classList.add("hidden");
    if (content) content.classList.remove("hidden");

    const st = computeStats(list);

    // totals
    const totals = $("ca-totals");
    if (totals) {
      totals.innerHTML =
        `<div class="ca-tot"><b>${st.total}</b><span>catches</span></div>` +
        `<div class="ca-tot"><b>${st.species.length}</b><span>species</span></div>` +
        `<div class="ca-tot"><b>${st.withPos}</b><span>with GPS</span></div>`;
    }

    // per-species table
    const body = $("ca-species-body");
    if (body) {
      body.innerHTML = "";
      st.species.forEach((s) => {
        const tr = document.createElement("tr");
        tr.innerHTML =
          `<td>${escapeHtml(s.species)}</td><td>${s.n}</td>` +
          `<td>${fmt(s.avgLen, 0)}</td><td>${fmt(s.maxLen, 0)}</td>` +
          `<td>${fmt(s.avgWt, 1)}</td><td>${fmt(s.maxWt, 1)}</td>`;
        body.appendChild(tr);
      });
    }

    // hour histogram
    const hoursEl = $("ca-hours");
    if (hoursEl) {
      hoursEl.innerHTML = "";
      const max = Math.max.apply(null, st.hours.concat([0]));
      if (max <= 0) {
        const p = document.createElement("div"); p.className = "hint"; p.textContent = "No timestamps available.";
        hoursEl.appendChild(p);
      } else {
        for (let h = 0; h < 24; h++) {
          const lbl = (h < 10 ? "0" + h : "" + h) + ":00";
          hoursEl.appendChild(barRow(lbl, st.hours[h], max));
        }
      }
    }

    // depth bands
    const depthEl = $("ca-depth"), note = $("ca-depth-note");
    if (depthEl) {
      depthEl.innerHTML = "";
      if (!st.depthBands.length) {
        depthEl.classList.add("hidden");
        if (note) note.textContent = "No depth recorded yet — depth is captured on catches saved from now on.";
      } else {
        depthEl.classList.remove("hidden");
        const max = Math.max.apply(null, st.depthBands.map((b) => b.n).concat([0]));
        st.depthBands.forEach((b) => depthEl.appendChild(barRow(b.label, b.n, max)));
        if (note) {
          const omitted = st.total - st.depthCount;
          note.textContent = omitted > 0
            ? `${st.depthCount} catch(es) with depth · ${omitted} older without.`
            : `${st.depthCount} catch(es) with depth.`;
        }
      }
    }
  }

  // ======================================================================
  // Modal open/close
  // ======================================================================
  const panel = $("ca-panel"), scrim = $("ca-scrim");
  function setPanel(on) {
    if (panel) panel.classList.toggle("hidden", !on);
    if (scrim) scrim.classList.toggle("hidden", !on);
    if (on) render();
  }
  ["catch-analytics-open", "catch-analytics-open2"].forEach((id) => {
    const b = $(id); if (b) b.addEventListener("click", () => setPanel(true));
  });
  const closeBtn = $("ca-close");
  if (closeBtn) closeBtn.addEventListener("click", () => setPanel(false));
  if (scrim) scrim.addEventListener("click", () => setPanel(false));

  // ======================================================================
  // React to catch-set changes
  // ======================================================================
  if (VA.catchLog && typeof VA.catchLog.onChange === "function") {
    VA.catchLog.onChange(() => {
      if (panel && !panel.classList.contains("hidden")) render();
      if (heatVisible) refreshHeat();
    });
  }

  // Register the Catch heatmap overlay into the shared layers control (#86) so
  // it can be toggled from the top-left panel as well as the Settings checkbox.
  // Register BEFORE the initial setHeatVisible so our own HEAT_VIS_KEY restore
  // also reflects in the control.
  if (heatLayer && VA.map && typeof VA.map.addOverlay === "function") {
    VA.map.addOverlay("Catch heatmap", heatLayer, {
      persistKey: HEAT_VIS_KEY,
      onToggle(on) { if (!heatSyncing) setHeatVisible(on); },
    });
  }

  // init heatmap visibility (after catch.js has loaded the catches)
  setHeatVisible(heatVisible);

  VA.catchAnalytics = { open() { setPanel(true); }, refreshHeat, setHeatVisible };
})();
