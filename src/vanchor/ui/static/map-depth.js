/* Vanchor-NG — depth overlay.
 *
 * The automatic depth-map overlay and everything that rides on it: selectable
 * depth palettes, the averaged coloured depth grid (canvas), relief/hillshade,
 * the depth-range highlight band, the isobath contours (marching squares +
 * Chaikin smoothing), the sonar-cone footprint sizing, the legacy per-sounding
 * dot fallback, the grid poll, the legend, and the Settings depth controls.
 *
 * Registers its OWN VA.onTelemetry handler (legend / soundings count / dot
 * fallback) and the Depth map / Depth contours / Relief overlays in the shared
 * layers control. Extends the public VA.map with the depth setters. Reads the
 * shared map from VA.mapCtx. Loads after map-core.js.
 *
 * IMPORTANT — load order: this module registers the LAST built-in overlays and
 * then calls VA.mapCtx.restoreLayers() to restore the saved basemap + overlay
 * selection, so it must load before any other module that registers overlays
 * (Catches, Catch heatmap, No-go zones), matching the original map.js ordering.
 */
"use strict";

(function () {
  const VA = window.VA;
  const ctx = VA.mapCtx;
  const map = ctx.map;
  const addOverlay = ctx.addOverlay;

  // ---- depth overlay -----------------------------------------------------
  // Selectable depth palettes (#palette). Two opposing chart conventions,
  // DELIBERATELY not mixed within one ramp:
  //   "angler"  — high-contrast, shallow = warm/light, deep = dark (the public's
  //               stated preference): coral shoal -> amber -> sand -> teal ->
  //               blue -> deep navy.
  //   "nautical" — S-52 / Navionics navigational convention: shallow = saturated
  //               dark blue (= danger, stands out), deep = pale near-white. Hexes
  //               blend the S-52 DAY_BRIGHT four-shade table with the GEBCO-style
  //               smooth blue ramp so the offscreen upscale yields a continuous
  //               field rather than hard bands.
  const DEPTH_PALETTES = {
    angler: {
      label: "Angler",
      // shallow -> deep
      stops: [
        [0.0, [201, 76, 60]], [0.15, [232, 163, 61]], [0.35, [233, 217, 122]],
        [0.55, [121, 197, 163]], [0.75, [61, 143, 181]], [1.0, [31, 79, 122]],
      ],
    },
    nautical: {
      label: "Nautical",
      // shallow (saturated blue) -> deep (pale near-white)
      stops: [
        [0.0, [63, 117, 186]], [0.18, [98, 153, 207]], [0.40, [152, 197, 242]],
        [0.62, [186, 213, 225]], [0.82, [212, 234, 238]], [1.0, [240, 249, 255]],
      ],
    },
  };
  const PALETTE_KEY = "vanchor-depth-palette";
  let depthPalette = (function () {
    try { const v = localStorage.getItem(PALETTE_KEY); if (v && DEPTH_PALETTES[v]) return v; }
    catch (e) { /* ignore */ }
    return "angler";
  })();

  function depthColorRGB(d, min, max) {
    const span = max - min;
    const f = span > 1e-6 ? Math.max(0, Math.min(1, (d - min) / span)) : 0.5;
    const stops = (DEPTH_PALETTES[depthPalette] || DEPTH_PALETTES.angler).stops;
    let a = stops[0], b = stops[stops.length - 1];
    for (let i = 0; i < stops.length - 1; i++) {
      if (f >= stops[i][0] && f <= stops[i + 1][0]) { a = stops[i]; b = stops[i + 1]; break; }
    }
    const tt = b[0] === a[0] ? 0 : (f - a[0]) / (b[0] - a[0]);
    return a[1].map((ch, i) => Math.round(ch + (b[1][i] - ch) * tt));
  }
  function depthColor(d, min, max) {
    const c = depthColorRGB(d, min, max);
    return `rgb(${c[0]},${c[1]},${c[2]})`;
  }

  // ---- bottom-hardness ramp (hardness, raw 0..127 index) ----------
  // A soft->hard ramp distinct from the depth palettes, scaled over the FIXED
  // 0..127 index (7-bit, no calibration -- not normalised to data min/max):
  // soft bottom (mud/silt) = cool blue, firming through green/amber to a hard
  // rock red. ``gridField`` selects which layer the grid overlay colours by.
  const HARDNESS_STOPS = [
    [0.0, [44, 100, 160]], [0.4, [86, 170, 138]], [0.65, [222, 199, 96]], [1.0, [176, 58, 42]],
  ];
  const HARDNESS_MAX = 127;
  function hardnessColorRGB(v) {
    const f = Math.max(0, Math.min(1, v / HARDNESS_MAX));
    const stops = HARDNESS_STOPS;
    let a = stops[0], b = stops[stops.length - 1];
    for (let i = 0; i < stops.length - 1; i++) {
      if (f >= stops[i][0] && f <= stops[i + 1][0]) { a = stops[i]; b = stops[i + 1]; break; }
    }
    const tt = b[0] === a[0] ? 0 : (f - a[0]) / (b[0] - a[0]);
    return a[1].map((ch, i) => Math.round(ch + (b[1][i] - ch) * tt));
  }
  let gridField = "depth";   // "depth" | "hardness" -- which layer the grid colours by

  // Manual depth-overlay alignment nudge: an imported chart's georeferencing can
  // sit slightly off the basemap. A {lat, lon} delta added to EVERY rendered
  // depth coordinate (grid + contours), persisted across reloads, set via the
  // two-click Adjust tool below.
  const OFFSET_KEY = "vanchor-depth-offset";
  let _depthOffset = (function () {
    try {
      const v = JSON.parse(localStorage.getItem(OFFSET_KEY));
      if (v && Number.isFinite(v.lat) && Number.isFinite(v.lon)) return v;
    } catch (e) { /* ignore */ }
    return { lat: 0, lon: 0 };
  })();
  VA.depthOffset = () => _depthOffset;
  // ---- sonar-cone footprint (#47) ----------------------------------------
  // The depth dots represent the sonar cone's real-world footprint on the
  // bottom: a circle of diameter 2*depth*tan(cone/2). We size each dot in
  // METRES converted to on-screen pixels at the current zoom, so the dots grow
  // with depth and with zoom. The cone angle comes from telemetry
  // (boat.sonar_cone_deg, a backend default) with a localStorage override that
  // the Settings input writes (VA.sonarCone).
  const CONE_KEY = "vanchor-sonar-cone";
  const DEFAULT_CONE = 20;
  VA.sonarCone = {
    override: (function () {
      try { const v = parseFloat(localStorage.getItem(CONE_KEY)); return Number.isFinite(v) ? v : null; }
      catch (e) { return null; }
    })(),
    set(deg) {
      const v = Number(deg);
      if (Number.isFinite(v) && v > 0) {
        this.override = v;
        try { localStorage.setItem(CONE_KEY, String(v)); } catch (e) { /* ignore */ }
      } else {
        this.override = null;
        try { localStorage.removeItem(CONE_KEY); } catch (e) { /* ignore */ }
      }
      redrawDepth();
    },
    // Effective cone angle: override → telemetry → default.
    get() {
      if (Number.isFinite(this.override)) return this.override;
      const tele = VA.last && VA.last.boat && Number(VA.last.boat.sonar_cone_deg);
      return Number.isFinite(tele) && tele > 0 ? tele : DEFAULT_CONE;
    },
  };

  // Metres-per-pixel at a given latitude and the map's current zoom.
  function metresPerPixel(lat) {
    const z = map.getZoom();
    return (40075016.686 * Math.cos((lat * Math.PI) / 180)) / Math.pow(2, z + 8);
  }
  // Footprint radius in pixels for a sounding at (lat, depth).
  function footprintPx(lat, depth) {
    const coneRad = (VA.sonarCone.get() * Math.PI) / 180;
    const diaM = 2 * Math.max(0, depth) * Math.tan(coneRad / 2);
    const px = (diaM / 2) / metresPerPixel(lat);
    // floor so very shallow soundings stay visible; cap so a deep spot can't
    // swamp the map.
    return Math.max(2.5, Math.min(60, px));
  }

  const depthLayer = L.layerGroup();
  let depthShow = false;

  // ---- legacy per-sounding dot fallback ----------------------------------
  // Kept only as a graceful fallback for when the gridded backend endpoint is
  // absent. The default rendering is the averaged colored grid below.
  let lastDepthPts = [];   // cached so zoomend can re-render footprints
  let lastDepthMin = 0, lastDepthMax = 1;
  function renderDepthDots() {
    depthLayer.clearLayers();
    for (const p of lastDepthPts) {
      if (!Array.isArray(p) || p.length < 3) continue;
      const [lat, lon, d] = p;
      if (!Number.isFinite(lat) || !Number.isFinite(lon) || !Number.isFinite(d)) continue;
      L.circleMarker([lat, lon], {
        radius: footprintPx(lat, d), weight: 0,
        fillColor: depthColor(d, lastDepthMin, lastDepthMax), fillOpacity: 0.6,
      }).addTo(depthLayer).bindTooltip(d.toFixed(1) + " m");
    }
  }
  function renderDepthDotsFrom(t) {
    const pts = Array.isArray(t && t.depth_points) ? t.depth_points : [];
    let min = Infinity, max = -Infinity;
    for (const p of pts) {
      const d = Array.isArray(p) ? p[2] : null;
      if (Number.isFinite(d)) { if (d < min) min = d; if (d > max) max = d; }
    }
    if (!Number.isFinite(min)) { min = 0; max = 1; }
    lastDepthPts = pts; lastDepthMin = min; lastDepthMax = max;
    setLegend(min, max, pts.length);
    renderDepthDots();
  }

  // ---- relief / hillshade + depth-range highlight state (#relief #highlight)
  // Both ride INSIDE the fast offscreen grid pass (no per-pixel canvas filters):
  // the shade is baked per-lattice-cell into the same low-res offscreen that is
  // then upscaled in one blit. Persisted toggles, mirrored into the layers panel
  // + Settings.
  const RELIEF_KEY = "vanchor-depth-relief";
  let reliefShow = (function () {
    try { const v = localStorage.getItem(RELIEF_KEY); if (v === "0") return false; if (v === "1") return true; }
    catch (e) { /* ignore */ }
    return true;     // sensible default: relief ON (the most-praised look)
  })();
  // Light from the NW (azimuth 315°, elevation 45°) per the research doc §5.
  // Precompute the light vector in screen/grid space: +x = east, +y = south
  // (canvas y grows downward), z up. azimuth measured clockwise from north.
  const HILLSHADE = (function () {
    const azDeg = 315, elDeg = 45;
    const az = azDeg * Math.PI / 180, el = elDeg * Math.PI / 180;
    const cosEl = Math.cos(el);
    // direction the light comes FROM, projected to grid axes (i = north→+, j = east→+)
    return {
      // light vector components in (east, north, up)
      lx: cosEl * Math.sin(az),     // east component
      ly: cosEl * Math.cos(az),     // north component
      lz: Math.sin(el),             // up
      strength: 0.32,               // composite strength (~32%, research §5)
      zScale: 4.0,                  // vertical exaggeration so subtle ledges read
    };
  })();

  // Depth-range highlight (Humminbird LakeMaster style, research §8). Cells whose
  // depth is within [hlMin, hlMax] get a bright green glow; the rest are dimmed
  // slightly so the band "lights up" across the whole map.
  const HL_KEY = "vanchor-depth-highlight";
  const hlState = (function () {
    const def = { on: false, min: 3, max: 5 };
    try {
      const raw = localStorage.getItem(HL_KEY);
      if (raw) { const o = JSON.parse(raw); if (o && typeof o === "object") return Object.assign(def, o); }
    } catch (e) { /* ignore */ }
    return def;
  })();
  function saveHlState() {
    try { localStorage.setItem(HL_KEY, JSON.stringify(hlState)); } catch (e) { /* ignore */ }
  }

  // ---- averaged colored depth grid (canvas overlay) ----------------------
  // A real depth-chart look: the backend buckets ~100k soundings into cells of
  // cell_m metres and returns one averaged depth per cell. We paint every cell
  // as a filled, semi-transparent rectangle into ONE canvas in a single pass —
  // this stays smooth at thousands of cells where per-cell Leaflet vectors
  // would choke. The canvas is repositioned/redrawn on pan & zoom.
  // ---- canvas-overlay redraw coalescing (perf) ---------------------------
  // The boat-follow pan fires `moveend` every ~5 Hz telemetry frame (a few px
  // of view shift). Redrawing these canvas overlays (depth grid / contours /
  // heatmap) on every one of those is the dominant source of frame jank — each
  // redraw iterates thousands of cells/segments and is a 50–110 ms long task.
  // This mixin (a) coalesces bursts of moveend/zoomend/resize into at most one
  // redraw per animation frame via requestAnimationFrame, and (b) skips the
  // redraw entirely when the map view hasn't shifted by at least VIEW_EPS_PX
  // since the last draw — so the sub-pixel micro-pans of boat-follow no longer
  // trigger full repaints, while genuine pans/zooms still redraw promptly.
  const VIEW_EPS_PX = 6;
  const CanvasOverlayMixin = {
    // Subclasses bind moveend/zoomend/resize to _scheduleReset (not _reset).
    _scheduleReset() {
      if (this._resetRAF) return;            // already queued for this frame
      this._resetRAF = requestAnimationFrame(() => {
        this._resetRAF = 0;
        const m = this._map;
        if (!m || !this._canvas) return;
        // Skip if neither zoom nor (pixel) center moved enough to matter, and
        // the canvas size is unchanged. setData() bypasses this via _forceDraw.
        if (!this._forceDraw) {
          const z = m.getZoom();
          // Zoom-aware pan epsilon: at high zoom (>= 18) cruising a few px/frame
          // must NOT re-trigger the 50-110 ms grid redraw several times a second,
          // so widen the skip threshold; keep the tight 6 px below z18. (perf)
          const eps = z >= 18 ? 24 : VIEW_EPS_PX;
          // Track the canvas's top-left in LAYER coords -- this moves with every
          // pan. (The map centre in *container* coords is always ~size/2, so the
          // old check never detected a pan and skipped the redraw, leaving the
          // overlay screen-glued/offset until a zoom forced a redraw.)
          const tl = m.containerPointToLayerPoint([0, 0]);
          const size = m.getSize();
          const lv = this._lastView;
          if (lv && lv.z === z && lv.sx === size.x && lv.sy === size.y &&
              Math.abs(lv.tx - tl.x) < eps && Math.abs(lv.ty - tl.y) < eps) {
            // View unchanged -> just keep the canvas glued (cheap), skip redraw.
            L.DomUtil.setPosition(this._canvas, tl);
            return;
          }
          this._lastView = { z, tx: tl.x, ty: tl.y, sx: size.x, sy: size.y };
        }
        this._forceDraw = false;
        this._reset();
      });
    },
    _cancelReset() {
      if (this._resetRAF) { cancelAnimationFrame(this._resetRAF); this._resetRAF = 0; }
      this._lastView = null;
    },
    // Track the zoom ANIMATION (bound to "zoomanim") so the canvas scales +
    // translates with the map instead of showing wrong-scale content until the
    // zoomend redraw. Mirrors Leaflet's own overlay/renderer zoom handling.
    _animateZoom(e) {
      const m = this._map;
      if (!m || !this._canvas) return;
      const scale = m.getZoomScale(e.zoom);
      const offset = m._latLngToNewLayerPoint(m.containerPointToLatLng([0, 0]), e.zoom, e.center);
      L.DomUtil.setTransform(this._canvas, offset, scale);
    },
  };

  // Shared OSM water clip (lon/lat polygon rings, true position -- no offset).
  // Depth/contours/composition all clip to it so the overlays never paint over
  // land or islands. Null = no water loaded -> draw unclipped. Apply between
  // ctx.save()/ctx.restore() in each layer's _draw.
  let waterMask = null;
  // Bumps whenever `waterMask` is replaced (refetch). Part of the clip cache key
  // so a new mask invalidates the cached Path2D even at an unchanged view.
  let waterMaskVersion = 0;
  // The ~930k-vertex OSM water polygon is identical across all three overlays
  // within a frame and across frames at the same view, but its PROJECTION to
  // screen changes on every pan/zoom. We tessellate it into a Path2D exactly
  // once per (view, mask) and reuse it; `ctx.clip(path, "evenodd")` preserves
  // the exterior-minus-holes semantics of the old per-draw beginPath/clip.
  let _clipCache = null;        // { path, key } where path is a Path2D
  function _clipCacheKey(m) {
    const tl = m.containerPointToLayerPoint([0, 0]);
    const size = m.getSize();
    return waterMaskVersion + ":" + m.getZoom() + ":" + tl.x + ":" + tl.y +
           ":" + size.x + ":" + size.y;
  }
  function clipToWaterMask(ctx2, m) {
    if (!waterMask || !waterMask.length) return;
    const key = _clipCacheKey(m);
    if (!_clipCache || _clipCache.key !== key) {
      // Rebuild the Path2D from the mask projected to the current view.
      const path = new Path2D();
      for (const poly of waterMask) {
        for (const ring of poly) {
          if (!ring.length) continue;
          const q0 = m.latLngToContainerPoint([ring[0][1], ring[0][0]]);  // ring=[lon,lat]
          path.moveTo(q0.x, q0.y);
          for (let k = 1; k < ring.length; k++) {
            const q = m.latLngToContainerPoint([ring[k][1], ring[k][0]]);
            path.lineTo(q.x, q.y);
          }
          path.closePath();
        }
      }
      _clipCache = { path, key };
    }
    ctx2.clip(_clipCache.path, "evenodd");   // exterior minus island holes = water
  }

  const GridLayer = L.Layer.extend(Object.assign({}, CanvasOverlayMixin, {
    onAdd(m) {
      this._map = m;
      const c = this._canvas = L.DomUtil.create("canvas", "leaflet-depth-grid");
      c.style.position = "absolute";
      c.style.pointerEvents = "none";
      // Composition renders in a lower pane so it sits UNDER the contour lines.
      ((this._paneName && m.getPane(this._paneName)) || m.getPanes().overlayPane).appendChild(c);
      m.on("moveend zoomend resize", this._scheduleReset, this);
      if (m.options.zoomAnimation) m.on("zoomanim", this._animateZoom, this);
      this._forceDraw = true;
      this._reset();
    },
    onRemove(m) {
      m.off("moveend zoomend resize", this._scheduleReset, this);
      m.off("zoomanim", this._animateZoom, this);
      this._cancelReset();
      if (this._canvas && this._canvas.parentNode) this._canvas.parentNode.removeChild(this._canvas);
      this._canvas = null;
    },
    setData(cells, min, max, cellM) {
      this._cells = Array.isArray(cells) ? cells : [];
      this._min = Number.isFinite(min) ? min : 0;
      this._max = Number.isFinite(max) ? max : 1;
      this._cellM = Number.isFinite(cellM) && cellM > 0 ? cellM : null;
      this._buildField();
      this._forceDraw = true;        // new data → always repaint
      this._scheduleReset();
    },
    // Index the cells onto an integer (i,j) metric lattice (i = north, j = east)
    // and store each cell's depth keyed "i,j". This lets the hillshade pass read
    // a cell's N/S and E/W neighbours to estimate the bottom gradient — the same
    // lattice ContourLayer builds for marching squares. Built once per setData.
    _buildField() {
      this._field = null;
      const cells = this._cells;
      if (!cells || cells.length < 4) return;
      let lat0 = Infinity, lon0 = Infinity;
      for (const k of cells) { if (!k) continue; if (k.lat < lat0) lat0 = k.lat; if (k.lon < lon0) lon0 = k.lon; }
      const cellM = this._cellM || 5;
      const cosLat = Math.cos(lat0 * Math.PI / 180) || 1e-6;
      const dlat = cellM / 111320, dlon = dlat / cosLat;
      const f = new Map();
      for (const k of cells) {
        if (!k) continue;
        const i = Math.round((k.lat - lat0) / dlat);
        const j = Math.round((k.lon - lon0) / dlon);
        k._i = i; k._j = j;            // remember each cell's lattice index
        f.set(i + "," + j, +k.depth);
      }
      this._field = { f, cellM };
    },
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
      const ctx2 = c.getContext("2d");
      ctx2.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx2.clearRect(0, 0, size.x, size.y);
      const cells = this._cells;
      if (!cells || !cells.length) return;
      const min = this._min, max = this._max;
      const bounds = m.getBounds().pad(0.25);
      const bS = bounds.getSouth(), bN = bounds.getNorth(), bW = bounds.getWest(), bE = bounds.getEast();
      const cellM = this._cellM || 5;
      // One projection per cell (cells are a uniform metric lattice, so on-screen
      // size is ~constant over the viewport — derive it once from m/px).
      const centerLat = (bS + bN) / 2;
      const mpp = (40075016.686 * Math.cos((centerLat * Math.PI) / 180)) / Math.pow(2, m.getZoom() + 8);
      const wpx = cellM / mpp;            // cell size on screen (CSS px)

      // Relief: precompute the per-lattice gradient → Lambert shade factor once.
      // light·normal in [0..1]; we map to a multiply/screen tint baked into each
      // blob's colour (NO per-pixel canvas filter — all in the cheap offscreen).
      const fd = this._field;
      const aoff = VA.depthOffset ? VA.depthOffset() : { lat: 0, lon: 0 };  // alignment nudge
      // Relief (depth-slope hillshade) and the depth-range highlight are
      // depth-only concepts -- disabled when colouring by bottom hardness.
      const isDepthField = gridField !== "hardness";
      const doRelief = isDepthField && reliefShow && fd && fd.f.size > 3;
      // Depth-range highlight (#highlight): cells in [hlMin,hlMax] glow green; the
      // rest are dimmed. Cheap recolour at blob time.
      const doHl = isDepthField && hlState.on;
      const hlLo = Math.min(hlState.min, hlState.max), hlHi = Math.max(hlState.min, hlState.max);

      // SMOOTH SURFACE, fast: project the visible cells, paint them SHARP into a
      // small low-res offscreen, then blit it up to full size with bilinear
      // smoothing. One GPU upscale turns the blocky mosaic into a continuous
      // bathymetric field — far cheaper than a per-cell blur (which hangs).
      const xs = [], ys = [], cols = [];
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      const f = fd ? fd.f : null;
      // Horizontal grid spacing in metres for the slope (one cell).
      const cellMeters = cellM || 5;
      for (let i = 0; i < cells.length; i++) {
        const k = cells[i];
        if (!k) continue;
        const lat = +k.lat, lon = +k.lon, d = +k.depth;
        if (!Number.isFinite(lat) || !Number.isFinite(lon) || !Number.isFinite(d)) continue;
        if (lat < bS || lat > bN || lon < bW || lon > bE) continue;
        const pc = m.latLngToContainerPoint([lat + aoff.lat, lon + aoff.lon]);
        let rgb = gridField === "hardness" ? hardnessColorRGB(d) : depthColorRGB(d, min, max);

        if (doRelief && k._i !== undefined) {
          // Central differences on DEPTH (positive = deeper). Treat the bottom as
          // an elevation = -depth so a shoaling ledge (gets shallower toward the
          // light) catches light. Fall back to the centre cell at gaps.
          const ii = k._i, jj = k._j;
          const dN = f.get((ii + 1) + "," + jj), dS = f.get((ii - 1) + "," + jj);
          const dE = f.get(ii + "," + (jj + 1)), dW = f.get(ii + "," + (jj - 1));
          const zN = -(dN === undefined ? d : dN), zS = -(dS === undefined ? d : dS);
          const zE = -(dE === undefined ? d : dE), zW = -(dW === undefined ? d : dW);
          // gradient of elevation: d/dEast, d/dNorth (metres per metre)
          const gE = (zE - zW) / (2 * cellMeters) * HILLSHADE.zScale;
          const gN = (zN - zS) / (2 * cellMeters) * HILLSHADE.zScale;
          // surface normal ~ (-gE, -gN, 1); Lambert with the NW light vector.
          const inv = 1 / Math.sqrt(gE * gE + gN * gN + 1);
          let dot = (-gE * HILLSHADE.lx + (-gN) * HILLSHADE.ly + HILLSHADE.lz) * inv;
          if (dot < 0) dot = 0; if (dot > 1) dot = 1;
          // shade in [-1..1]: >0 lit (lighten), <0 in shadow (darken), at strength.
          const shade = (dot - 0.5) * 2 * HILLSHADE.strength;
          if (shade >= 0) {
            rgb = [rgb[0] + (255 - rgb[0]) * shade, rgb[1] + (255 - rgb[1]) * shade, rgb[2] + (255 - rgb[2]) * shade];
          } else {
            const s = 1 + shade;        // darken toward black
            rgb = [rgb[0] * s, rgb[1] * s, rgb[2] * s];
          }
        }

        let inBand = false;
        if (doHl) {
          inBand = d >= hlLo && d <= hlHi;
          if (inBand) {
            // bright green glow: blend strongly toward green, keep some depth tint.
            rgb = [rgb[0] * 0.25 + 51 * 0.75, rgb[1] * 0.25 + 220 * 0.75, rgb[2] * 0.25 + 71 * 0.75];
          } else {
            rgb = [rgb[0] * 0.55, rgb[1] * 0.55, rgb[2] * 0.6];   // dim the rest
          }
        }

        xs.push(pc.x); ys.push(pc.y);
        cols.push(`rgb(${Math.round(rgb[0])},${Math.round(rgb[1])},${Math.round(rgb[2])})`);
        if (pc.x < minX) minX = pc.x; if (pc.x > maxX) maxX = pc.x;
        if (pc.y < minY) minY = pc.y; if (pc.y > maxY) maxY = pc.y;
      }
      if (!xs.length) return;
      const pad = wpx + 6;
      minX -= pad; minY -= pad; maxX += pad; maxY += pad;
      const bw = Math.max(1, maxX - minX), bh = Math.max(1, maxY - minY);
      const SCALE = 0.5;                  // offscreen resolution vs screen
      const ow = Math.max(1, Math.round(bw * SCALE)), oh = Math.max(1, Math.round(bh * SCALE));
      const off = document.createElement("canvas");
      off.width = ow; off.height = oh;
      const octx = off.getContext("2d");
      // Generous blobs so the (sparse, ribbon-like) soundings melt into a solid,
      // continuous surface rather than faint scattered dots.
      const rs = Math.max(3.5, wpx * SCALE * 2.6);   // big enough to merge solidly
      for (let i = 0; i < xs.length; i++) {
        octx.fillStyle = cols[i];
        octx.fillRect((xs[i] - minX) * SCALE - rs / 2, (ys[i] - minY) * SCALE - rs / 2, rs, rs);
      }
      // Bold + opaque; the half-res bilinear upscale supplies the smoothing
      // (no extra blur — that just thins a narrow survey ribbon). Clipped to the
      // water polygon so the interpolated depth doesn't bleed over land/islands.
      ctx2.save();
      clipToWaterMask(ctx2, m);
      ctx2.globalAlpha = 0.92;
      ctx2.imageSmoothingEnabled = true;
      ctx2.imageSmoothingQuality = "high";
      ctx2.drawImage(off, 0, 0, ow, oh, minX, minY, bw, bh);
      ctx2.restore();
    },
  }));
  const gridLayer = new GridLayer();

  // ---- contour polyline chaining + Chaikin smoothing (#chaikin) -----------
  // Marching squares emits disjoint segments; to smooth an isobath we first chain
  // segments that share an endpoint into continuous polylines, then apply Chaikin
  // corner-cutting so the blocky lattice zigzag becomes a smooth curve. Endpoints
  // are quantised to a small grid so floating-point near-matches still join.
  // Input: flat [x0,y0,x1,y1,...]. Output: array of polylines (each [x,y,...]).
  function chainSegments(seg) {
    const Q = 2;                                  // quantise key (px) for joins
    const key = (x, y) => Math.round(x / Q) + ":" + Math.round(y / Q);
    const adj = new Map();                        // node key -> [{to, x0,y0,x1,y1, used}]
    const node = (k) => { let a = adj.get(k); if (!a) { a = []; adj.set(k, a); } return a; };
    const edges = [];
    for (let s = 0; s < seg.length; s += 4) {
      const x0 = seg[s], y0 = seg[s + 1], x1 = seg[s + 2], y1 = seg[s + 3];
      const ka = key(x0, y0), kb = key(x1, y1);
      const e = { ka, kb, x0, y0, x1, y1, used: false };
      edges.push(e);
      node(ka).push(e); node(kb).push(e);
    }
    const lines = [];
    for (const start of edges) {
      if (start.used) continue;
      // Walk forward from this edge, then backward, building one polyline.
      const pts = [start.x0, start.y0, start.x1, start.y1];
      start.used = true;
      // forward: extend from the current tail key
      let tailKey = start.kb, tx = start.x1, ty = start.y1;
      let grew = true;
      while (grew) {
        grew = false;
        const cand = adj.get(tailKey);
        if (!cand) break;
        for (const e of cand) {
          if (e.used) continue;
          if (e.ka === tailKey) { pts.push(e.x1, e.y1); tx = e.x1; ty = e.y1; tailKey = e.kb; }
          else if (e.kb === tailKey) { pts.push(e.x0, e.y0); tx = e.x0; ty = e.y0; tailKey = e.ka; }
          else continue;
          e.used = true; grew = true; break;
        }
      }
      // backward from the head
      let headKey = start.ka;
      grew = true;
      while (grew) {
        grew = false;
        const cand = adj.get(headKey);
        if (!cand) break;
        for (const e of cand) {
          if (e.used) continue;
          if (e.ka === headKey) { pts.unshift(e.x1, e.y1); headKey = e.kb; }
          else if (e.kb === headKey) { pts.unshift(e.x0, e.y0); headKey = e.ka; }
          else continue;
          e.used = true; grew = true; break;
        }
      }
      lines.push(pts);
    }
    return lines;
  }
  // Chaikin corner-cutting on a flat [x,y,...] polyline. `iter` passes (1–2).
  // Detects a closed ring (first ≈ last) and keeps it closed.
  function chaikin(pts, iter) {
    for (let it = 0; it < iter; it++) {
      const n = pts.length;
      if (n < 6) break;                           // <3 points: nothing to cut
      const closed = Math.abs(pts[0] - pts[n - 2]) < 0.6 && Math.abs(pts[1] - pts[n - 1]) < 0.6;
      const out = [];
      if (!closed) { out.push(pts[0], pts[1]); }
      const last = n - 2;
      for (let i = 0; i < last; i += 2) {
        const ax = pts[i], ay = pts[i + 1], bx = pts[i + 2], by = pts[i + 3];
        out.push(ax + (bx - ax) * 0.25, ay + (by - ay) * 0.25);
        out.push(ax + (bx - ax) * 0.75, ay + (by - ay) * 0.75);
      }
      if (!closed) { out.push(pts[last], pts[last + 1]); }
      else { out.push(out[0], out[1]); }          // re-close the ring
      pts = out;
    }
    return pts;
  }

  // ---- depth contours (isobaths via marching squares) (#105) -------------
  // A separate, toggleable cartographic overlay: thin isobath lines computed
  // CLIENT-SIDE from the very same gridded depth cells the colored heatmap
  // uses (no backend/API change). We rebuild a regular (i,j) scalar field from
  // the cells (snapping each to its lattice index via cell_m), run marching
  // squares per cell at each isobath level, then chain the segments into
  // polylines, Chaikin-smooth them, and stroke. Deeper isobaths read stronger; a
  // few points carry inline depth labels.
  // Default off; registered in the unified layers panel + persisted.
  const ContourLayer = L.Layer.extend(Object.assign({}, CanvasOverlayMixin, {
    onAdd(m) {
      this._map = m;
      const c = this._canvas = L.DomUtil.create("canvas", "leaflet-depth-contours");
      c.style.position = "absolute";
      c.style.pointerEvents = "none";
      m.getPanes().overlayPane.appendChild(c);
      m.on("moveend zoomend resize", this._scheduleReset, this);
      if (m.options.zoomAnimation) m.on("zoomanim", this._animateZoom, this);
      this._forceDraw = true;
      this._reset();
    },
    onRemove(m) {
      m.off("moveend zoomend resize", this._scheduleReset, this);
      m.off("zoomanim", this._animateZoom, this);
      this._cancelReset();
      if (this._canvas && this._canvas.parentNode) this._canvas.parentNode.removeChild(this._canvas);
      this._canvas = null;
    },
    // Same cell payload as the grid. We index cells onto a lattice using cell_m
    // so neighbouring cells share edges for marching squares.
    setData(cells, min, max, cellM) {
      this._cells = Array.isArray(cells) ? cells : [];
      this._min = Number.isFinite(min) ? min : 0;
      this._max = Number.isFinite(max) ? max : 1;
      this._cellM = Number.isFinite(cellM) && cellM > 0 ? cellM : 15;
      this._buildField();
      this._forceDraw = true;        // new data → always repaint
      this._scheduleReset();
    },
    // Explicit imported contour polylines [{d, pts:[[lat,lon],...]}, ...] -- the
    // the imported isobaths, preferred over the marching-squares fallback. Pass
    // null to drop back to deriving contours from the grid.
    setExplicit(contours, min, max) {
      this._explicit = Array.isArray(contours) && contours.length ? contours : null;
      if (Number.isFinite(min)) this._emin = min;
      if (Number.isFinite(max)) this._emax = max;
      this._forceDraw = true;
      this._scheduleReset();
    },
    _drawExplicit(ctx2) {
      const m = this._map, b = m.getBounds().pad(0.25);
      const bS = b.getSouth(), bN = b.getNorth(), bW = b.getWest(), bE = b.getEast();
      const min = Number.isFinite(this._emin) ? this._emin : 0;
      const max = Number.isFinite(this._emax) ? this._emax : 1;
      const off = (typeof VA.depthOffset === "function") ? VA.depthOffset() : { lat: 0, lon: 0 };
      ctx2.lineWidth = 1.2; ctx2.lineJoin = "round";
      // Group visible polylines by depth and stroke ONCE per depth level. The
      // imported isobaths sit on discrete levels, so this collapses tens of
      // thousands of per-segment stroke() calls (which peg the browser) into a
      // few dozen. (off) applies the manual overlay alignment nudge.
      const byDepth = new Map();
      for (const cnt of this._explicit) {
        const pts = cnt.pts;
        if (!pts || pts.length < 2) continue;
        let vis = false;
        for (let k = 0; k < pts.length; k++) {
          if (pts[k][0] >= bS && pts[k][0] <= bN && pts[k][1] >= bW && pts[k][1] <= bE) { vis = true; break; }
        }
        if (!vis) continue;
        let arr = byDepth.get(cnt.d);
        if (!arr) { arr = []; byDepth.set(cnt.d, arr); }
        arr.push(pts);
      }
      const labels = [];
      ctx2.save();
      clipToWaterMask(ctx2, m);                   // keep isobaths on the water
      for (const [d, lines] of byDepth) {
        const rgb = depthColorRGB(+d, min, max);
        ctx2.strokeStyle = `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
        ctx2.beginPath();
        let n = 0;
        for (const pts of lines) {
          for (let k = 0; k < pts.length; k++) {
            const p = m.latLngToContainerPoint([pts[k][0] + off.lat, pts[k][1] + off.lon]);
            if (k === 0) ctx2.moveTo(p.x, p.y); else ctx2.lineTo(p.x, p.y);
          }
          if ((n++ % 60) === 0) {                 // stash a sparse label point
            const mid = pts[(pts.length / 2) | 0];
            labels.push([m.latLngToContainerPoint([mid[0] + off.lat, mid[1] + off.lon]), Math.round(d)]);
          }
        }
        ctx2.stroke();                            // one stroke for the whole level
      }
      ctx2.restore();                             // end water clip (labels stay unclipped)
      // Sparse inline depth labels (haloed) so the chart reads as depths.
      ctx2.font = "10px system-ui, sans-serif";
      ctx2.textAlign = "center"; ctx2.textBaseline = "middle";
      ctx2.lineWidth = 3; ctx2.strokeStyle = "rgba(0,0,0,0.6)"; ctx2.fillStyle = "rgba(255,255,255,0.95)";
      for (const [p, txt] of labels) {
        ctx2.strokeText(txt, p.x, p.y);
        ctx2.fillText(txt, p.x, p.y);
      }
    },
    // Build a sparse scalar field keyed by integer (i,j) lattice indices, plus
    // the lat/lon of the (0,0) lattice origin and the per-cell degree steps.
    _buildField() {
      const cells = this._cells;
      this._field = null;
      if (!cells || cells.length < 4) return;
      let lat0 = Infinity, lon0 = Infinity;
      for (const k of cells) {
        if (!k) continue;
        if (k.lat < lat0) lat0 = k.lat;
        if (k.lon < lon0) lon0 = k.lon;
      }
      const cosLat = Math.cos(lat0 * Math.PI / 180) || 1e-6;
      const dlat = this._cellM / 111320;
      const dlon = dlat / cosLat;
      const f = new Map();             // "i,j" -> depth
      let iMax = 0, jMax = 0;
      for (const k of cells) {
        if (!k) continue;
        const i = Math.round((k.lat - lat0) / dlat);
        const j = Math.round((k.lon - lon0) / dlon);
        f.set(i + "," + j, +k.depth);
        if (i > iMax) iMax = i;
        if (j > jMax) jMax = j;
      }
      this._field = { f, lat0, lon0, dlat, dlon, iMax, jMax };
    },
    // Choose isobath levels: ~1 m spacing, but adapt up if the range is wide so
    // we never draw a runaway number of lines.
    _levels() {
      let lo = Math.ceil(this._min), hi = Math.floor(this._max);
      if (!(hi > lo)) return [];
      let step = 2;                               // ~2 m isobaths (less clutter)
      while ((hi - lo) / step > 14) step *= 2;    // cap the line count
      const out = [];
      for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(v);
      return out;
    },
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
    // Marching squares over the lattice. For each cell with all 4 corners
    // present we classify the square against the iso-level and emit the linearly
    // interpolated crossing segment(s). Missing corners (data gaps) skip the
    // square, so contours only form where the grid is filled.
    _draw(size, dpr) {
      const c = this._canvas, m = this._map;
      const ctx2 = c.getContext("2d");
      ctx2.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx2.clearRect(0, 0, size.x, size.y);
      if (this._explicit) { this._drawExplicit(ctx2); return; }  // real imported isobaths
      const fd = this._field;
      if (!fd) return;
      const { f, lat0, lon0, dlat, dlon, iMax, jMax } = fd;
      const levels = this._levels();
      if (!levels.length) return;
      const span = this._max - this._min || 1;
      const bounds = m.getBounds().pad(0.25);
      // Hoist bounds getters out of the per-cell/per-level hot loops.
      const bS = bounds.getSouth(), bN = bounds.getNorth(), bW = bounds.getWest(), bE = bounds.getEast();

      // Lattice (i,j) -> screen point, memoized per draw: the same lattice corner
      // is needed by up to ~24 isobath levels, but its projection is level-
      // independent. Caching collapses thousands of redundant latLngToContainerPoint
      // calls (the dominant cost) into one per distinct corner. (perf)
      const ptCache = new Map();
      const pt = (i, j) => {
        const key = i * 100003 + j;        // collision-free for realistic lattices
        let v = ptCache.get(key);
        if (v === undefined) { v = m.latLngToContainerPoint([lat0 + i * dlat, lon0 + j * dlon]); ptCache.set(key, v); }
        return v;
      };
      const get = (i, j) => f.get(i + "," + j);

      const labelEvery = 14;           // sample inline labels sparsely
      let labelTick = 0;
      ctx2.lineWidth = 1;
      ctx2.lineJoin = "round";
      ctx2.font = "10px system-ui, sans-serif";
      ctx2.textAlign = "center";
      ctx2.textBaseline = "middle";

      // Per-level segment buckets so we can scan the lattice ONCE (instead of
      // once per isobath level) and project each corner ONCE (via the memoized
      // `pt`), then stroke all segments of a level in a single batched path.
      // Previously the lattice was rescanned + reprojected for every level — the
      // dominant contour-draw cost. Output is identical. (perf)
      const nLv = levels.length;
      const segsByLvl = new Array(nLv);            // each: flat [x0,y0,x1,y1,...]
      const labelsByLvl = new Array(nLv);          // each: [mx,my,...]
      for (let l = 0; l < nLv; l++) { segsByLvl[l] = []; labelsByLvl[l] = []; }

      for (let i = 0; i < iMax; i++) {
        for (let j = 0; j < jMax; j++) {
          // Corners: bl=(i,j) br=(i,j+1) tr=(i+1,j+1) tl=(i+1,j).
          const d0 = get(i, j), d1 = get(i, j + 1);
          const d2 = get(i + 1, j + 1), d3 = get(i + 1, j);
          if (d0 === undefined || d1 === undefined || d2 === undefined || d3 === undefined) continue;
          // Cull off-screen squares cheaply via the bottom-left corner.
          const clat = lat0 + i * dlat, clon = lon0 + j * dlon;
          if (clat < bS || clat > bN || clon < bW || clon > bE) continue;

          // Min/max corner depth: a level only crosses this cell when within it.
          let dmin = d0, dmax = d0;
          if (d1 < dmin) dmin = d1; else if (d1 > dmax) dmax = d1;
          if (d2 < dmin) dmin = d2; else if (d2 > dmax) dmax = d2;
          if (d3 < dmin) dmin = d3; else if (d3 > dmax) dmax = d3;

          // Project the 4 corners ONCE for this cell (memoized across cells).
          let Pbl, Pbr, Ptr, Ptl, bl, br, tr, tl;
          const lerp = (pa, pb, va, vb, lvl) => {
            const tt = (lvl - va) / (vb - va);
            return [pa[0] + (pb[0] - pa[0]) * tt, pa[1] + (pb[1] - pa[1]) * tt];
          };

          for (let l = 0; l < nLv; l++) {
            const lvl = levels[l];
            if (lvl < dmin || lvl > dmax) continue;   // no crossing in this cell
            let idx = 0;
            if (d0 >= lvl) idx |= 1;
            if (d1 >= lvl) idx |= 2;
            if (d2 >= lvl) idx |= 4;
            if (d3 >= lvl) idx |= 8;
            if (idx === 0 || idx === 15) continue;

            if (bl === undefined) {
              Pbl = pt(i, j); Pbr = pt(i, j + 1); Ptr = pt(i + 1, j + 1); Ptl = pt(i + 1, j);
              bl = [Pbl.x, Pbl.y]; br = [Pbr.x, Pbr.y]; tr = [Ptr.x, Ptr.y]; tl = [Ptl.x, Ptl.y];
            }
            const eB = () => lerp(bl, br, d0, d1, lvl);   // bottom edge
            const eR = () => lerp(br, tr, d1, d2, lvl);   // right edge
            const eT = () => lerp(tl, tr, d3, d2, lvl);   // top edge
            const eL = () => lerp(bl, tl, d0, d3, lvl);   // left edge

            const segs = [];
            switch (idx) {
              case 1: case 14: segs.push([eL(), eB()]); break;
              case 2: case 13: segs.push([eB(), eR()]); break;
              case 3: case 12: segs.push([eL(), eR()]); break;
              case 4: case 11: segs.push([eR(), eT()]); break;
              case 6: case 9:  segs.push([eB(), eT()]); break;
              case 7: case 8:  segs.push([eL(), eT()]); break;
              case 5:  segs.push([eL(), eT()]); segs.push([eB(), eR()]); break;
              case 10: segs.push([eL(), eB()]); segs.push([eT(), eR()]); break;
              default: break;
            }
            const bucket = segsByLvl[l], lbucket = labelsByLvl[l];
            for (const s of segs) {
              bucket.push(s[0][0], s[0][1], s[1][0], s[1][1]);
              // Sparse inline depth labels along the isobaths.
              if ((labelTick++ % labelEvery) === 0) {
                lbucket.push((s[0][0] + s[1][0]) / 2, (s[0][1] + s[1][1]) / 2);
              }
            }
          }
        }
      }

      // Stroke each level's segments in a single batched path, then its labels.
      for (let l = 0; l < nLv; l++) {
        const seg = segsByLvl[l];
        if (!seg.length && !labelsByLvl[l].length) continue;
        const lvl = levels[l];
        // Thin, semi-transparent WHITE isobaths (Navionics / nautical-chart
        // style): clean accents over the colour-shaded surface. Major lines
        // (every 5 m) read stronger; a bold warm SHOAL/safety contour flags
        // shallow water (a praised Humminbird-LakeMaster-style feature).
        const major = lvl % 5 === 0;
        const safety = lvl <= 3;            // shoal-warning isobath (<= 3 m)
        if (safety) {
          ctx2.strokeStyle = "rgba(255,110,80,0.92)"; ctx2.lineWidth = 1.8;
        } else {
          ctx2.strokeStyle = major ? "rgba(255,255,255,0.6)" : "rgba(255,255,255,0.38)";
          ctx2.lineWidth = major ? 1.0 : 0.7;
        }
        // Chain the disjoint marching-squares segments into polylines, then
        // Chaikin-smooth (2 passes) so the blocky lattice zigzag becomes a smooth
        // isobath curve before stroking. (#chaikin)
        ctx2.beginPath();
        const lines = chainSegments(seg);
        for (let li = 0; li < lines.length; li++) {
          const poly = chaikin(lines[li], 2);
          if (poly.length < 4) continue;
          ctx2.moveTo(poly[0], poly[1]);
          for (let p = 2; p < poly.length; p += 2) ctx2.lineTo(poly[p], poly[p + 1]);
        }
        ctx2.stroke();
        const labs = labelsByLvl[l];
        if (labs.length && (major || safety)) {   // label major + shoal isobaths
          const txt = lvl.toFixed(0);
          ctx2.save();
          ctx2.globalAlpha = 0.9;
          ctx2.fillStyle = "rgba(255,255,255,0.95)";
          ctx2.lineWidth = 3; ctx2.strokeStyle = "rgba(0,0,0,0.6)";
          for (let p = 0; p < labs.length; p += 2) {
            ctx2.strokeText(txt, labs[p], labs[p + 1]);
            ctx2.fillText(txt, labs[p], labs[p + 1]);
          }
          ctx2.restore();
        }
      }
    },
  }));
  const contourLayer = new ContourLayer();
  let contourShow = false;       // Depth contours overlay on/off (default off)

  // ---- bottom-composition overlay (composition polygons) ----------
  // Composition is a VECTOR POLYGON layer (RENDERING_COMPOSITION.md §3/§5):
  // FILLED polygons coloured by pct on a sequential YlOrBr ramp (0..100, FIXED,
  // uncalibrated -- no substrate names, polarity unknown), fill-opacity ~0.5 with
  // a thin stroke, drawn UNDER the contour lines. Distinct from BOTH the depth
  // palette AND the 0..127 hardness ramp -- never conflate them. Never rasterise
  // it and never fill bare areas -- patchy coverage is missing data, not 0%.
  const COMPOSITION_STOPS = [
    [0.0, [255, 255, 229]], [0.25, [254, 227, 145]], [0.5, [254, 153, 41]],
    [0.75, [217, 95, 14]], [1.0, [153, 52, 4]],
  ];
  function compositionColorRGB(pct) {
    const f = Math.max(0, Math.min(1, (+pct || 0) / 100));   // fixed 0..100 domain
    const stops = COMPOSITION_STOPS;
    let a = stops[0], b = stops[stops.length - 1];
    for (let i = 0; i < stops.length - 1; i++) {
      if (f >= stops[i][0] && f <= stops[i + 1][0]) { a = stops[i]; b = stops[i + 1]; break; }
    }
    const tt = b[0] === a[0] ? 0 : (f - a[0]) / (b[0] - a[0]);
    return a[1].map((ch, i) => Math.round(ch + (b[1][i] - ch) * tt));
  }
  // Render composition UNDER the contour lines: its own pane below overlayPane
  // (tilePane 200 < composition 350 < overlayPane 400 where contours live).
  if (!map.getPane("composition")) {
    map.createPane("composition");
    const cp = map.getPane("composition");
    cp.style.zIndex = "350";
    cp.style.pointerEvents = "none";
  }
  const CompositionLayer = L.Layer.extend(Object.assign({}, CanvasOverlayMixin, {
    onAdd(m) {
      this._map = m;
      const c = this._canvas = L.DomUtil.create("canvas", "leaflet-depth-composition");
      c.style.position = "absolute";
      c.style.pointerEvents = "none";
      (m.getPane("composition") || m.getPanes().overlayPane).appendChild(c);
      m.on("moveend zoomend resize", this._scheduleReset, this);
      if (m.options.zoomAnimation) m.on("zoomanim", this._animateZoom, this);
      this._forceDraw = true;
      this._reset();
    },
    onRemove(m) {
      m.off("moveend zoomend resize", this._scheduleReset, this);
      m.off("zoomanim", this._animateZoom, this);
      this._cancelReset();
      if (this._canvas && this._canvas.parentNode) this._canvas.parentNode.removeChild(this._canvas);
      this._canvas = null;
    },
    setData(polys) { this._polys = Array.isArray(polys) ? polys : []; this._forceDraw = true; this._scheduleReset(); },
    _reset() {
      const c = this._canvas, m = this._map;
      if (!c || !m) return;
      const size = m.getSize();
      L.DomUtil.setPosition(c, m.containerPointToLayerPoint([0, 0]));
      const dpr = window.devicePixelRatio || 1;
      if (c.width !== size.x * dpr || c.height !== size.y * dpr) {
        c.width = size.x * dpr; c.height = size.y * dpr;
        c.style.width = size.x + "px"; c.style.height = size.y + "px";
      }
      this._draw(size, dpr);
    },
    _draw(size, dpr) {
      const c = this._canvas, m = this._map;
      const ctx2 = c.getContext("2d");
      ctx2.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx2.clearRect(0, 0, size.x, size.y);
      const polys = this._polys;
      if (!polys || !polys.length) return;
      const b = m.getBounds().pad(0.25);
      const bS = b.getSouth(), bN = b.getNorth(), bW = b.getWest(), bE = b.getEast();
      const off = VA.depthOffset ? VA.depthOffset() : { lat: 0, lon: 0 };
      // Group visible polygons by pct (5% steps -> ~20 groups) and fill ONCE per
      // group: thousands of individual fills would peg the browser. One fill per
      // pct also keeps alpha uniform within a group (no internal double-darkening).
      const byPct = new Map();
      for (const poly of polys) {
        const ring = poly.ring;
        if (!ring || ring.length < 3) continue;
        let vis = false;
        for (let k = 0; k < ring.length; k++) {
          if (ring[k][0] >= bS && ring[k][0] <= bN && ring[k][1] >= bW && ring[k][1] <= bE) { vis = true; break; }
        }
        if (!vis) continue;
        let arr = byPct.get(poly.pct);
        if (!arr) { arr = []; byPct.set(poly.pct, arr); }
        arr.push(ring);
      }
      ctx2.save();
      clipToWaterMask(ctx2, m);               // never paint composition over land
      ctx2.globalAlpha = 0.5;                 // fill-opacity ~0.5 so basemap/depth read through (spec §5)
      ctx2.lineWidth = 0.6;                   // thin stroke on each polygon (spec §5)
      ctx2.lineJoin = "round";
      for (const [pct, rings] of byPct) {
        const rgb = compositionColorRGB(pct);
        ctx2.fillStyle = `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
        // Thin stroke: a darkened shade of the same fill so boundaries read
        // without introducing a second (conflatable) colour scale.
        ctx2.strokeStyle = `rgb(${Math.round(rgb[0] * 0.6)},${Math.round(rgb[1] * 0.6)},${Math.round(rgb[2] * 0.6)})`;
        ctx2.beginPath();
        for (const ring of rings) {
          const p0 = m.latLngToContainerPoint([ring[0][0] + off.lat, ring[0][1] + off.lon]);
          ctx2.moveTo(p0.x, p0.y);
          for (let k = 1; k < ring.length; k++) {
            const p = m.latLngToContainerPoint([ring[k][0] + off.lat, ring[k][1] + off.lon]);
            ctx2.lineTo(p.x, p.y);
          }
          ctx2.closePath();
        }
        ctx2.fill();
        ctx2.stroke();
      }
      ctx2.restore();
    },
  }));
  const compositionLayer = new CompositionLayer();
  let compositionShow = false;   // Composition overlay on/off (default off)
  let compositionBusy = false;
  let waterMaskBusy = false;
  // The bbox (padded viewport) the current waterMask was fetched for. The mask
  // is a slowly-changing visual clip, so we fetch it at most once per region:
  // only when the padded viewport leaves the extent the current mask covers.
  let waterMaskBBox = null;      // {w,s,e,n} or null = never fetched
  async function fetchWaterMask() {
    if (waterMaskBusy) return;               // dedupe concurrent fetches from the 3 overlays
    waterMaskBusy = true;
    try {
      const b = map.getBounds().pad(0.3);
      waterMaskBBox = { w: b.getWest(), s: b.getSouth(), e: b.getEast(), n: b.getNorth() };
      const r = await VA.getJSON(
        "/api/depth/water?west=" + b.getWest() + "&south=" + b.getSouth() +
        "&east=" + b.getEast() + "&north=" + b.getNorth());
      waterMask = (r && r.ok && Array.isArray(r.water) && r.water.length) ? r.water : null;
      waterMaskVersion++;          // invalidate the cached clip Path2D (new mask)
    } catch (e) {
      waterMask = null;   // no water available -> draw unclipped (graceful)
      waterMaskVersion++;          // invalidate the cached clip Path2D
    } finally {
      waterMaskBusy = false;
    }
    // Redraw whichever overlays are showing, now with (or without) the water clip.
    for (const [show, layer] of [[compositionShow, compositionLayer],
                                 [depthShow, gridLayer], [contourShow, contourLayer]]) {
      if (show && map.hasLayer(layer)) { layer._forceDraw = true; layer._scheduleReset(); }
    }
  }
  // Fetch the water clip only when the current padded viewport is NOT already
  // covered by the bbox the mask was last fetched for. Called from the overlay
  // fetch paths instead of unconditionally hitting /api/depth/water every 4 s
  // poll + every move (the mask is a slowly-changing visual clip).
  function maybeFetchWaterMask() {
    const wb = waterMaskBBox;
    if (waterMask && wb) {
      const b = map.getBounds().pad(0.3);
      // Already-covered: viewport fully inside the fetched extent -> reuse.
      if (b.getWest() >= wb.w && b.getSouth() >= wb.s &&
          b.getEast() <= wb.e && b.getNorth() <= wb.n) return;
    }
    fetchWaterMask();
  }
  // Below this zoom, the composition/contour queries + polygon/isobath rendering
  // cover a huge low-zoom viewport and are the dominant frame cost, so we gate
  // them: skip the fetch, clear the layer, and hint the user to zoom in. The
  // zoomend flow (redrawDepth) re-invokes these fetches, so zooming past the
  // gate restores the overlay automatically. (perf)
  const DEPTH_MIN_ZOOM = 13;
  const COMPOSITION_CAP_DEFAULT = "composition (0–100%, uncalibrated)";
  // Reuse existing legend/state DOM for the hint rather than adding new nodes.
  function compHint(msg) {
    const cap = document.querySelector("#composition-legend .depth-legend-cap");
    if (cap) cap.textContent = msg || COMPOSITION_CAP_DEFAULT;
  }
  function contourHint(msg) {
    // The contour overlay has no dedicated legend; borrow the Depth-map state
    // badge, but only when the heatmap isn't using it for its soundings count.
    const badge = document.getElementById("depth-state");
    if (badge && !depthShow) badge.textContent = msg || "";
  }
  async function fetchComposition() {
    if (!compositionShow || compositionBusy) return;
    if (map.getZoom() < DEPTH_MIN_ZOOM) {   // zoom gate (perf): clear + hint
      compositionLayer.setData([]);
      compHint("zoom in for composition");
      return;
    }
    compositionBusy = true;
    try {
      const b = map.getBounds().pad(0.3);
      const r = await VA.getJSON(
        "/api/depth/composition?west=" + b.getWest() + "&south=" + b.getSouth() +
        "&east=" + b.getEast() + "&north=" + b.getNorth());
      const ps = r && Array.isArray(r.polygons) ? r.polygons : [];
      if (!map.hasLayer(compositionLayer)) compositionLayer.addTo(map);
      compositionLayer.setData(ps);
      // Server may cap the payload (?limit=) and flag it — same zoom-in hint.
      compHint(r && r.truncated ? "partial — zoom in" : "");
    } catch (e) { /* leave the last good render */ }
    finally { compositionBusy = false; }
    maybeFetchWaterMask();   // async: clips when the water arrives; reused per region
  }
  const compositionProxy = L.layerGroup();   // fronts the layers-panel checkbox
  let compositionSyncing = false;
  function setCompositionShow(on) {
    compositionShow = !!on;
    const legend = document.getElementById("composition-legend");
    if (legend) legend.classList.toggle("hidden", !compositionShow);
    if (compositionShow) {
      fetchComposition();
    } else {
      if (map.hasLayer(compositionLayer)) map.removeLayer(compositionLayer);
      compositionLayer.setData([]);
    }
    compositionSyncing = true;
    if (compositionShow && !map.hasLayer(compositionProxy)) compositionProxy.addTo(map);
    else if (!compositionShow && map.hasLayer(compositionProxy)) map.removeLayer(compositionProxy);
    compositionSyncing = false;
  }
  VA.setCompositionShow = setCompositionShow;

  let gridOk = false;            // true once a grid fetch has succeeded
  let gridTimer = null;
  let gridBusy = false;

  // Pick a cell size (metres) from the current zoom: small cells when zoomed
  // in for detail, larger when zoomed out so the payload stays light.
  function cellMForZoom() {
    const z = map.getZoom();
    if (z >= 19) return 1;
    if (z >= 18) return 2;
    if (z >= 17) return 3;
    if (z >= 16) return 5;
    if (z >= 15) return 8;
    if (z >= 14) return 15;
    if (z >= 13) return 30;
    return 60;
  }

  function setLegend(min, max, count) {
    VA.setText("depth-legend-min", Number.isFinite(min) ? min.toFixed(1) : "—");
    VA.setText("depth-legend-max", Number.isFinite(max) ? max.toFixed(1) : "—");
    const badge = document.getElementById("depth-state");
    if (badge) badge.textContent = depthShow && count != null ? "● " + count : "";
  }

  async function fetchDepthGrid() {
    // The same grid fetch feeds both the colored heatmap (Depth map) and the
    // isobath overlay (Depth contours); run while either is on.
    if ((!depthShow && !contourShow) || gridBusy) return;
    gridBusy = true;
    try {
      const cellM = cellMForZoom();
      // Tier-1: fetch only the viewport (+30% scroll margin), not the whole chart.
      const b = map.getBounds().pad(0.3);
      const g = await VA.getJSON(
        "/api/depth/grid?cell_m=" + cellM + "&field=" + gridField +
        "&west=" + b.getWest() + "&south=" + b.getSouth() +
        "&east=" + b.getEast() + "&north=" + b.getNorth());
      if (!g || !g.ok || !Array.isArray(g.cells)) throw new Error("no grid");
      gridOk = true;
      _markView();   // remember the viewport we fetched, so pan refetches are gated
      // Hardness colours across the fixed 0..127 index; depth across its data range.
      const isHard = gridField === "hardness";
      const min = isHard ? 0 : (Number.isFinite(g.min_depth) ? g.min_depth : 0);
      const max = isHard ? HARDNESS_MAX : (Number.isFinite(g.max_depth) ? g.max_depth : 1);
      const cm = Number.isFinite(g.cell_m) ? g.cell_m : cellM;
      // Colored heatmap: only mounted when Depth map is on.
      if (depthShow) {
        if (map.hasLayer(depthLayer)) { map.removeLayer(depthLayer); depthLayer.clearLayers(); }
        if (!map.hasLayer(gridLayer)) gridLayer.addTo(map);
        gridLayer.setData(g.cells, min, max, cm);
        setLegend(min, max, Number.isFinite(g.count) ? g.count : g.cells.length);
      }
      // Isobath contours: explicit imported isobaths (fetched separately) take
      // precedence; otherwise derive them from the grid via marching squares.
      // Below the zoom gate the contour overlay is cleared (see fetchContours),
      // so don't let the shared grid poll re-populate it. (perf)
      if (contourShow && !hasExplicitContours && map.getZoom() >= DEPTH_MIN_ZOOM) {
        if (!map.hasLayer(contourLayer)) contourLayer.addTo(map);
        contourLayer.setData(g.cells, min, max, cm);
      }
    } catch (e) {
      // Endpoint absent/failed → fall back to the legacy dot rendering so the
      // overlay keeps working from 5 Hz telemetry. Only do this if we've never
      // seen a good grid (avoid flicker on a transient error).
      if (!gridOk && depthShow) {
        if (map.hasLayer(gridLayer)) map.removeLayer(gridLayer);
        if (!map.hasLayer(depthLayer)) depthLayer.addTo(map);
        renderDepthDotsFrom(VA.last);
      }
    } finally {
      gridBusy = false;
    }
    maybeFetchWaterMask();   // keep the clip current for depth/contours (per region)
  }

  // Explicit imported contours (isobaths) fetched windowed to the
  // viewport, separately from the grid. When present they REPLACE the marching-
  // squares fallback derived from the (sparse) sounding grid.
  let hasExplicitContours = false;
  let contoursBusy = false;
  async function fetchContours() {
    if (!contourShow || contoursBusy) return;
    if (map.getZoom() < DEPTH_MIN_ZOOM) {   // zoom gate (perf): clear + hint
      contourLayer.setExplicit(null);       // drop imported isobaths
      contourLayer.setData([]);             // drop the marching-squares field
      hasExplicitContours = false;
      contourHint("zoom in for contours");
      return;
    }
    contoursBusy = true;
    try {
      const b = map.getBounds().pad(0.3);
      const r = await VA.getJSON(
        "/api/depth/contours?west=" + b.getWest() + "&south=" + b.getSouth() +
        "&east=" + b.getEast() + "&north=" + b.getNorth());
      const cs = r && Array.isArray(r.contours) ? r.contours : [];
      hasExplicitContours = cs.length > 0;
      if (!map.hasLayer(contourLayer)) contourLayer.addTo(map);
      if (hasExplicitContours) {
        let mn = Infinity, mx = -Infinity;
        for (const c of cs) { const d = +c.d; if (d < mn) mn = d; if (d > mx) mx = d; }
        contourLayer.setExplicit(cs, mn, mx);
      } else {
        contourLayer.setExplicit(null);   // none here -> marching-squares fallback
      }
      // Server may cap the payload (?limit=) and flag it — same zoom-in hint.
      contourHint(r && r.truncated ? "partial — zoom in" : "");
    } catch (e) {
      hasExplicitContours = false;
    } finally {
      contoursBusy = false;
    }
    maybeFetchWaterMask();   // keep the clip current for the isobaths (per region)
  }

  // Routine poll: skip the refetch when the view hasn't moved since the last
  // successful fetch (reusing the move-path's _viewMovedEnough/_lastView gate),
  // since the visible window is unchanged. But every Nth poll, refresh
  // unconditionally so newly-recorded soundings still show while stationary.
  let _pollTick = 0;
  const POLL_FORCE_EVERY = 8;   // ~every 32 s, unconditional refresh
  function pollGridTick() {
    _pollTick++;
    if (_pollTick % POLL_FORCE_EVERY === 0) { fetchDepthGrid(); return; }
    if (_viewMovedEnough()) fetchDepthGrid();   // only refetch when the window moved
  }
  function startGridPoll() {
    if (gridTimer) return;
    fetchDepthGrid();
    gridTimer = setInterval(pollGridTick, 4000); // depth map changes slowly
  }
  function stopGridPoll() {
    // Keep polling while either consumer (heatmap or contours) is still on.
    if (depthShow || contourShow) return;
    if (gridTimer) { clearInterval(gridTimer); gridTimer = null; }
  }

  // Redraw the colored grid immediately on zoom; refetch (new cell_m) shortly
  // after so cells re-bucket for the new zoom level.
  function redrawDepth() {
    if (!depthShow && !contourShow && !compositionShow) return;
    if (contourShow) fetchContours();        // re-window the imported isobaths
    if (compositionShow) fetchComposition(); // re-window the composition polygons
    if (gridOk) { fetchDepthGrid(); }
    else if (depthShow) { renderDepthDots(); }
  }
  map.on("zoomend", redrawDepth);
  // Tier-1: panning changes the visible window too -> refetch (debounced). But
  // only when the viewport actually moved a meaningful fraction (or zoom
  // changed): the map following the moving boat fires a moveend every tick, and
  // refetching + redrawing the chart several times a second overwhelms the
  // browser (it crashed a tab). Boat-follow micro-drifts are skipped here; the
  // 4 s grid poll keeps the overlay current as the boat travels.
  let _lastViewC = null, _lastViewZ = null;
  function _markView() { _lastViewC = map.getCenter(); _lastViewZ = map.getZoom(); }
  function _viewMovedEnough() {
    if (!_lastViewC || _lastViewZ !== map.getZoom()) return true;
    const b = map.getBounds(), c = map.getCenter();
    const dy = Math.abs(c.lat - _lastViewC.lat) / Math.max(1e-9, b.getNorth() - b.getSouth());
    const dx = Math.abs(c.lng - _lastViewC.lng) / Math.max(1e-9, b.getEast() - b.getWest());
    return dy > 0.25 || dx > 0.25;
  }
  let _depthMoveTimer = null;
  map.on("moveend", () => {
    if (!depthShow && !contourShow && !compositionShow) return;
    if (_depthMoveTimer) clearTimeout(_depthMoveTimer);
    _depthMoveTimer = setTimeout(() => { if (_viewMovedEnough()) redrawDepth(); }, 300);
  });

  // Per-frame telemetry hook: keeps the readout + soundings count fresh and, in
  // dot-fallback mode, repaints from the live points.
  function updateDepthMap(t) {
    const depth = VA.fin(t.depth_m);
    VA.setText("depth-now", depth === null ? "—" : depth.toFixed(1) + " m");
    // Backend sends `depth_count` (int) on EVERY frame; the full `depth_points`
    // array only ~1 Hz. Use the count for the readout (always present); only act
    // on points when they arrive, otherwise retain the last dot state.
    const cnt = Number.isFinite(t.depth_count) ? t.depth_count : null;
    if (cnt !== null) VA.setText("depth-count", String(cnt));
    if (!depthShow) return;
    // Dot fallback (no grid): repaint only when points are present this frame.
    if (!gridOk && Array.isArray(t.depth_points)) renderDepthDotsFrom(t);
  }

  // A lightweight proxy layer registered in the shared layers control to
  // represent the "Depth map" overlay. The real rendering is the grid/dot
  // canvas that setDepthShow() starts/stops; this proxy just lets the control
  // show a checkbox and fire add/remove. setDepthShow() keeps the proxy's
  // membership in sync so the control reflects the true on/off state.
  const depthProxy = L.layerGroup();
  let depthSyncing = false;  // guard: proxy<->setDepthShow re-entrancy

  function setDepthShow(on) {
    on = !!on;
    depthShow = on;
    const legend = document.getElementById("depth-legend");
    if (legend) legend.classList.toggle("hidden", !on);
    if (on) {
      startGridPoll();   // fetches the grid (or falls back to dots) immediately
    } else {
      stopGridPoll();
      if (map.hasLayer(gridLayer)) map.removeLayer(gridLayer);
      if (map.hasLayer(depthLayer)) map.removeLayer(depthLayer);
      depthLayer.clearLayers();
      setLegend(NaN, NaN, null);
    }
    // Mirror into the Settings checkbox + the control proxy (suppressing the
    // proxy's own add/remove handler so we don't loop back into setDepthShow).
    const box = document.getElementById("depth-show");
    if (box) box.checked = on;
    depthSyncing = true;
    if (on && !map.hasLayer(depthProxy)) depthProxy.addTo(map);
    else if (!on && map.hasLayer(depthProxy)) map.removeLayer(depthProxy);
    depthSyncing = false;
  }

  // Proxy layer fronting the Depth contours control checkbox (mirrors how
  // Depth map uses depthProxy); declared before setContourShow so it can keep
  // the proxy's membership in step with the true on/off state.
  const contourProxy = L.layerGroup();
  let contourSyncing = false;

  // Turn the isobath (Depth contours) overlay on/off. It rides on the same
  // grid poll as the heatmap (shared /api/depth/grid fetch), so we just start
  // the poll if needed and mount/unmount the contour canvas.
  function setContourShow(on) {
    on = !!on;
    contourShow = on;
    if (on) {
      fetchContours();   // prefer explicit imported isobaths (windowed)
      startGridPoll();   // shared with the heatmap; safe to call when running
      if (gridOk) fetchDepthGrid();
    } else {
      if (map.hasLayer(contourLayer)) map.removeLayer(contourLayer);
      contourLayer.setExplicit(null);
      hasExplicitContours = false;
      stopGridPoll();    // no-op while the heatmap is still on
    }
    // Mirror into the control proxy (suppressing its own add/remove handler).
    contourSyncing = true;
    if (on && !map.hasLayer(contourProxy)) contourProxy.addTo(map);
    else if (!on && map.hasLayer(contourProxy)) map.removeLayer(contourProxy);
    contourSyncing = false;
  }

  // ---- relief / hillshade toggle (#relief) -------------------------------
  // Relief is a render MODE of the existing grid layer (the shade is baked into
  // the same offscreen pass), so toggling it just flips the flag and forces the
  // grid to repaint — no separate canvas. A proxy layer fronts the control
  // checkbox so it lives in the unified layers panel alongside Depth/Contours.
  const reliefProxy = L.layerGroup();
  let reliefSyncing = false;
  function repaintGrid() {
    if (depthShow && map.hasLayer(gridLayer)) { gridLayer._forceDraw = true; gridLayer._scheduleReset(); }
  }
  function setReliefShow(on) {
    on = !!on;
    reliefShow = on;
    try { localStorage.setItem(RELIEF_KEY, on ? "1" : "0"); } catch (e) { /* ignore */ }
    repaintGrid();
    const box = document.getElementById("relief-show");
    if (box) box.checked = on;
    reliefSyncing = true;
    if (on && !map.hasLayer(reliefProxy)) reliefProxy.addTo(map);
    else if (!on && map.hasLayer(reliefProxy)) map.removeLayer(reliefProxy);
    reliefSyncing = false;
  }

  // ---- palette + depth-range highlight setters (#palette #highlight) ------
  function setDepthPalette(name) {
    if (!DEPTH_PALETTES[name]) name = "angler";
    depthPalette = name;
    try { localStorage.setItem(PALETTE_KEY, name); } catch (e) { /* ignore */ }
    repaintGrid();
    // The legend bar is a CSS gradient; refresh it to the active ramp.
    refreshLegendRamp();
  }
  function setHighlight(opts) {
    if (opts && typeof opts === "object") {
      if (opts.on !== undefined) hlState.on = !!opts.on;
      if (Number.isFinite(opts.min)) hlState.min = opts.min;
      if (Number.isFinite(opts.max)) hlState.max = opts.max;
    }
    saveHlState();
    repaintGrid();
  }

  // Paint the legend bar with the active palette's ramp so it always matches the
  // map. The legend bar element is .depth-legend-bar inside #depth-legend.
  function refreshLegendRamp() {
    const bar = document.querySelector("#depth-legend .depth-legend-bar");
    if (!bar) return;
    const stops = gridField === "hardness"
      ? HARDNESS_STOPS
      : (DEPTH_PALETTES[depthPalette] || DEPTH_PALETTES.angler).stops;
    const css = stops.map((s) => `rgb(${s[1][0]},${s[1][1]},${s[1][2]}) ${Math.round(s[0] * 100)}%`).join(", ");
    bar.style.background = `linear-gradient(90deg, ${css})`;
    const cap = document.getElementById("depth-legend-cap");
    if (cap) cap.textContent = gridField === "hardness" ? "bottom hardness (0–127)" : "depth (m)";
  }

  // ---- colour-by field: depth vs bottom hardness (#depth-field) -----------
  // The hardness overlay reuses the whole depth-map pipeline (same grid layer,
  // poll and Tier-1 windowing); switching field just changes which layer the
  // server grids, the ramp and the legend. Palette/relief/highlight stay depth.
  function setGridField(field) {
    gridField = field === "hardness" ? "hardness" : "depth";
    refreshLegendRamp();
    if (depthShow) {
      if (gridOk) fetchDepthGrid(); else repaintGrid();
    }
  }

  // ---- depth-overlay alignment nudge (#depth-adjust) ----------------------
  function _adjustHint(msg) {
    const el = document.getElementById("depth-adjust-status");
    if (el) el.textContent = msg || "";
  }
  function setDepthOffset(lat, lon) {
    _depthOffset = { lat: +lat || 0, lon: +lon || 0 };
    try { localStorage.setItem(OFFSET_KEY, JSON.stringify(_depthOffset)); } catch (e) { /* ignore */ }
    repaintGrid();                       // repaint the heatmap at the new alignment
    if (contourShow) fetchContours();    // re-window + redraw contours shifted
  }
  // Two clicks: a point ON the (mis-aligned) overlay, then where it SHOULD sit.
  // The delta is ADDED to the running offset, so it converges with repeated use.
  let _adjustSrc = null;
  function _onAdjustClick(ev) {
    if (!_adjustSrc) {
      _adjustSrc = ev.latlng;
      _adjustHint("Now click where that point SHOULD be on the map.");
      return;
    }
    setDepthOffset(_depthOffset.lat + (ev.latlng.lat - _adjustSrc.lat),
                   _depthOffset.lon + (ev.latlng.lng - _adjustSrc.lng));
    map.off("click", _onAdjustClick);
    map.getContainer().style.cursor = "";
    _adjustSrc = null;
    _adjustHint("Aligned (Δlat " + _depthOffset.lat.toFixed(6) + ", Δlon " +
                _depthOffset.lon.toFixed(6) + "). Adjust again or Reset.");
  }
  function startDepthAdjust() {
    if (!depthShow && !contourShow) { _adjustHint("Turn on the depth or contour overlay first."); return; }
    _adjustSrc = null;
    map.off("click", _onAdjustClick);    // guard against double-arm
    map.on("click", _onAdjustClick);
    map.getContainer().style.cursor = "crosshair";
    _adjustHint("Click a point ON the depth overlay…");
  }
  function resetDepthOffset() {
    map.off("click", _onAdjustClick);
    map.getContainer().style.cursor = "";
    _adjustSrc = null;
    setDepthOffset(0, 0);
    _adjustHint("Alignment reset.");
  }

  // ---- Settings: depth palette + relief + depth-range highlight UI --------
  // Injected into the existing #depth-card so the controls sit with the depth
  // overlay settings. Mirrors the existing inject* pattern; all inputs persist
  // via the setters above and reflect the restored state on load.
  function injectDepthControls() {
    const card = document.getElementById("depth-card");
    if (!card || document.getElementById("depth-enh-controls")) return;
    if (!document.getElementById("depth-enh-css")) {
      const st = document.createElement("style");
      st.id = "depth-enh-css";
      st.textContent = `
        #depth-enh-controls #depth-palette { width: auto; min-width: 110px; margin: 0; }
        #depth-enh-controls #depth-palette { flex: 0 0 auto; }
        #depth-enh-controls .num {
          padding: 6px 8px; font-size: 13px; border-radius: var(--r-sm, 8px);
          color: var(--text, #eaf6fb); background: var(--glass-solid, rgba(255,255,255,0.06));
          border: 1px solid var(--line, rgba(255,255,255,0.12));
        }
        #depth-enh-controls #hl-range { margin-top: 6px; }`;
      document.head.appendChild(st);
    }
    const wrap = document.createElement("div");
    wrap.id = "depth-enh-controls";
    const palOpts = Object.keys(DEPTH_PALETTES)
      .map((k) => `<option value="${k}"${k === depthPalette ? " selected" : ""}>${DEPTH_PALETTES[k].label}</option>`)
      .join("");
    wrap.innerHTML =
      `<div class="row" style="margin-top:8px">
         <label for="depth-field">Colour by</label>
         <select id="depth-field" class="sel">
           <option value="depth"${gridField === "depth" ? " selected" : ""}>Depth</option>
           <option value="hardness"${gridField === "hardness" ? " selected" : ""}>Bottom hardness</option>
         </select>
       </div>
       <div class="row" style="margin-top:8px">
         <label for="depth-palette">Palette</label>
         <select id="depth-palette" class="sel">${palOpts}</select>
       </div>
       <label class="switch"><input type="checkbox" id="relief-show"${reliefShow ? " checked" : ""} /><span class="track"></span> Relief (hillshade)</label>
       <label class="switch"><input type="checkbox" id="hl-on"${hlState.on ? " checked" : ""} /><span class="track"></span> Depth-range highlight</label>
       <div class="row" id="hl-range" style="gap:8px;align-items:center">
         <label style="white-space:nowrap">Band (m)</label>
         <input type="number" id="hl-min" class="num" min="0" max="200" step="0.5" value="${hlState.min}" style="width:64px" />
         <span>–</span>
         <input type="number" id="hl-max" class="num" min="0" max="200" step="0.5" value="${hlState.max}" style="width:64px" />
       </div>
       <div class="hint">Relief shades drop-offs (NW light). The highlight lights up cells whose depth falls in the band (green) and dims the rest — like a Humminbird depth-range highlight.</div>
       <div class="ctx-sub">Alignment</div>
       <div class="btn-row" style="gap:8px">
         <button id="depth-adjust-btn" class="btn-primary">⊹ Adjust…</button>
         <button id="depth-adjust-reset" class="btn-primary">Reset</button>
       </div>
       <div id="depth-adjust-status" class="hint">Nudge a slightly-off imported chart: click a point on the overlay, then where it should be on the map.</div>`;
    card.appendChild(wrap);

    const fld = wrap.querySelector("#depth-field");
    if (fld) fld.addEventListener("change", () => setGridField(fld.value));
    const pal = wrap.querySelector("#depth-palette");
    if (pal) pal.addEventListener("change", () => setDepthPalette(pal.value));
    const rel = wrap.querySelector("#relief-show");
    if (rel) rel.addEventListener("change", () => setReliefShow(rel.checked));
    const hlOn = wrap.querySelector("#hl-on");
    const hlMin = wrap.querySelector("#hl-min");
    const hlMax = wrap.querySelector("#hl-max");
    const pushHl = () => setHighlight({
      on: hlOn.checked,
      min: parseFloat(hlMin.value),
      max: parseFloat(hlMax.value),
    });
    if (hlOn) hlOn.addEventListener("change", pushHl);
    if (hlMin) hlMin.addEventListener("change", pushHl);
    if (hlMax) hlMax.addEventListener("change", pushHl);
    const adj = wrap.querySelector("#depth-adjust-btn");
    if (adj) adj.addEventListener("click", startDepthAdjust);
    const adjR = wrap.querySelector("#depth-adjust-reset");
    if (adjR) adjR.addEventListener("click", resetDepthOffset);
    refreshLegendRamp();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", injectDepthControls);
  } else {
    injectDepthControls();
  }

  // ---- import a depth map (CSV/XYZ or GeoJSON) from Settings --------------
  function wireDepthImport() {
    const btn = document.getElementById("depth-import-btn");
    const fileEl = document.getElementById("depth-import-file");
    const replaceEl = document.getElementById("depth-import-replace");
    const statusEl = document.getElementById("depth-import-status");
    if (!btn || !fileEl) return;
    const setStatus = (msg, kind) => { if (statusEl) { statusEl.textContent = msg || ""; statusEl.className = "hint" + (kind ? " " + kind : ""); } };
    btn.addEventListener("click", () => fileEl.click());
    fileEl.addEventListener("change", async () => {
      const f = fileEl.files && fileEl.files[0];
      if (!f) return;
      setStatus("Importing " + f.name + "…", "busy");
      const fd = new FormData();
      fd.append("file", f, f.name);
      try {
        const replace = replaceEl && replaceEl.checked ? "true" : "false";
        const resp = await fetch("/api/depth/import?replace=" + replace, { method: "POST", body: fd });
        const r = await resp.json();
        if (!resp.ok || !r.ok) { setStatus(r.error || "Import failed.", "err"); }
        else {
          setStatus("Imported " + r.imported + " soundings (" + r.total + " total). Open the depth overlay to view.", "ok");
          const cnt = document.getElementById("depth-count");
          if (cnt && Number.isFinite(r.total)) cnt.textContent = r.total;
          if (typeof fetchDepthGrid === "function") fetchDepthGrid();   // refresh the overlay now
        }
      } catch (e) {
        setStatus("Import failed: " + e, "err");
      } finally {
        fileEl.value = "";   // allow re-importing the same file
      }
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireDepthImport);
  } else {
    wireDepthImport();
  }

  // ---- per-frame render --------------------------------------------------
  VA.onTelemetry(function renderDepth(t) {
    updateDepthMap(t);
  });

  // ---- overlay registration ----------------------------------------------
  // Register the Depth map overlay into the shared control. The proxy's
  // add/remove (from clicking the control checkbox) drives setDepthShow, which
  // in turn mirrors the #depth-show checkbox. The depthSyncing guard prevents
  // the loop when setDepthShow toggles the proxy itself.
  addOverlay("Depth map", depthProxy, {
    persistKey: "depth",
    onToggle(on) {
      if (depthSyncing) return;
      setDepthShow(on);
    },
  });

  // Depth contours (isobaths). The proxy (declared above) fronts the control
  // checkbox the same way Depth map does; toggling it drives setContourShow,
  // which mounts the real contour canvas. Default off; persisted via the
  // unified layers record (restored by restoreLayers if it was on).
  addOverlay("Depth contours", contourProxy, {
    persistKey: "depth-contours",
    onToggle(on) {
      if (contourSyncing) return;
      setContourShow(on);
    },
  });

  // Bottom composition (composition_pct polygons) -- its OWN top-level
  // overlay, separate from the depth heatmap. Filled translucent areas.
  addOverlay("Bottom composition", compositionProxy, {
    persistKey: "depth-composition",
    onToggle(on) {
      if (compositionSyncing) return;
      setCompositionShow(on);
    },
  });

  // Relief / hillshade. A render mode of the Depth map (shade baked into the same
  // offscreen pass); the proxy just fronts the control checkbox. Default ON (the
  // most-praised look); its own pref (RELIEF_KEY) is the source of truth, with
  // the unified layers record kept in sync so it survives via either path.
  addOverlay("Relief (hillshade)", reliefProxy, {
    persistKey: "depth-relief",
    onToggle(on) {
      if (reliefSyncing) return;
      setReliefShow(on);
    },
  });
  // Seed the relief proxy membership from its own pref so the checkbox reflects
  // the default-on state on first load (restoreLayers below only re-adds saved
  // overlays; a brand-new user has no record, so honour RELIEF_KEY here).
  reliefSyncing = true;
  if (reliefShow && !map.hasLayer(reliefProxy)) reliefProxy.addTo(map);
  reliefSyncing = false;

  // Now that the built-in overlays (Sea marks + Reference grid + Depth map +
  // Depth contours + Relief) are registered, restore the saved basemap +
  // overlay selection. Overlays registered later by other modules (Catches,
  // Catch heatmap, No-go zones) restore through their own per-overlay
  // persistence + addOverlay, and saveLayers keeps the combined record current
  // as the user toggles anything.
  const hadSavedPrefs = ctx.restoreLayers();

  // Default-on depth overlay on fresh profiles: if no saved layer selection
  // exists, probe the depth-grid endpoint. If it returns any cells, turn the
  // depth overlay on so "where's the shallow?" is answered out-of-the-box.
  if (!hadSavedPrefs) {
    fetch("/api/depth/grid?fmt=count")
      .then((r) => r.ok ? r.json() : null)
      .catch(() => null)
      .then((data) => {
        // Any truthy cells count means we have charted data for this view.
        const hasCells = data && (
          (typeof data.count === "number" && data.count > 0) ||
          (Array.isArray(data.cells) && data.cells.length > 0)
        );
        if (hasCells) setDepthShow(true);
      });
  }

  // ---- public API (extends VA.map) ---------------------------------------
  Object.assign(VA.map, {
    redrawDepth,
    setDepthShow,
    setContourShow,
    setReliefShow,
    setDepthPalette,
    getDepthPalette() { return depthPalette; },
    setHighlight,
    getHighlight() { return Object.assign({}, hlState); },
  });
})();
