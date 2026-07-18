/* Vanchor-NG — map CORE module.
 *
 * Creates the Leaflet map (the living backdrop, CARTO dark-matter tiles),
 * owns the selectable basemaps + the unified layers control + persisted layer
 * selection, the faint reference grid, the offline tile-cache integration
 * point, the follow/zoom state, the map-click dispatch (consumers + the app.js
 * go-to handler), and the box/freehand area-selection gesture.
 *
 * This module MUST load FIRST among the map-*.js files: it creates the shared
 * context object `VA.mapCtx` (the map instance, panes, layer-registration
 * helpers, follow state) and the base public `VA.map` object that the feature
 * modules (boat / anchor / waypoints / depth / track) read from and extend.
 *
 * Shared context: VA.mapCtx = {
 *   map,                       // the Leaflet map instance
 *   START,                     // initial [lat, lon]
 *   addOverlay(name, layer, opts),  // register an overlay into the control
 *   follow: { boat, offsetX, offsetY },  // boat-follow pan state (read/written
 *                                  // by map-boat.js's telemetry pan)
 * }
 *
 * Public API: VA.map is created here with the core members (leaflet, addOverlay,
 * area select, click, go-to-arming, follow). Feature modules ASSIGN their own
 * members onto the same VA.map object (so the public interface is preserved
 * exactly).
 */
"use strict";

(function () {
  const VA = window.VA;
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
  // STARTS engaged (1): during load, overlay modules restore their overlays with
  // layer.addTo(map) -> overlayadd -> saveLayers, which would otherwise persist
  // the still-default basemap and CLOBBER the user's saved one before
  // restoreLayers() reads it. restoreLayers() opens saving once restore is done.
  let suppressSave = 1;

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
  map.on("baselayerchange", (e) => {
    saveLayers();
    // Dark basemap contrast: tag #map so CSS can boost brightness/saturation.
    const mapEl = document.getElementById("map");
    if (mapEl) mapEl.classList.toggle("base-dark", e.name === "Dark");
  });

  // Restore the saved basemap + overlay selection. Called once by the depth
  // module after the built-in overlays exist (that module registers the last
  // built-in overlays); overlay modules that load later restore themselves
  // through their own persistence and addOverlay.
  function restoreLayers() {
    const prefs = readLayerPrefs();   // read while saving is still suppressed, so
                                      // it's the user's pref, not a load-time clobber
    if (prefs) {
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
    }
    // Apply dark contrast class for the active basemap on boot.
    const mapEl = document.getElementById("map");
    if (mapEl) {
      let activeName = "Dark";
      Object.keys(base).forEach((n) => { if (map.hasLayer(base[n])) activeName = n; });
      mapEl.classList.toggle("base-dark", activeName === "Dark");
    }
    // Load + restore complete: enable saving and persist the (restored or default)
    // state once. From here, basemap/overlay changes save normally.
    suppressSave = 0;
    saveLayers();
    // Return true when saved prefs existed (depth module uses this to decide
    // whether to probe for a default-on depth overlay on first run).
    return !!prefs;
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
  //
  // In-memory object-URL LRU on top of the IndexedDB store: panning back and
  // forth otherwise re-runs IDB get -> blob -> createObjectURL -> revoke for
  // EVERY re-entered tile (profiling showed ~270 ms of createObjectURL + heavy
  // GC per 15 s of panning). A hit here re-uses the same object URL, so the
  // browser also re-uses the already-decoded image. ~256 tiles x ~20 KB. (perf)
  const _tileUrlLru = new Map();     // url -> object URL (Map preserves insert order)
  const TILE_LRU_MAX = 256;
  function lruGet(url) {
    const obj = _tileUrlLru.get(url);
    if (obj !== undefined) {         // refresh recency
      _tileUrlLru.delete(url);
      _tileUrlLru.set(url, obj);
    }
    return obj;
  }
  function lruPut(url, blob) {
    const obj = URL.createObjectURL(blob);
    _tileUrlLru.set(url, obj);
    if (_tileUrlLru.size > TILE_LRU_MAX) {
      const [oldUrl, oldObj] = _tileUrlLru.entries().next().value;
      _tileUrlLru.delete(oldUrl);
      try { URL.revokeObjectURL(oldObj); } catch (e) { /* ignore */ }
    }
    return obj;
  }
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
      const hot = lruGet(url);
      if (hot !== undefined) {
        img.onload = () => done(null, img);
        img.onerror = () => done(new Error("cached tile decode failed"), img);
        img.src = hot;
        return img;
      }
      cache.get(url).then((blob) => {
        if (blob) {
          img.onload = () => done(null, img);
          img.onerror = () => done(new Error("cached tile decode failed"), img);
          img.src = lruPut(url, blob);
          return;
        }
        // not cached: download the tile ONCE via fetch, then use that single blob
        // for BOTH the <img> (object URL) and the cache. Previously we set
        // img.src=url AND fetch(url) separately, downloading every uncached tile
        // twice. (perf) The plain fetch of the same URL is CORS-safe (the old
        // second fetch already worked).
        fetch(url)
          .then((r) => (r.ok ? r.blob() : Promise.reject(new Error("tile fetch failed"))))
          .then((blob) => {
            img.onload = () => done(null, img);
            img.onerror = () => done(new Error("tile load failed"), img);
            img.src = lruPut(url, blob);
            if (cache.put) { try { cache.put(url, blob); } catch (e) {} }
          })
          .catch(() => {
            // Network fetch failed — fall back to a direct <img> load.
            img.onload = () => done(null, img);
            img.onerror = () => done(new Error("tile load failed"), img);
            img.src = url;
          });
      }).catch(() => {
        img.src = url;
        img.onload = () => done(null, img);
        img.onerror = () => done(new Error("tile load failed"), img);
      });
      return img;
    };
  });

  // ---- follow / zoom state -----------------------------------------------
  // The boat-follow pan lives in map-boat.js (it already resolves the boat
  // lat/lon/heading per frame). It reads this shared follow state; recenter()
  // and setFollowOffset() (public API below) write it. A user drag turns
  // following off.
  const follow = { boat: true, offsetX: 0, offsetY: 0 };
  map.on("dragstart", () => { follow.boat = false; });

  // ---- go-to (tap map) ---------------------------------------------------
  let gotoMarker = null, gotoArmed = false;
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

  // ---- shared context for the feature modules ----------------------------
  VA.mapCtx = {
    map,
    START,
    addOverlay,
    restoreLayers,
    follow,
  };

  // ---- public API for app.js (core members; feature modules extend this) --
  VA.map = {
    leaflet: map,
    addOverlay,
    startAreaSelect, cancelAreaSelect, clearAreaShape,
    setOnMapClick(fn) { onMapClick = fn; },
    addClickConsumer(fn) { clickConsumers.push(fn); },
    setGotoArmed(on) {
      gotoArmed = on;
      const mapEl = document.getElementById("map");
      if (mapEl) mapEl.classList.toggle("goto-arming", on);
    },
    isGotoArmed() { return gotoArmed; },
    setGotoMarker, clearGotoMarker,
    recenter() { follow.boat = true; },
    setFollowOffset(x, y) { follow.offsetX = x || 0; follow.offsetY = y || 0; },
  };
})();
