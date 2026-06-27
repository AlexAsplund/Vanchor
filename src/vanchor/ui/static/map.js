/* Vanchor-NG — map module.
 *
 * The Leaflet map is the living backdrop (CARTO dark-matter tiles). Owns the
 * boat marker (glowing vessel + heading + trolling-motor direction indicator),
 * the GPS-fix dot, the anchor marker + watch circle, the live trail, the
 * recorded track, the waypoint/route editor, the tap-to-go-to destination, and
 * the auto depth-map overlay.
 *
 * Exposes a small API on VA.map for app.js (waypoint editing, go-to arming).
 */
"use strict";

(function () {
  const START = [59.66275, 13.32247];
  const map = L.map("map", { zoomControl: false, attributionControl: true, maxZoom: 22 }).setView(START, 16);

  // Selectable basemaps (Satellite gives real detail when zoomed in close).
  // {r} = retina suffix; maxNativeZoom upscales beyond the tiles' native max.
  const OSM = '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
  const CARTO = '© <a href="https://carto.com/attributions">CARTO</a>';
  const base = {
    "Dark": L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
      { maxZoom: 22, maxNativeZoom: 20, attribution: OSM + ", " + CARTO }),
    "Satellite": L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      // maxNativeZoom kept where real imagery exists (remote water/shore has none
      // deeper) so Leaflet UPSCALES the deepest available tile past it instead of
      // fetching Esri's "Map data not available" placeholder.
      { maxZoom: 22, maxNativeZoom: 17, attribution: "© Esri, Maxar, Earthstar Geographics" }),
    "Light": L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
      { maxZoom: 22, maxNativeZoom: 20, attribution: OSM + ", " + CARTO }),
    "Topo": L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
      { maxZoom: 17, attribution: OSM + ", © OpenTopoMap" }),
  };
  base.Dark.addTo(map);

  // Selectable overlays. Sea marks = OpenSeaMap nautical buoys/marks/depths.
  const overlays = {
    "Sea marks": L.tileLayer("https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png",
      { maxZoom: 18, opacity: 0.95, attribution: "© OpenSeaMap" }),
  };

  L.control.zoom({ position: "topleft" }).addTo(map);
  const layersControl = L.control.layers(base, overlays, { position: "topleft", collapsed: true }).addTo(map);

  // ---- unified layers control + persisted selection (#86) ----------------
  // All toggleable overlays (Sea marks, Depth map, Catches, Catch heatmap,
  // No-go zones) live in this one control. The user's basemap + which overlays
  // are on persist in localStorage under LAYERS_KEY, restored on next load.
  const LAYERS_KEY = "vanchor-map-layers";
  // name -> layer for every overlay registered (including the built-in ones),
  // so we can restore saved overlays by name and reason about state.
  const overlayByName = new Map(Object.keys(overlays).map((n) => [n, overlays[n]]));
  // Per-overlay hooks { onToggle?, persistKey? } keyed by overlay name. These
  // let an overlay module keep its own checkbox/pref in sync (see addOverlay).
  const overlayHooks = new Map();
  // Re-entrancy guard: while we sync a module's own toggle in response to a
  // control event (or restore layers), suppress the saveLayers feedback loop.
  let suppressSave = 0;

  function readLayerPrefs() {
    try {
      const raw = localStorage.getItem(LAYERS_KEY);
      if (!raw) return null;
      const o = JSON.parse(raw);
      if (o && typeof o === "object") return o;
    } catch (e) { /* ignore */ }
    return null;
  }
  function saveLayers() {
    if (suppressSave > 0) return;
    let baseName = "Dark";
    Object.keys(base).forEach((n) => { if (map.hasLayer(base[n])) baseName = n; });
    const onOverlays = [];
    overlayByName.forEach((layer, name) => {
      if (layer && map.hasLayer(layer)) onOverlays.push(name);
    });
    try {
      localStorage.setItem(LAYERS_KEY, JSON.stringify({ base: baseName, overlays: onOverlays }));
    } catch (e) { /* ignore */ }
  }

  // Idempotently register an overlay into the shared control. opts may carry
  // { onToggle(on), persistKey }. onToggle is invoked (guarded against
  // re-entrancy) whenever the control adds/removes this overlay, so the owning
  // module can mirror the change into its Settings checkbox + its own pref.
  function addOverlay(name, layer, opts) {
    if (!layer || overlayByName.has(name)) {
      if (opts) overlayHooks.set(name, opts);  // refresh hooks if re-registered
      return layer;
    }
    overlayByName.set(name, layer);
    if (opts) overlayHooks.set(name, opts);
    layersControl.addOverlay(layer, name);
    return layer;
  }

  // Drive the owning module's toggle when the control flips an overlay. We run
  // its onToggle inside the suppressSave guard so the module's own
  // layer.addTo/removeLayer (and checkbox writes) don't recurse into saveLayers
  // mid-handling; saveLayers still runs once at the end via the overlay event.
  function handleOverlayEvent(name, on) {
    const hooks = overlayHooks.get(name);
    if (hooks && typeof hooks.onToggle === "function") {
      suppressSave++;
      try { hooks.onToggle(on); } catch (e) { /* ignore */ }
      suppressSave--;
    }
  }

  map.on("overlayadd", (e) => { handleOverlayEvent(e.name, true); saveLayers(); });
  map.on("overlayremove", (e) => { handleOverlayEvent(e.name, false); saveLayers(); });
  map.on("baselayerchange", saveLayers);

  // Restore the saved basemap + overlay selection. Called once at the end of
  // module setup (after the built-in overlays exist); overlay modules that load
  // later restore themselves through their own persistence and addOverlay.
  function restoreLayers() {
    const prefs = readLayerPrefs();
    if (!prefs) return;
    suppressSave++;
    try {
      if (prefs.base && base[prefs.base] && !map.hasLayer(base[prefs.base])) {
        Object.keys(base).forEach((n) => { if (map.hasLayer(base[n])) map.removeLayer(base[n]); });
        base[prefs.base].addTo(map);
      }
      if (Array.isArray(prefs.overlays)) {
        prefs.overlays.forEach((name) => {
          const layer = overlayByName.get(name);
          if (layer && !map.hasLayer(layer)) layer.addTo(map);
        });
      }
    } catch (e) { /* ignore */ }
    suppressSave--;
  }

  // Faint, geo-anchored REFERENCE GRID. Over featureless dark water the boat's
  // motion is hard to see when zoomed in (nothing to track against); a barely
  // visible grid fixed in geographic space scrolls under the boat so movement
  // reads clearly. Toggleable in the layers panel + persisted; default on.
  const RefGrid = L.GridLayer.extend({
    createTile(coords) {
      const c = document.createElement("canvas");
      const s = this.getTileSize();
      c.width = s.x; c.height = s.y;
      const ctx = c.getContext("2d");
      ctx.strokeStyle = "rgba(150, 200, 225, 0.09)";  // barely visible
      ctx.lineWidth = 1;
      const N = 4, step = s.x / N;          // ~4 lines per 256px tile (steady density)
      ctx.beginPath();
      for (let i = 0; i < N; i++) {
        const p = Math.round(i * step) + 0.5;
        ctx.moveTo(p, 0); ctx.lineTo(p, s.y);
        ctx.moveTo(0, p); ctx.lineTo(s.x, p);
      }
      ctx.stroke();
      return c;
    },
  });
  const refGrid = new RefGrid({ zIndex: 250 });
  addOverlay("Reference grid", refGrid, { onToggle() {} });
  // Default ON for first-time users; an existing saved selection is honoured
  // (restoreLayers re-adds it only if it was on).
  if (!readLayerPrefs()) refGrid.addTo(map);

  // Expose the base layers + their template URLs so the offline-map downloader
  // (#52) can wire an IndexedDB tile cache into createTile and enumerate tiles.
  VA._baseLayers = base;
  VA._baseTemplates = {
    Dark: "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
    Satellite: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    Light: "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    Topo: "https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
  };
  VA._baseNativeMax = { Dark: 20, Satellite: 17, Light: 20, Topo: 17 };

  // Tile-cache integration point. offline.js installs VA.tileCache.get(url) ->
  // Promise<Blob|null> and VA.tileCache.put(url, blob). We patch createTile on
  // each base layer to consult the cache first (and store fetched tiles), so the
  // map keeps working offline. The patch is a no-op until offline.js is loaded.
  Object.keys(base).forEach((name) => {
    const layer = base[name];
    const origCreate = layer.createTile;
    layer.createTile = function (coords, done) {
      const img = document.createElement("img");
      const url = this.getTileUrl(coords);
      img.alt = "";
      img.setAttribute("role", "presentation");
      const cache = VA.tileCache;
      if (!cache || !cache.get) {
        // no cache installed — fall back to default network behaviour
        return origCreate.call(this, coords, done);
      }
      cache.get(url).then((blob) => {
        if (blob) {
          img.src = URL.createObjectURL(blob);
          img.onload = () => { try { URL.revokeObjectURL(img.src); } catch (e) {} done(null, img); };
          img.onerror = () => done(new Error("cached tile decode failed"), img);
          return;
        }
        // not cached: load from network, then store a copy if online.
        img.crossOrigin = "anonymous";
        img.onload = () => {
          done(null, img);
          if (cache.put) {
            fetch(url).then((r) => (r.ok ? r.blob() : null)).then((b) => { if (b) cache.put(url, b); }).catch(() => {});
          }
        };
        img.onerror = () => done(new Error("tile load failed"), img);
        img.src = url;
      }).catch(() => {
        img.src = url;
        img.onload = () => done(null, img);
        img.onerror = () => done(new Error("tile load failed"), img);
      });
      return img;
    };
  });

  // ---- boat icon ---------------------------------------------------------
  // Selectable top-down vessel designs (#84). All are drawn bow-up (= north at
  // heading 0°), share the same 34×48-ish icon box / anchor / scale, keep the
  // root <svg class="boat-icon"> so applyBoatTransform() still finds + rotates +
  // scales it, and carry a shared #motor direction-needle group near the bow so
  // updateMotorIndicator() keeps working. The user picks one in Settings; the
  // choice persists in localStorage (vanchor-boat-icon).
  const BOW_X = 17, BOW_Y = 3.2;

  // Shared trolling-motor direction needle, appended near the bow of every
  // design. updateMotorIndicator() drives #motor-line / #motor-head.
  const MOTOR_G = `
        <circle cx="17" cy="3.2" r="2.4" fill="#04222b" stroke="#bff8ff" stroke-width="0.8"/>
        <g id="motor" transform="rotate(0 17 3.2)" style="visibility:hidden">
          <line id="motor-line" x1="17" y1="3.2" x2="17" y2="3.2"
                stroke="#22d3a6" stroke-width="2.6" stroke-linecap="round"/>
          <polygon id="motor-head" points="0,0 0,0 0,0" fill="#22d3a6"/>
        </g>`;

  // --- design bodies (everything inside <svg>, minus the motor group) -------

  // Current — the original glowing cyan vessel, kept exactly as-is.
  function bodyCurrent() {
    return `
        <defs>
          <linearGradient id="boatHull" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#1be4ff"/>
            <stop offset="1" stop-color="#0a8fb0"/>
          </linearGradient>
        </defs>
        <polygon class="boat-glow" points="17,2 27,18 26,44 8,44 7,18"/>
        <polygon class="boat-hull" points="17,2 27,18 26,44 8,44 7,18"
                 fill="url(#boatHull)" stroke="#bff8ff" stroke-width="1.2" stroke-linejoin="round"/>
        <path d="M17,6 L23,18 L11,18 Z" fill="#ffffff" opacity="0.35"/>
        <rect x="12" y="24" width="10" height="8" rx="2" fill="#04222b" opacity="0.65"/>`;
  }

  // Bass boat — sleek low fishing boat: sharp pointed bow, wide flat casting
  // deck, low gunwales, console + two seats, transom outboard at the stern.
  function bodyBass() {
    return `
        <defs>
          <linearGradient id="bassHull" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#ff4d6d"/>
            <stop offset="1" stop-color="#a3122e"/>
          </linearGradient>
        </defs>
        <!-- outboard at transom (stern, bottom) -->
        <rect x="14.6" y="44" width="4.8" height="4" rx="1" fill="#0c0f14" stroke="#9fb0bd" stroke-width="0.5"/>
        <line x1="17" y1="46" x2="17" y2="48.6" stroke="#9fb0bd" stroke-width="1.4" stroke-linecap="round"/>
        <!-- glow + sharp-bowed hull -->
        <path class="boat-glow" d="M17,1 C24,9 26,16 25.5,30 L24,44 L10,44 L8.5,30 C8,16 10,9 17,1 Z"/>
        <path d="M17,1 C24,9 26,16 25.5,30 L24,44 L10,44 L8.5,30 C8,16 10,9 17,1 Z"
              fill="url(#bassHull)" stroke="#ffd0d8" stroke-width="1.1" stroke-linejoin="round"/>
        <!-- bright flat casting deck up front -->
        <path d="M17,4 C22,10 23,15 22.6,22 L11.4,22 C11,15 12,10 17,4 Z" fill="#f3f6f8" opacity="0.85"/>
        <!-- low gunwale inner line -->
        <path d="M17,6 C22.5,12 24,18 23.4,40 L10.6,40 C10,18 11.5,12 17,6 Z"
              fill="none" stroke="#ffe3e9" stroke-width="0.6" opacity="0.7"/>
        <!-- console -->
        <rect x="13.4" y="25" width="7.2" height="5" rx="1.3" fill="#1a1f26" stroke="#cfd8df" stroke-width="0.5"/>
        <!-- two seats -->
        <circle cx="17" cy="33.5" r="2" fill="#1a1f26" stroke="#cfd8df" stroke-width="0.5"/>
        <circle cx="17" cy="38.5" r="2" fill="#1a1f26" stroke="#cfd8df" stroke-width="0.5"/>`;
  }

  // Titanic — ocean liner: long black hull, pointed bow + rounded stern, white
  // superstructure down the centre, 4 angled funnels, lifeboats along the sides.
  function bodyTitanic() {
    let lifeboats = "";
    for (let i = 0; i < 5; i++) {
      const y = 18 + i * 5;
      lifeboats += `<ellipse cx="11.2" cy="${y}" rx="1" ry="2" fill="#caa64a" stroke="#3a2c0f" stroke-width="0.3"/>`;
      lifeboats += `<ellipse cx="22.8" cy="${y}" rx="1" ry="2" fill="#caa64a" stroke="#3a2c0f" stroke-width="0.3"/>`;
    }
    let funnels = "";
    const fy = [14, 21, 28, 35];
    for (let i = 0; i < 4; i++) {
      // angled (raked) buff funnels with black tops
      funnels += `<g transform="rotate(-12 17 ${fy[i]})">
            <rect x="14.5" y="${fy[i] - 3}" width="5" height="6" rx="1.4" fill="#e8b04b" stroke="#5a4410" stroke-width="0.4"/>
            <rect x="14.5" y="${fy[i] - 3}" width="5" height="1.6" rx="0.8" fill="#15110a"/>
          </g>`;
    }
    return `
        <defs>
          <linearGradient id="titanHull" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#2a2f36"/>
            <stop offset="1" stop-color="#0a0d11"/>
          </linearGradient>
        </defs>
        <!-- long black hull: pointed bow (top), rounded stern (bottom) -->
        <path class="boat-glow" d="M17,1 C21,5 23,9 23,14 L23,40 C23,44 20.5,47 17,47 C13.5,47 11,44 11,40 L11,14 C11,9 13,5 17,1 Z"/>
        <path d="M17,1 C21,5 23,9 23,14 L23,40 C23,44 20.5,47 17,47 C13.5,47 11,44 11,40 L11,14 C11,9 13,5 17,1 Z"
              fill="url(#titanHull)" stroke="#aeb8c2" stroke-width="0.9" stroke-linejoin="round"/>
        <!-- white superstructure down the centre -->
        <rect x="13.5" y="9" width="7" height="32" rx="2.4" fill="#eef2f5" opacity="0.95"/>
        <line x1="17" y1="9" x2="17" y2="41" stroke="#c4ccd2" stroke-width="0.4"/>
        ${lifeboats}
        ${funnels}
        <!-- forward mast hint -->
        <circle cx="17" cy="6.5" r="1.1" fill="#eef2f5"/>`;
  }

  // Narco sub — low-profile semi-submersible: narrow grey hull mostly awash
  // (translucent so it reads as barely above water), a tiny cockpit/intake hump,
  // a faint wake. Sinister and low.
  function bodyNarco() {
    return `
        <defs>
          <linearGradient id="narcoHull" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#5b6670"/>
            <stop offset="1" stop-color="#2b333b"/>
          </linearGradient>
        </defs>
        <!-- faint wake astern -->
        <path d="M13,44 Q17,52 21,44" fill="none" stroke="#7fd9ff" stroke-width="1" opacity="0.25"/>
        <!-- awash outer wash (very translucent) -->
        <path d="M17,4 C20.5,12 21,20 21,30 L20,44 L14,44 L13,30 C13,20 13.5,12 17,4 Z"
              fill="#9fdcff" opacity="0.14"/>
        <!-- narrow low hull, translucent = barely above water -->
        <path class="boat-glow" d="M17,6 C19.6,13 20,20 20,30 L19,43 L15,43 L14,30 C14,20 14.4,13 17,6 Z" style="opacity:0.35"/>
        <path d="M17,6 C19.6,13 20,20 20,30 L19,43 L15,43 L14,30 C14,20 14.4,13 17,6 Z"
              fill="url(#narcoHull)" stroke="#8a99a6" stroke-width="0.7" stroke-linejoin="round" opacity="0.78"/>
        <!-- tiny cockpit / intake hump -->
        <ellipse cx="17" cy="22" rx="2.1" ry="3" fill="#10151a" stroke="#9fb0bd" stroke-width="0.5" opacity="0.95"/>
        <rect x="16.3" y="14" width="1.4" height="3" rx="0.5" fill="#0a0e12"/>`;
  }

  // Yellow submarine — Beatles-style: rounded yellow hull, conning tower with a
  // periscope, round portholes along the side, tail fins + a hint of propeller.
  function bodyYellowSub() {
    let ports = "";
    for (let i = 0; i < 4; i++) {
      const y = 16 + i * 6;
      ports += `<circle cx="17" cy="${y}" r="1.5" fill="#bfeaff" stroke="#1c3a52" stroke-width="0.6"/>`;
    }
    return `
        <defs>
          <linearGradient id="subHull" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#ffe14d"/>
            <stop offset="1" stop-color="#e0a400"/>
          </linearGradient>
        </defs>
        <!-- propeller hint at stern -->
        <line x1="13.5" y1="46" x2="20.5" y2="46" stroke="#cfd8df" stroke-width="1.2" stroke-linecap="round"/>
        <circle cx="17" cy="46" r="1" fill="#9fb0bd"/>
        <!-- tail fins -->
        <path d="M9,40 L14,43 L14,45 Z" fill="#caa400"/>
        <path d="M25,40 L20,43 L20,45 Z" fill="#caa400"/>
        <!-- rounded yellow hull -->
        <path class="boat-glow" d="M17,2 C22,8 22,12 22,24 C22,36 21,44 17,46 C13,44 12,36 12,24 C12,12 12,8 17,2 Z"/>
        <path d="M17,2 C22,8 22,12 22,24 C22,36 21,44 17,46 C13,44 12,36 12,24 C12,12 12,8 17,2 Z"
              fill="url(#subHull)" stroke="#fff4c2" stroke-width="1.1" stroke-linejoin="round"/>
        <!-- conning tower -->
        <rect x="14.4" y="20" width="5.2" height="8" rx="2.2" fill="#ffd400" stroke="#8a6a00" stroke-width="0.7"/>
        <!-- periscope -->
        <line x1="17" y1="20" x2="17" y2="14" stroke="#5a4400" stroke-width="1.3" stroke-linecap="round"/>
        <line x1="17" y1="14" x2="19" y2="14" stroke="#5a4400" stroke-width="1.3" stroke-linecap="round"/>
        ${ports}`;
  }

  const BOAT_DESIGNS = {
    current: { label: "Current", body: bodyCurrent },
    bass: { label: "Bass boat", body: bodyBass },
    titanic: { label: "Titanic", body: bodyTitanic },
    narco: { label: "Narco sub", body: bodyNarco },
    yellowsub: { label: "Yellow submarine", body: bodyYellowSub },
  };

  const BOAT_ICON_KEY = "vanchor-boat-icon";
  function getBoatIconId() {
    try {
      const v = localStorage.getItem(BOAT_ICON_KEY);
      if (v && BOAT_DESIGNS[v]) return v;
    } catch (e) { /* ignore */ }
    return "current";
  }
  let boatIconId = getBoatIconId();

  function boatDiv(id) {
    const design = BOAT_DESIGNS[id] || BOAT_DESIGNS.current;
    const svg = `
      <svg width="34" height="48" viewBox="0 0 34 48" class="boat-icon"
           style="transform-origin:50% 50%; overflow:visible">
        ${design.body()}
        ${MOTOR_G}
      </svg>`;
    return L.divIcon({ className: "", html: svg, iconSize: [34, 48], iconAnchor: [17, 24] });
  }

  function updateMotorIndicator(svgEl, motor) {
    const g = svgEl.querySelector("#motor");
    if (!g) return;
    const line = g.querySelector("#motor-line");
    const head = g.querySelector("#motor-head");
    const ang = motor && Number.isFinite(motor.steer_angle_deg) ? motor.steer_angle_deg : null;
    const thrust = motor && Number.isFinite(motor.thrust) ? motor.thrust : null;
    if (ang === null || thrust === null || Math.abs(thrust) < 0.02) {
      g.style.visibility = "hidden";
      return;
    }
    g.style.visibility = "visible";
    g.setAttribute("transform", `rotate(${ang} ${BOW_X} ${BOW_Y})`);
    const color = thrust < 0 ? "#ffb454" : "#22d3a6"; // reverse = amber
    const len = 5 + Math.min(1, Math.abs(thrust)) * 16;
    const tipY = BOW_Y - len;
    line.setAttribute("x2", BOW_X);
    line.setAttribute("y2", tipY);
    line.setAttribute("stroke", color);
    const w = 3.4;
    head.setAttribute("points",
      `${BOW_X - w},${tipY + w} ${BOW_X + w},${tipY + w} ${BOW_X},${tipY - w}`);
    head.setAttribute("fill", color);
  }

  const boatMarker = L.marker(START, { icon: boatDiv(boatIconId), zIndexOffset: 1000 }).addTo(map);
  const gpsMarker = L.circleMarker(START, {
    radius: 4, color: "#1be4ff", fillColor: "#1be4ff", fillOpacity: 0.9, weight: 1,
  }).addTo(map).bindTooltip("GPS fix");
  let _gpsLat = null, _gpsLon = null;  // low-passed GPS dot position (display only)

  let anchorMarker = null, anchorCircle = null, lastAnchor = null;
  let gotoMarker = null, gotoArmed = false;
  const wpLayer = L.layerGroup().addTo(map);
  const pendingLayer = L.layerGroup().addTo(map);  // draggable, rebuilt on change only
  let routeLine = null;
  const depthLayer = L.layerGroup();
  let depthShow = false;
  const trail = L.polyline([], { color: "#1be4ff", weight: 2, opacity: 0.45 }).addTo(map);
  const trailPts = [];
  let trackLine = null;
  let pendingWaypoints = [];      // [{name, lat, lon}]
  let followBoat = true;
  let followOffsetX = 0, followOffsetY = 0; // shift the followed boat off-centre (clear of the setup-wizard panel)
  let wpSeq = 0;
  let lastCommitted = [], lastActiveIx = -1;

  map.on("dragstart", () => { followBoat = false; });

  // ---- boat icon scaling -------------------------------------------------
  // The boat is a fixed-pixel icon by default, so it looks tiny when zoomed
  // in. Scale it to the boat's REAL length (a minimum size floor so it never
  // vanishes, but no maximum so it grows true-to-scale as you zoom in).
  const BASE_ICON_PX = 48; // icon height in px = the minimum on-screen size
  let _boatEl = null, _boatLat = null, _boatHdg = 0;
  function boatScale(lat) {
    const z = map.getZoom();
    const mpp = (40075016.686 * Math.cos((lat * Math.PI) / 180)) / Math.pow(2, z + 8);
    const lenM = (VA.last && VA.last.boat && VA.last.boat.length_m) || 4.1;
    return Math.max(1, lenM / mpp / BASE_ICON_PX);
  }
  function applyBoatTransform() {
    if (!_boatEl || _boatLat === null) return;
    const rot = VA.continuousAngle("boat", _boatHdg);
    _boatEl.style.transform = `rotate(${rot}deg) scale(${boatScale(_boatLat).toFixed(3)})`;
  }
  map.on("zoomend", applyBoatTransform);

  // ---- boat icon design picker (#84) -------------------------------------
  // Rebuild the boat marker's icon with the selected design, then re-grab the
  // fresh SVG element and re-apply heading/zoom transform + the motor needle so
  // it updates live (no reload). setIcon() replaces the DOM node, so the cached
  // _boatEl must be refreshed.
  function setBoatIcon(id) {
    if (!BOAT_DESIGNS[id]) id = "current";
    boatIconId = id;
    try { localStorage.setItem(BOAT_ICON_KEY, id); } catch (e) { /* ignore */ }
    boatMarker.setIcon(boatDiv(id));
    const el = boatMarker.getElement()?.querySelector(".boat-icon");
    if (el) {
      _boatEl = el;
      applyBoatTransform();
      updateMotorIndicator(el, VA.last && VA.last.motor);
    }
  }

  // Inject a themed "Boat icon" picker card into the Settings drawer. Uses the
  // existing CSS custom props / card styling so it matches the rest of the UI.
  function injectBoatIconPicker() {
    const host = document.querySelector("#settings .drawer-body") || document.getElementById("settings");
    if (!host || document.getElementById("boat-icon-picker")) return;

    if (!document.getElementById("boat-icon-picker-css")) {
      const st = document.createElement("style");
      st.id = "boat-icon-picker-css";
      st.textContent = `
        #boat-icon-picker .boat-icon-grid {
          display: grid; grid-template-columns: repeat(auto-fit, minmax(84px, 1fr));
          gap: 8px; margin-top: 8px;
        }
        #boat-icon-picker .boat-choice {
          display: flex; flex-direction: column; align-items: center; gap: 4px;
          padding: 8px 4px; cursor: pointer;
          background: var(--glass, rgba(255,255,255,0.04));
          border: 1px solid var(--line, rgba(255,255,255,0.12));
          border-radius: var(--r, 10px);
          color: var(--muted, #9fb0bd); font-size: 11px; text-align: center;
          transition: border-color .15s, color .15s, background .15s;
        }
        #boat-icon-picker .boat-choice:hover { border-color: var(--accent, #1be4ff); }
        #boat-icon-picker .boat-choice.sel {
          border-color: var(--accent, #1be4ff);
          color: var(--text, #eaf6fb);
          box-shadow: 0 0 0 1px var(--accent, #1be4ff) inset;
        }
        #boat-icon-picker .boat-choice svg { display: block; height: 40px; width: auto; }
        #boat-icon-picker .boat-choice input { display: none; }`;
      document.head.appendChild(st);
    }

    const card = document.createElement("div");
    card.className = "card";
    card.id = "boat-icon-picker";
    const head = document.createElement("div");
    head.className = "summary";
    head.textContent = "Boat icon";
    card.appendChild(head);

    const grid = document.createElement("div");
    grid.className = "boat-icon-grid";
    Object.keys(BOAT_DESIGNS).forEach((id) => {
      const d = BOAT_DESIGNS[id];
      const label = document.createElement("label");
      label.className = "boat-choice" + (id === boatIconId ? " sel" : "");
      label.dataset.id = id;
      // Small upright preview (no rotation/scale; reuse the design body).
      label.innerHTML =
        `<svg width="34" height="48" viewBox="0 0 34 48" style="overflow:visible">${d.body()}</svg>` +
        `<input type="radio" name="boat-icon" value="${id}"${id === boatIconId ? " checked" : ""}>` +
        `<span>${d.label}</span>`;
      label.addEventListener("click", () => {
        setBoatIcon(id);
        grid.querySelectorAll(".boat-choice").forEach((c) => c.classList.toggle("sel", c.dataset.id === id));
        const input = label.querySelector("input");
        if (input) input.checked = true;
      });
      grid.appendChild(label);
    });
    card.appendChild(grid);
    host.appendChild(card);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", injectBoatIconPicker);
  } else {
    injectBoatIconPicker();
  }

  // ---- waypoints / route -------------------------------------------------
  // Active-route waypoints (committed, from telemetry) are draggable so the user
  // can edit a running route live; on edit we re-send the whole route. We keep a
  // mutable local copy (editCommitted) that telemetry only overwrites when the
  // user isn't mid-edit, so a 5 Hz frame can't clobber a drag or the long-press
  // menu. (#51)
  let editCommitted = [];     // [{name,lat,lon}] live-editable copy
  let editing = false;        // true while dragging / menu open (suppress sync)
  let onRouteEdit = null;     // (waypoints) => void  — re-send the edited route

  function sameWps(a, b) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) {
      if (Math.abs(a[i].lat - b[i].lat) > 1e-9 || Math.abs(a[i].lon - b[i].lon) > 1e-9 ||
        (a[i].name || "") !== (b[i].name || "")) return false;
    }
    return true;
  }

  let lastDrawnActive = -2;
  function drawWaypoints(committed, activeIx) {
    let changed = false;
    if (committed !== undefined && committed !== null) {
      lastCommitted = committed;
      // Sync the editable copy from telemetry only when not mid-edit and the
      // server route actually differs (so user drags aren't fought).
      if (!editing) {
        const norm = committed.map((w, i) => ({ name: w.name || "WP" + (i + 1), lat: w.lat, lon: w.lon }));
        if (!sameWps(editCommitted, norm)) { editCommitted = norm; changed = true; }
      }
    } else {
      // Direct call (after a local edit) — always rebuild from editCommitted.
      changed = true;
    }
    if (activeIx !== undefined && activeIx !== null && activeIx !== lastActiveIx) {
      lastActiveIx = activeIx; changed = true;
    }
    const wps = editCommitted, active = lastActiveIx;
    // Only rebuild the draggable markers when the set/active index changed, so a
    // 5 Hz frame can't interrupt a long-press or thrash the DOM. (#51)
    if (changed || active !== lastDrawnActive) {
      lastDrawnActive = active;
      wpLayer.clearLayers();
      wps.forEach((w, i) => {
        const m = L.marker([w.lat, w.lon], {
          icon: committedIcon(i + 1, i === active), draggable: true, autoPan: true, zIndexOffset: 700,
        }).addTo(wpLayer).bindTooltip(w.name || "WP" + (i + 1));
        wireCommittedMarker(m, i);
      });
    }
    // Route line (cheap) tracks pending + committed every call.
    const pts = wps.map((w) => [w.lat, w.lon]).concat(pendingWaypoints.map((w) => [w.lat, w.lon]));
    if (!routeLine) routeLine = L.polyline(pts, { color: "#1be4ff", weight: 2, dashArray: "5,6", opacity: 0.7 }).addTo(map);
    else routeLine.setLatLngs(pts);
  }

  function committedIcon(label, active) {
    return L.divIcon({
      className: "",
      html: `<div class="wp-pin wp-pin-active${active ? " wp-pin-now" : ""}">${label}</div>`,
      iconSize: [22, 22], iconAnchor: [11, 11],
    });
  }

  // Re-send the edited committed route through the same path startRoute uses.
  function sendRouteEdit() {
    const wps = editCommitted.map((w, i) => ({ name: w.name || "WP" + (i + 1), lat: w.lat, lon: w.lon }));
    if (onRouteEdit) onRouteEdit(wps);
  }

  // Drag to move; press-and-hold ~3 s (held still) opens an edit menu.
  const LONGPRESS_MS = 3000, MOVE_TOL = 8; // px
  function wireCommittedMarker(m, ix) {
    let lpTimer = null, downPt = null, dragged = false;

    const clearLp = () => { if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; } };

    m.on("dragstart", () => { editing = true; dragged = true; clearLp(); closeWpMenu(); });
    m.on("drag", (e) => {
      const ll = e.target.getLatLng();
      if (editCommitted[ix]) { editCommitted[ix].lat = ll.lat; editCommitted[ix].lon = ll.lng; }
      // redraw the route line live without rebuilding markers
      const pts = editCommitted.map((w) => [w.lat, w.lon]).concat(pendingWaypoints.map((w) => [w.lat, w.lon]));
      if (routeLine) routeLine.setLatLngs(pts);
    });
    m.on("dragend", () => { editing = false; sendRouteEdit(); drawWaypoints(); });

    // Long-press detection on the marker element (pointer events).
    const el = m.getElement && m.getElement();
    const startLp = (clientX, clientY) => {
      dragged = false;
      downPt = { x: clientX, y: clientY };
      clearLp();
      lpTimer = setTimeout(() => {
        if (!dragged) { editing = true; openWpMenu(m, ix); }
      }, LONGPRESS_MS);
    };
    const moveLp = (clientX, clientY) => {
      if (!downPt) return;
      if (Math.hypot(clientX - downPt.x, clientY - downPt.y) > MOVE_TOL) clearLp();
    };
    const node = el || (m._icon);
    if (node) {
      node.addEventListener("pointerdown", (ev) => startLp(ev.clientX, ev.clientY));
      node.addEventListener("pointermove", (ev) => moveLp(ev.clientX, ev.clientY));
      ["pointerup", "pointercancel", "pointerleave"].forEach((t) => node.addEventListener(t, clearLp));
    }
  }

  // ---- long-press waypoint edit menu -------------------------------------
  let wpMenu = null;
  function closeWpMenu() {
    if (wpMenu) { wpMenu.remove(); wpMenu = null; editing = false; }
  }
  function openWpMenu(marker, ix) {
    closeWpMenu();
    editing = true;
    const pt = map.latLngToContainerPoint(marker.getLatLng());
    const menu = document.createElement("div");
    menu.className = "wp-menu glass";
    menu.innerHTML =
      `<div class="wp-menu-title">Waypoint ${ix + 1}</div>` +
      `<button type="button" data-act="before">Add waypoint before</button>` +
      `<button type="button" data-act="after">Add waypoint after</button>` +
      `<button type="button" data-act="delete" class="danger">Delete waypoint</button>` +
      `<button type="button" data-act="cancel" class="cancel">Cancel</button>`;
    menu.style.left = pt.x + "px";
    menu.style.top = pt.y + "px";
    const host = map.getContainer();
    host.appendChild(menu);
    wpMenu = menu;

    menu.querySelectorAll("button").forEach((b) => b.addEventListener("click", (e) => {
      e.stopPropagation();
      const act = b.dataset.act;
      if (act === "before" || act === "after") {
        const base = editCommitted[ix] || editCommitted[editCommitted.length - 1];
        const neighbor = act === "after"
          ? (editCommitted[ix + 1] || base)
          : (editCommitted[ix - 1] || base);
        // place the new waypoint midway toward the neighbour (or a small nudge)
        const nlat = base ? (base.lat + (neighbor ? neighbor.lat : base.lat)) / 2 : base.lat;
        const nlon = base ? (base.lon + (neighbor ? neighbor.lon : base.lon)) / 2 : base.lon;
        const insertAt = act === "after" ? ix + 1 : ix;
        editCommitted.splice(insertAt, 0, { name: "WP" + (++wpSeq), lat: nlat, lon: nlon });
      } else if (act === "delete") {
        editCommitted.splice(ix, 1);
      }
      closeWpMenu();
      if (act !== "cancel") { sendRouteEdit(); drawWaypoints(); }
    }));
    // tap elsewhere closes
    setTimeout(() => {
      const off = (ev) => { if (wpMenu && !wpMenu.contains(ev.target)) { closeWpMenu(); document.removeEventListener("pointerdown", off, true); } };
      document.addEventListener("pointerdown", off, true);
    }, 0);
  }
  map.on("zoomstart movestart", closeWpMenu);

  // Draggable pending waypoints (the route the user is building, pre-start).
  // Grab a marker and drag to move it; the route line follows live.
  function pendingIcon(label) {
    return L.divIcon({ className: "", html: `<div class="wp-pin">${label}</div>`, iconSize: [20, 20], iconAnchor: [10, 10] });
  }
  function drawPending() {
    pendingLayer.clearLayers();
    pendingWaypoints.forEach((w, i) => {
      const m = L.marker([w.lat, w.lon], {
        icon: pendingIcon(i + 1), draggable: true, autoPan: true, zIndexOffset: 800,
      }).addTo(pendingLayer).bindTooltip(w.name || "WP" + (i + 1));
      m.on("drag", (e) => {
        const ll = e.target.getLatLng();
        w.lat = ll.lat; w.lon = ll.lng;
        drawWaypoints();           // route line tracks the drag
      });
      m.on("dragend", () => { drawWaypoints(); if (onWpChange) onWpChange(); });
    });
  }
  let onWpChange = null;

  // ---- go-to (tap map) ---------------------------------------------------
  function setGotoMarker(lat, lon) {
    const ll = [lat, lon];
    if (!gotoMarker) {
      gotoMarker = L.circleMarker(ll, {
        radius: 8, color: "#ff5a7a", weight: 3, fillColor: "#ff5a7a", fillOpacity: 0.8,
      }).addTo(map).bindTooltip("Go to");
    } else gotoMarker.setLatLng(ll);
  }
  function clearGotoMarker() {
    if (gotoMarker) { map.removeLayer(gotoMarker); gotoMarker = null; }
  }

  // Click handler delegates to app.js via the registered callbacks.
  // Extra modules (markers, routing) can register click "consumers" that get
  // first refusal on each click: if a consumer returns true it has handled the
  // click and the default app.js route/go-to handler is skipped.
  let onMapClick = null;     // (lat, lon, armed) => void
  const clickConsumers = [];
  map.on("click", (e) => {
    const lat = e.latlng.lat, lon = e.latlng.lng;
    for (const fn of clickConsumers) {
      try { if (fn(lat, lon)) return; } catch (err) { /* ignore */ }
    }
    if (onMapClick) onMapClick(lat, lon, gotoArmed);
  });

  // ---- depth overlay -----------------------------------------------------
  // Selectable depth palettes (#palette). Two opposing chart conventions per the
  // research doc (docs/research/depth-map-design.md §1–2), and DELIBERATELY not
  // mixed within one ramp:
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
          const cp = m.latLngToContainerPoint(m.getCenter());
          const size = m.getSize();
          const lv = this._lastView;
          if (lv && lv.z === z && lv.sx === size.x && lv.sy === size.y &&
              Math.abs(lv.cx - cp.x) < VIEW_EPS_PX && Math.abs(lv.cy - cp.y) < VIEW_EPS_PX) {
            // Still reposition the canvas so the overlay stays glued to the map
            // (cheap: a transform), but skip the expensive pixel redraw.
            const topLeft = m.containerPointToLayerPoint([0, 0]);
            L.DomUtil.setPosition(this._canvas, topLeft);
            return;
          }
          this._lastView = { z, cx: cp.x, cy: cp.y, sx: size.x, sy: size.y };
        }
        this._forceDraw = false;
        this._reset();
      });
    },
    _cancelReset() {
      if (this._resetRAF) { cancelAnimationFrame(this._resetRAF); this._resetRAF = 0; }
      this._lastView = null;
    },
  };

  const GridLayer = L.Layer.extend(Object.assign({}, CanvasOverlayMixin, {
    onAdd(m) {
      this._map = m;
      const c = this._canvas = L.DomUtil.create("canvas", "leaflet-depth-grid");
      c.style.position = "absolute";
      c.style.pointerEvents = "none";
      m.getPanes().overlayPane.appendChild(c);
      m.on("moveend zoomend resize", this._scheduleReset, this);
      this._forceDraw = true;
      this._reset();
    },
    onRemove(m) {
      m.off("moveend zoomend resize", this._scheduleReset, this);
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
      const ctx = c.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, size.x, size.y);
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
      const doRelief = reliefShow && fd && fd.f.size > 3;
      // Depth-range highlight (#highlight): cells in [hlMin,hlMax] glow green; the
      // rest are dimmed. Cheap recolour at blob time.
      const doHl = hlState.on;
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
        const pc = m.latLngToContainerPoint([lat, lon]);
        let rgb = depthColorRGB(d, min, max);

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
      // (no extra blur — that just thins a narrow survey ribbon).
      ctx.globalAlpha = 0.92;
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = "high";
      ctx.drawImage(off, 0, 0, ow, oh, minX, minY, bw, bh);
      ctx.globalAlpha = 1;
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
      this._forceDraw = true;
      this._reset();
    },
    onRemove(m) {
      m.off("moveend zoomend resize", this._scheduleReset, this);
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
      const ctx = c.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, size.x, size.y);
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
      ctx.lineWidth = 1;
      ctx.lineJoin = "round";
      ctx.font = "10px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";

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
          ctx.strokeStyle = "rgba(255,110,80,0.92)"; ctx.lineWidth = 1.8;
        } else {
          ctx.strokeStyle = major ? "rgba(255,255,255,0.6)" : "rgba(255,255,255,0.38)";
          ctx.lineWidth = major ? 1.0 : 0.7;
        }
        // Chain the disjoint marching-squares segments into polylines, then
        // Chaikin-smooth (2 passes) so the blocky lattice zigzag becomes a smooth
        // isobath curve before stroking. (#chaikin)
        ctx.beginPath();
        const lines = chainSegments(seg);
        for (let li = 0; li < lines.length; li++) {
          const poly = chaikin(lines[li], 2);
          if (poly.length < 4) continue;
          ctx.moveTo(poly[0], poly[1]);
          for (let p = 2; p < poly.length; p += 2) ctx.lineTo(poly[p], poly[p + 1]);
        }
        ctx.stroke();
        const labs = labelsByLvl[l];
        if (labs.length && (major || safety)) {   // label major + shoal isobaths
          const txt = lvl.toFixed(0);
          ctx.save();
          ctx.globalAlpha = 0.9;
          ctx.fillStyle = "rgba(255,255,255,0.95)";
          ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,0.6)";
          for (let p = 0; p < labs.length; p += 2) {
            ctx.strokeText(txt, labs[p], labs[p + 1]);
            ctx.fillText(txt, labs[p], labs[p + 1]);
          }
          ctx.restore();
        }
      }
    },
  }));
  const contourLayer = new ContourLayer();
  let contourShow = false;       // Depth contours overlay on/off (default off)

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
      const g = await VA.getJSON("/api/depth/grid?cell_m=" + cellM);
      if (!g || !g.ok || !Array.isArray(g.cells)) throw new Error("no grid");
      gridOk = true;
      const min = Number.isFinite(g.min_depth) ? g.min_depth : 0;
      const max = Number.isFinite(g.max_depth) ? g.max_depth : 1;
      const cm = Number.isFinite(g.cell_m) ? g.cell_m : cellM;
      // Colored heatmap: only mounted when Depth map is on.
      if (depthShow) {
        if (map.hasLayer(depthLayer)) { map.removeLayer(depthLayer); depthLayer.clearLayers(); }
        if (!map.hasLayer(gridLayer)) gridLayer.addTo(map);
        gridLayer.setData(g.cells, min, max, cm);
        setLegend(min, max, Number.isFinite(g.count) ? g.count : g.cells.length);
      }
      // Isobath contours: only mounted when Depth contours is on.
      if (contourShow) {
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
  }

  function startGridPoll() {
    if (gridTimer) return;
    fetchDepthGrid();
    gridTimer = setInterval(fetchDepthGrid, 4000); // depth map changes slowly
  }
  function stopGridPoll() {
    // Keep polling while either consumer (heatmap or contours) is still on.
    if (depthShow || contourShow) return;
    if (gridTimer) { clearInterval(gridTimer); gridTimer = null; }
  }

  // Redraw the colored grid immediately on zoom; refetch (new cell_m) shortly
  // after so cells re-bucket for the new zoom level.
  function redrawDepth() {
    if (!depthShow && !contourShow) return;
    if (gridOk) { fetchDepthGrid(); }
    else if (depthShow) { renderDepthDots(); }
  }
  map.on("zoomend", redrawDepth);

  // Per-frame telemetry hook: keeps the readout + soundings count fresh and, in
  // dot-fallback mode, repaints from the live points.
  function updateDepthMap(t) {
    const depth = VA.fin(t.depth_m);
    VA.setText("depth-now", depth === null ? "—" : depth.toFixed(1) + " m");
    const pts = Array.isArray(t.depth_points) ? t.depth_points : [];
    VA.setText("depth-count", String(pts.length));
    if (!depthShow) return;
    if (!gridOk) renderDepthDotsFrom(t);   // grid endpoint unavailable
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
      startGridPoll();   // shared with the heatmap; safe to call when running
      if (gridOk) fetchDepthGrid();
    } else {
      if (map.hasLayer(contourLayer)) map.removeLayer(contourLayer);
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
    const stops = (DEPTH_PALETTES[depthPalette] || DEPTH_PALETTES.angler).stops;
    const css = stops.map((s) => `rgb(${s[1][0]},${s[1][1]},${s[1][2]}) ${Math.round(s[0] * 100)}%`).join(", ");
    bar.style.background = `linear-gradient(90deg, ${css})`;
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
       <div class="hint">Relief shades drop-offs (NW light). The highlight lights up cells whose depth falls in the band (green) and dims the rest — like a Humminbird depth-range highlight.</div>`;
    card.appendChild(wrap);

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
    refreshLegendRamp();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", injectDepthControls);
  } else {
    injectDepthControls();
  }

  // ---- recorded track ----------------------------------------------------
  function updateTrack(track) {
    const pts = track && Array.isArray(track.points) ? track.points : [];
    if (pts.length) {
      if (!trackLine) trackLine = L.polyline(pts, { color: "#c084fc", weight: 3, opacity: 0.85 }).addTo(map);
      else trackLine.setLatLngs(pts);
    } else if (trackLine) trackLine.setLatLngs([]);
  }

  // ---- per-frame render --------------------------------------------------
  // Prefer sim ground truth when present; otherwise use the GPS position +
  // heading so the boat still renders on a real boat (truth is sim-only).
  VA.onTelemetry(function renderMap(t) {
    const truth = t.truth;
    const lat = truth ? truth.lat : (t.position ? t.position.lat : null);
    const lon = truth ? truth.lon : (t.position ? t.position.lon : null);
    const hdg = truth && Number.isFinite(truth.heading_deg) ? truth.heading_deg : t.heading_deg;
    if (lat !== null && lon !== null && Number.isFinite(lat) && Number.isFinite(lon)) {
      boatMarker.setLatLng([lat, lon]);
      const el = boatMarker.getElement()?.querySelector(".boat-icon");
      if (el) {
        _boatEl = el; _boatLat = lat; _boatHdg = Number.isFinite(hdg) ? hdg : _boatHdg;
        applyBoatTransform();
        updateMotorIndicator(el, t.motor);
      }
      trailPts.push([lat, lon]);
      if (trailPts.length > 600) trailPts.shift();
      trail.setLatLngs(trailPts);
      if (followBoat) {
        if (followOffsetX || followOffsetY) {
          // Keep the boat at an off-centre point (e.g. right of the wizard panel).
          const z = map.getZoom();
          const p = map.project([lat, lon], z);
          map.panTo(map.unproject([p.x - followOffsetX, p.y - followOffsetY], z), { animate: false });
        } else {
          map.panTo([lat, lon], { animate: false });
        }
      }
    }
    // Smooth the displayed GPS dot. Raw 1 Hz fixes carry a few metres of noise
    // that makes the dot jump around; a real plotter low-passes it so it sits
    // steady. (The control loop still steers on the raw fix, not this.)
    if (t.position) {
      const a = 0.18;
      if (_gpsLat === null) { _gpsLat = t.position.lat; _gpsLon = t.position.lon; }
      else {
        _gpsLat += (t.position.lat - _gpsLat) * a;
        _gpsLon += (t.position.lon - _gpsLon) * a;
      }
      gpsMarker.setLatLng([_gpsLat, _gpsLon]);
    }

    lastAnchor = t.anchor || null;
    if (t.anchor) {
      const ll = [t.anchor.lat, t.anchor.lon];
      if (!anchorMarker) {
        anchorMarker = L.marker(ll).addTo(map).bindTooltip("⚓");
        anchorCircle = L.circle(ll, { radius: t.anchor_radius_m, color: "#ff5a7a", weight: 2, fill: false }).addTo(map);
      }
      anchorMarker.setLatLng(ll);
      anchorCircle.setLatLng(ll).setRadius(t.anchor_radius_m);
    } else if (anchorMarker) {
      map.removeLayer(anchorMarker); map.removeLayer(anchorCircle);
      anchorMarker = anchorCircle = null;
    }

    drawWaypoints(t.waypoints, t.active_waypoint);
    updateTrack(t.track);
    updateDepthMap(t);
  });

  // ---- area selection (box drag / freehand polygon) ----------------------
  // Shared by the survey planner (#47) and the offline-map downloader (#52).
  // Modes: "box" = drag a rectangle; "freehand" = pointer-draw a polygon. The
  // live shape is shown while drawing; on release the caller's onDone gets the
  // finished geometry. Returns a cancel() handle. We temporarily disable map
  // dragging so the gesture draws instead of panning.
  let areaShape = null;       // current Leaflet layer being drawn
  let areaActive = false;
  function clearAreaShape() {
    if (areaShape) { map.removeLayer(areaShape); areaShape = null; }
  }
  function startAreaSelect(opts) {
    opts = opts || {};
    const mode = opts.mode === "freehand" ? "freehand" : "box";
    const onDone = typeof opts.onDone === "function" ? opts.onDone : function () {};
    cancelAreaSelect();
    areaActive = true;
    const container = map.getContainer();
    container.classList.add("area-arming");
    map.dragging.disable();

    let start = null;          // box anchor latlng
    let poly = [];             // freehand points

    function latlngAt(ev) {
      const rect = container.getBoundingClientRect();
      const pt = L.point(ev.clientX - rect.left, ev.clientY - rect.top);
      return map.containerPointToLatLng(pt);
    }
    function onDown(ev) {
      if (ev.button !== undefined && ev.button !== 0) return;
      ev.preventDefault();
      const ll = latlngAt(ev);
      if (mode === "box") {
        start = ll;
        clearAreaShape();
        areaShape = L.rectangle([ll, ll], { color: "#2ff3ff", weight: 2, dashArray: "5,5", fillOpacity: 0.08 }).addTo(map);
      } else {
        poly = [ll];
        clearAreaShape();
        areaShape = L.polygon(poly, { color: "#2ff3ff", weight: 2, dashArray: "5,5", fillOpacity: 0.08 }).addTo(map);
      }
      container.addEventListener("pointermove", onMove);
      container.addEventListener("pointerup", onUp);
    }
    function onMove(ev) {
      const ll = latlngAt(ev);
      if (mode === "box" && start && areaShape) {
        areaShape.setBounds(L.latLngBounds(start, ll));
      } else if (mode === "freehand" && areaShape) {
        const last = poly[poly.length - 1];
        if (!last || map.latLngToContainerPoint(ll).distanceTo(map.latLngToContainerPoint(last)) > 5) {
          poly.push(ll); areaShape.setLatLngs(poly);
        }
      }
    }
    function onUp() {
      container.removeEventListener("pointermove", onMove);
      container.removeEventListener("pointerup", onUp);
      finish();
    }
    function finish() {
      let result = null;
      if (mode === "box" && areaShape) {
        const b = areaShape.getBounds();
        result = {
          mode: "box",
          bounds: b,
          bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()],
          polygon: [
            [b.getNorth(), b.getWest()], [b.getNorth(), b.getEast()],
            [b.getSouth(), b.getEast()], [b.getSouth(), b.getWest()],
          ],
        };
      } else if (mode === "freehand" && poly.length >= 3) {
        const ll = L.latLngBounds(poly);
        result = {
          mode: "freehand",
          bounds: ll,
          bbox: [ll.getWest(), ll.getSouth(), ll.getEast(), ll.getNorth()],
          polygon: poly.map((p) => [p.lat, p.lng]),
        };
      }
      cancelAreaSelect(true);   // keep the shape visible for review
      onDone(result);
    }
    container.addEventListener("pointerdown", onDown);
    // remember the down handler so cancel can unbind it
    startAreaSelect._down = onDown;
    return cancelAreaSelect;
  }
  function cancelAreaSelect(keepShape) {
    if (!areaActive) { if (!keepShape) clearAreaShape(); return; }
    areaActive = false;
    const container = map.getContainer();
    container.classList.remove("area-arming");
    if (startAreaSelect._down) { container.removeEventListener("pointerdown", startAreaSelect._down); startAreaSelect._down = null; }
    try { map.dragging.enable(); } catch (e) { /* ignore */ }
    if (!keepShape) clearAreaShape();
  }

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

  // Now that the built-in overlays (Sea marks + Depth map) are registered,
  // restore the saved basemap + overlay selection. Overlays registered later
  // by other modules (Catches, Catch heatmap, No-go zones) restore through
  // their own per-overlay persistence + addOverlay, and saveLayers keeps the
  // combined record current as the user toggles anything.
  restoreLayers();

  // ---- public API for app.js ---------------------------------------------
  VA.map = {
    leaflet: map,
    addOverlay,
    startAreaSelect, cancelAreaSelect, clearAreaShape,
    redrawDepth,
    setOnMapClick(fn) { onMapClick = fn; },
    addClickConsumer(fn) { clickConsumers.push(fn); },
    setGotoArmed(on) {
      gotoArmed = on;
      const mapEl = document.getElementById("map");
      if (mapEl) mapEl.classList.toggle("goto-arming", on);
    },
    isGotoArmed() { return gotoArmed; },
    setGotoMarker, clearGotoMarker,
    setDepthShow,
    setContourShow,
    setReliefShow,
    setDepthPalette,
    getDepthPalette() { return depthPalette; },
    setHighlight,
    getHighlight() { return Object.assign({}, hlState); },
    getLastAnchor() { return lastAnchor; },
    // pending-waypoint editor accessors
    pending() { return pendingWaypoints; },
    addPending(lat, lon) {
      pendingWaypoints.push({ name: "WP" + (++wpSeq), lat, lon });
      drawPending(); drawWaypoints();
    },
    setPending(arr) { pendingWaypoints = arr; drawPending(); drawWaypoints(); },
    onWaypointChange(fn) { onWpChange = fn; },
    // Re-send the live (committed) route after the user drags/edits its
    // waypoints. The callback receives [{name,lat,lon}].
    onRouteEdit(fn) { onRouteEdit = fn; },
    redrawWaypoints() { drawPending(); drawWaypoints(); },
    recenter() { followBoat = true; },
    setFollowOffset(x, y) { followOffsetX = x || 0; followOffsetY = y || 0; },
  };
})();
