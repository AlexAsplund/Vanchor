/* Vanchor-NG — map markers module (task #38).
 *
 * Lets the user drop named, icon-tagged markers on the map: arm placement with
 * the FAB (or long-press the map), tap to drop. Each marker has a selectable
 * icon (✕ default, plus home/fish/anchor/hazard/star), can be renamed, deleted
 * and dragged via its popup. Markers persist in localStorage and live in a
 * toggleable overlay that coexists with the existing layer overlays (so the
 * user can show/hide them alongside Sea marks etc.).
 *
 * Also supports importing points from a GPX or GeoJSON file and exporting the
 * markers back out to GeoJSON.
 *
 * Exposes a small API on VA.markers for the layout/profiles module (visibility,
 * persistence) and for routing.js (destination picking can coexist).
 */
"use strict";

(function () {
  if (!window.VA || !VA.map || !VA.map.leaflet) return;
  const map = VA.map.leaflet;
  const L = window.L;

  const STORE_KEY = "vanchor-markers";
  const VIS_KEY = "vanchor-markers-visible";

  // ---- icon palette ------------------------------------------------------
  // Glyphs are plain text/emoji so they render without extra assets and stay
  // theme-consistent (the pin chrome is themed via CSS).
  const ICONS = [
    { id: "x", glyph: "✕", label: "Marker" },
    { id: "home", glyph: "🏠", label: "Home" },
    { id: "fish", glyph: "🐟", label: "Fishing" },
    { id: "anchor", glyph: "⚓", label: "Anchorage" },
    { id: "hazard", glyph: "⚠", label: "Hazard" },
    { id: "star", glyph: "★", label: "Favourite" },
  ];
  const iconById = (id) => ICONS.find((i) => i.id === id) || ICONS[0];

  const layer = L.layerGroup();
  let visible = true;
  try { visible = localStorage.getItem(VIS_KEY) !== "0"; } catch (e) { /* ignore */ }

  let markers = [];        // [{id, name, icon, lat, lon}]
  let seq = 0;
  let armed = false;
  const objs = new Map();  // marker.id -> L.marker

  // ---- persistence -------------------------------------------------------
  function load() {
    let raw = null;
    try { raw = localStorage.getItem(STORE_KEY); } catch (e) { /* ignore */ }
    if (!raw) return;
    try {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr)) {
        markers = arr.filter((m) => m && Number.isFinite(m.lat) && Number.isFinite(m.lon))
          .map((m) => ({
            id: m.id || ("m" + (++seq)),
            name: typeof m.name === "string" ? m.name : "Marker",
            icon: iconById(m.icon).id,
            lat: m.lat, lon: m.lon,
          }));
        markers.forEach((m) => { const n = parseInt(String(m.id).replace(/\D/g, ""), 10); if (n > seq) seq = n; });
      }
    } catch (e) { /* ignore */ }
  }
  function save() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(markers)); } catch (e) { /* ignore */ }
    updateBadge();
  }
  function updateBadge() {
    const b = document.getElementById("marker-state");
    if (b) b.textContent = markers.length ? "● " + markers.length : "";
  }

  // ---- leaflet icon ------------------------------------------------------
  function divIconFor(icon) {
    const g = iconById(icon).glyph;
    return L.divIcon({
      className: "",
      html: `<div class="map-marker-pin" data-icon="${icon}"><span>${g}</span></div>`,
      iconSize: [30, 30], iconAnchor: [15, 30], popupAnchor: [0, -28],
    });
  }

  // ---- popup (rename / icon / delete) ------------------------------------
  function popupHtml(m) {
    const opts = ICONS.map((ic) =>
      `<button type="button" class="mm-ic ${ic.id === m.icon ? "on" : ""}" data-icon="${ic.id}" title="${ic.label}">${ic.glyph}</button>`
    ).join("");
    return (
      `<div class="mm-popup" data-id="${m.id}">` +
      `<input class="mm-name" type="text" value="${escapeAttr(m.name)}" aria-label="marker name" />` +
      `<div class="mm-icons">${opts}</div>` +
      `<div class="mm-route-label">Take me here</div>` +
      `<div class="mm-route">` +
      `<button type="button" class="mm-route-fast">Fastest route</button>` +
      `<button type="button" class="mm-route-shore">Along shoreline</button>` +
      `</div>` +
      `<div class="mm-actions">` +
      `<button type="button" class="mm-del">Delete</button>` +
      `</div></div>`
    );
  }
  function escapeAttr(s) {
    return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function wirePopup(m, mk) {
    mk.on("popupopen", (e) => {
      const root = e.popup.getElement();
      if (!root) return;
      const nameEl = root.querySelector(".mm-name");
      if (nameEl) nameEl.addEventListener("input", () => { m.name = nameEl.value; mk.options.title = m.name; save(); });
      root.querySelectorAll(".mm-ic").forEach((b) =>
        b.addEventListener("click", () => {
          m.icon = b.dataset.icon;
          mk.setIcon(divIconFor(m.icon));
          root.querySelectorAll(".mm-ic").forEach((x) => x.classList.toggle("on", x.dataset.icon === m.icon));
          save();
        }));
      const ll = mk.getLatLng();
      const routeTo = (mode) => {
        mk.closePopup();
        if (VA.routing && VA.routing.planTo) VA.routing.planTo(ll.lat, ll.lng, mode);
        else VA.logLine("smart routing not available");
      };
      const rf = root.querySelector(".mm-route-fast");
      if (rf) rf.addEventListener("click", () => routeTo("fastest"));
      const rs = root.querySelector(".mm-route-shore");
      if (rs) rs.addEventListener("click", () => routeTo("shoreline"));
      const del = root.querySelector(".mm-del");
      if (del) del.addEventListener("click", () => removeMarker(m.id));
    });
  }

  // ---- marker lifecycle --------------------------------------------------
  function addMarkerObj(m) {
    const mk = L.marker([m.lat, m.lon], {
      icon: divIconFor(m.icon), draggable: true, title: m.name, zIndexOffset: 600,
    });
    // Popup content is regenerated each open so the selected-icon highlight and
    // current name reflect live state.
    mk.bindPopup(() => popupHtml(m), { className: "mm-popup-wrap", minWidth: 190 });
    mk.on("dragend", () => {
      const ll = mk.getLatLng();
      m.lat = ll.lat; m.lon = ll.lng;
      save();
    });
    wirePopup(m, mk);
    mk.addTo(layer);
    objs.set(m.id, mk);
  }
  function redraw() {
    layer.clearLayers();
    objs.clear();
    markers.forEach(addMarkerObj);
  }
  function createMarker(lat, lon, icon, name) {
    const m = { id: "m" + (++seq), name: name || "Marker " + seq, icon: iconById(icon).id, lat, lon };
    markers.push(m);
    addMarkerObj(m);
    save();
    return m;
  }
  function removeMarker(id) {
    const ix = markers.findIndex((m) => m.id === id);
    if (ix === -1) return;
    const mk = objs.get(id);
    if (mk) { layer.removeLayer(mk); objs.delete(id); }
    markers.splice(ix, 1);
    save();
  }

  // ---- visibility (toggleable overlay) -----------------------------------
  function setVisible(on) {
    visible = !!on;
    if (visible) { if (!map.hasLayer(layer)) layer.addTo(map); }
    else if (map.hasLayer(layer)) map.removeLayer(layer);
    try { localStorage.setItem(VIS_KEY, visible ? "1" : "0"); } catch (e) { /* ignore */ }
    if (overlayBox) overlayBox.checked = visible;
    if (fabBtn) fabBtn.classList.toggle("markers-hidden", !visible);
  }

  // ---- arming + map interaction ------------------------------------------
  let selectedIcon = "x";
  function setArmed(on) {
    armed = !!on;
    const el = document.getElementById("map");
    if (el) el.classList.toggle("marker-arming", armed);
    if (fabBtn) fabBtn.classList.toggle("active", armed);
    if (armed && !visible) setVisible(true);
  }

  // Hook into the existing map click pipeline without clobbering go-to/route.
  // map.js routes clicks to a single onMapClick; routing/markers/route all need
  // a click, so we layer on top: markers consume the click only when armed.
  if (VA.map.addClickConsumer) {
    VA.map.addClickConsumer((lat, lon) => {
      if (!armed) return false;
      createMarker(lat, lon, selectedIcon);
      if (!stickyBox || !stickyBox.checked) setArmed(false);
      return true;
    });
  } else {
    // Fallback: listen directly (won't pre-empt app.js's handler, but still
    // works because app.js ignores clicks unless go-to is armed or in route).
    map.on("click", (e) => {
      if (!armed) return;
      createMarker(e.latlng.lat, e.latlng.lng, selectedIcon);
      if (!stickyBox || !stickyBox.checked) setArmed(false);
    });
  }

  // Long-press the map (or right-click on desktop) opens a small CHOICE menu at
  // that point instead of silently dropping a marker: place a marker, or navigate
  // there (fastest direct line, or a water-only route around the shoreline).
  function openMapMenu(latlng) {
    const lat = latlng.lat, lon = latlng.lng;
    const popup = L.popup({ className: "map-menu-wrap", closeButton: true, autoPanPadding: [24, 24] })
      .setLatLng(latlng)
      .setContent(
        `<div class="map-menu">
           <div class="mm-depth" data-depth hidden></div>
           <button class="mm-btn" data-act="marker">📍 Place marker here</button>
           <div class="mm-sep">Navigate here</div>
           <button class="mm-btn" data-act="fast">⚡ Fastest (direct)</button>
           <button class="mm-btn" data-act="shore">🌊 Along shoreline</button>
         </div>`)
      .openOn(map);
    const goto = () => VA.send({ type: "goto", waypoints: [{ name: "GOTO", lat, lon }], throttle: 0.6, on_arrival: "anchor" });
    const node = popup.getElement();
    if (!node) return;

    // Depth at the pressed point (nearest sounding, else nearest contour) --
    // fetched async like the island detect below so the menu pops instantly;
    // stays hidden when the chart has no depth data here or the endpoint is absent.
    const depthEl = node.querySelector("[data-depth]");
    fetch(`/api/depth/at?lat=${lat.toFixed(7)}&lon=${lon.toFixed(7)}`)
      .then((res) => (res.ok ? res.json() : null)).then((r) => {
        if (!r || r.ok === false || !Number.isFinite(r.depth_m)) return;
        if (!depthEl || !depthEl.isConnected) return;       // popup already closed
        const src = r.source === "contour" ? " (contour)" : "";
        depthEl.textContent = `🌊 Depth ~${r.depth_m.toFixed(1)} m${src}`;
        depthEl.hidden = false;
      }).catch(() => { /* no depth info -> row stays hidden */ });
    node.querySelector('[data-act="marker"]').addEventListener("click", () => {
      createMarker(lat, lon, selectedIcon); map.closePopup(popup);
    });
    node.querySelector('[data-act="fast"]').addEventListener("click", () => {
      goto(); map.closePopup(popup);
    });
    node.querySelector('[data-act="shore"]').addEventListener("click", async () => {
      map.closePopup(popup);
      if (VA.routing && VA.routing.planTo) {
        const ok = await VA.routing.planTo(lat, lon, "shoreline");
        if (ok && VA.routeEditor && VA.routeEditor.startRoute) VA.routeEditor.startRoute();
      } else {
        goto();  // smart routing module absent -> fall back to a direct goto
      }
    });

    // If the long-press is on an ISLAND (land surrounded by the boat's water),
    // swap "Along shoreline" for "Loop around island" — detected async so the
    // menu still pops instantly (water presses bail out cheaply on the backend).
    const shoreBtn = node.querySelector('[data-act="shore"]');
    fetch("/api/route/island", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lat, lon, offset_m: 20 }),
    }).then((res) => (res.ok ? res.json() : null)).then((r) => {
      if (!r || r.ok === false || !Array.isArray(r.waypoints) || !r.waypoints.length) return;
      if (!shoreBtn || !shoreBtn.isConnected) return;       // popup closed before detect returned
      const fresh = shoreBtn.cloneNode(false);              // shallow clone -> drops the shoreline listener
      fresh.dataset.act = "island";
      fresh.textContent = "🏝 Loop around island";
      shoreBtn.replaceWith(fresh);
      fresh.addEventListener("click", () => {
        map.closePopup(popup);
        if (VA.island && VA.island.planIsland) {
          Promise.resolve(VA.island.planIsland(lat, lon, true)).then(() => {
            if (VA.routeEditor && VA.routeEditor.startRoute) VA.routeEditor.startRoute();
          });
        }
      });
    }).catch(() => { /* endpoint absent -> keep 'Along shoreline' */ });
  }
  map.on("contextmenu", (e) => openMapMenu(e.latlng));

  // ---- import GPX / GeoJSON ----------------------------------------------
  function importGeoJSON(obj) {
    let count = 0;
    const visit = (geom, props) => {
      if (!geom) return;
      if (geom.type === "Point" && Array.isArray(geom.coordinates)) {
        const [lon, lat] = geom.coordinates;
        if (Number.isFinite(lat) && Number.isFinite(lon)) {
          createMarker(lat, lon, (props && props.icon) || selectedIcon, (props && (props.name || props.title)) || undefined);
          count++;
        }
      } else if (geom.type === "MultiPoint" && Array.isArray(geom.coordinates)) {
        geom.coordinates.forEach((c) => visit({ type: "Point", coordinates: c }, props));
      } else if ((geom.type === "LineString") && Array.isArray(geom.coordinates)) {
        geom.coordinates.forEach((c) => visit({ type: "Point", coordinates: c }, props));
      }
    };
    if (obj.type === "FeatureCollection" && Array.isArray(obj.features)) {
      obj.features.forEach((f) => visit(f.geometry, f.properties));
    } else if (obj.type === "Feature") {
      visit(obj.geometry, obj.properties);
    } else if (obj.type && obj.coordinates) {
      visit(obj, null);
    }
    return count;
  }
  function importGPX(text) {
    let count = 0;
    let doc;
    try { doc = new DOMParser().parseFromString(text, "application/xml"); } catch (e) { return 0; }
    if (!doc || doc.getElementsByTagName("parsererror").length) return 0;
    const tags = ["wpt", "rtept", "trkpt"];
    tags.forEach((tag) => {
      const els = doc.getElementsByTagName(tag);
      for (let i = 0; i < els.length; i++) {
        const lat = parseFloat(els[i].getAttribute("lat"));
        const lon = parseFloat(els[i].getAttribute("lon"));
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
        const nameEl = els[i].getElementsByTagName("name")[0];
        const nm = nameEl ? nameEl.textContent.trim() : undefined;
        createMarker(lat, lon, selectedIcon, nm);
        count++;
      }
    });
    return count;
  }
  function importFile(file) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result || "");
      const isGpx = /\.gpx$/i.test(file.name) || /<gpx[\s>]/i.test(text);
      let n = 0;
      if (isGpx) n = importGPX(text);
      else {
        try { n = importGeoJSON(JSON.parse(text)); }
        catch (e) { n = importGPX(text); /* last-ditch */ }
      }
      const status = document.getElementById("marker-import-status");
      if (status) status.textContent = n ? `Imported ${n} marker${n === 1 ? "" : "s"}.` : "No points found in file.";
      if (n) { setVisible(true); fitToMarkers(); }
    };
    reader.readAsText(file);
  }
  function fitToMarkers() {
    if (!markers.length) return;
    const b = L.latLngBounds(markers.map((m) => [m.lat, m.lon]));
    try { map.fitBounds(b.pad(0.3), { maxZoom: 17 }); } catch (e) { /* ignore */ }
  }

  // ---- export GeoJSON ----------------------------------------------------
  function toGeoJSON() {
    return {
      type: "FeatureCollection",
      features: markers.map((m) => ({
        type: "Feature",
        properties: { name: m.name, icon: m.icon },
        geometry: { type: "Point", coordinates: [m.lon, m.lat] },
      })),
    };
  }
  function exportGeoJSON() {
    const blob = new Blob([JSON.stringify(toGeoJSON(), null, 2)], { type: "application/geo+json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "vanchor-markers.geojson";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // ---- wire DOM controls -------------------------------------------------
  const fabBtn = document.getElementById("marker-fab");
  const overlayBox = document.getElementById("marker-overlay-show");
  const stickyBox = document.getElementById("marker-sticky");
  const iconChooser = document.getElementById("marker-icon-chooser");
  const importInput = document.getElementById("marker-import");
  const exportBtn = document.getElementById("marker-export");
  const clearBtn = document.getElementById("marker-clear");

  if (iconChooser) {
    iconChooser.innerHTML = ICONS.map((ic) =>
      `<button type="button" class="mm-ic ${ic.id === selectedIcon ? "on" : ""}" data-icon="${ic.id}" title="${ic.label}">${ic.glyph}</button>`
    ).join("");
    iconChooser.querySelectorAll(".mm-ic").forEach((b) =>
      b.addEventListener("click", () => {
        selectedIcon = b.dataset.icon;
        iconChooser.querySelectorAll(".mm-ic").forEach((x) => x.classList.toggle("on", x.dataset.icon === selectedIcon));
      }));
  }
  if (fabBtn) fabBtn.addEventListener("click", () => setArmed(!armed));
  if (overlayBox) overlayBox.addEventListener("change", () => setVisible(overlayBox.checked));
  if (importInput) importInput.addEventListener("change", () => { if (importInput.files[0]) { importFile(importInput.files[0]); importInput.value = ""; } });
  if (exportBtn) exportBtn.addEventListener("click", exportGeoJSON);
  if (clearBtn) clearBtn.addEventListener("click", () => {
    if (!markers.length) return;
    if (window.confirm("Delete all " + markers.length + " markers?")) { markers = []; redraw(); save(); }
  });

  // ---- init --------------------------------------------------------------
  load();
  redraw();
  updateBadge();
  if (overlayBox) overlayBox.checked = visible;
  setVisible(visible);

  VA.markers = {
    setVisible, isVisible() { return visible; },
    count() { return markers.length; },
    setArmed, isArmed() { return armed; },
    exportGeoJSON, importFile,
  };
})();
