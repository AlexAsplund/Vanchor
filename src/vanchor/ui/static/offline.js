/* Vanchor-NG — Offline map download (task #52, UI half).
 *
 * Pre-downloads map tiles for an area into an IndexedDB-backed cache so the map
 * works offline on the boat. map.js patches createTile on each base layer to
 * consult VA.tileCache first (and store fetched tiles); this module owns:
 *
 *   - the IndexedDB tile store (VA.tileCache.get/put + count/clear)
 *   - a "Download offline maps" tool: pick an area (reuses VA.map box-select) +
 *     a zoom range + base layer, then fetch & store those tiles with a progress
 *     bar and a storage-used readout, capping tile counts and warning on huge
 *     areas; plus a clear-cache button.
 *   - backend chart prefetch so routing works offline too:
 *       POST /api/route/prefetch {bbox}      — prefetch nautical charts
 *       GET  /api/route/charts               — list prefetched charts
 *       POST /api/route/charts/clear         — clear them
 *
 * Degrades gracefully: if IndexedDB is unavailable the cache is a no-op (the map
 * still works online); if the chart endpoints 404 those controls disable.
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);

  // ---- IndexedDB tile store ----------------------------------------------
  const DB_NAME = "vanchor-tiles";
  const STORE = "tiles";
  let dbPromise = null;

  function openDB() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve) => {
      if (!("indexedDB" in window)) { resolve(null); return; }
      let req;
      try { req = indexedDB.open(DB_NAME, 1); } catch (e) { resolve(null); return; }
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => resolve(null);
    });
    return dbPromise;
  }

  function tx(mode) {
    return openDB().then((db) => {
      if (!db) return null;
      return db.transaction(STORE, mode).objectStore(STORE);
    });
  }

  const tileCache = {
    get(url) {
      return tx("readonly").then((store) => {
        if (!store) return null;
        return new Promise((resolve) => {
          const r = store.get(url);
          r.onsuccess = () => resolve(r.result || null);
          r.onerror = () => resolve(null);
        });
      }).catch(() => null);
    },
    put(url, blob) {
      return tx("readwrite").then((store) => {
        if (!store) return false;
        return new Promise((resolve) => {
          const r = store.put(blob, url);
          r.onsuccess = () => resolve(true);
          r.onerror = () => resolve(false);
        });
      }).catch(() => false);
    },
    count() {
      return tx("readonly").then((store) => {
        if (!store) return 0;
        return new Promise((resolve) => {
          const r = store.count();
          r.onsuccess = () => resolve(r.result || 0);
          r.onerror = () => resolve(0);
        });
      }).catch(() => 0);
    },
    clear() {
      return tx("readwrite").then((store) => {
        if (!store) return false;
        return new Promise((resolve) => {
          const r = store.clear();
          r.onsuccess = () => resolve(true);
          r.onerror = () => resolve(false);
        });
      }).catch(() => false);
    },
  };
  VA.tileCache = tileCache;

  // ---- tile maths ---------------------------------------------------------
  function lon2tile(lon, z) { return Math.floor(((lon + 180) / 360) * Math.pow(2, z)); }
  function lat2tile(lat, z) {
    const r = (lat * Math.PI) / 180;
    return Math.floor(((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * Math.pow(2, z));
  }

  // Enumerate {z,x,y} tiles covering a bbox [w,s,e,n] over a zoom range.
  function enumerateTiles(bbox, zmin, zmax) {
    const [w, s, e, n] = bbox;
    const tiles = [];
    for (let z = zmin; z <= zmax; z++) {
      const x0 = lon2tile(w, z), x1 = lon2tile(e, z);
      const y0 = lat2tile(n, z), y1 = lat2tile(s, z);  // n -> smaller y
      for (let x = Math.min(x0, x1); x <= Math.max(x0, x1); x++) {
        for (let y = Math.min(y0, y1); y <= Math.max(y0, y1); y++) {
          tiles.push({ z, x, y });
        }
      }
    }
    return tiles;
  }

  function countTiles(bbox, zmin, zmax) {
    let total = 0;
    const [w, s, e, n] = bbox;
    for (let z = zmin; z <= zmax; z++) {
      const x0 = lon2tile(w, z), x1 = lon2tile(e, z);
      const y0 = lat2tile(n, z), y1 = lat2tile(s, z);
      total += (Math.abs(x1 - x0) + 1) * (Math.abs(y1 - y0) + 1);
    }
    return total;
  }

  // Build a real tile URL from a template + the {s} subdomain rotation.
  const SUBS = ["a", "b", "c"];
  function tileUrl(template, z, x, y) {
    return template
      .replace("{s}", SUBS[(x + y) % SUBS.length])
      .replace("{z}", z).replace("{x}", x).replace("{y}", y).replace("{r}", "");
  }

  // ---- DOM ----------------------------------------------------------------
  const card = $("offline-card");
  if (!card) return;
  const baseSel = $("offline-base");
  const zminSel = $("offline-zmin");
  const zmaxSel = $("offline-zmax");
  const pickBtn = $("offline-pick");
  const dlBtn = $("offline-download");
  const cancelBtn = $("offline-cancel");
  const clearBtn = $("offline-clear");
  const estEl = $("offline-est");
  const barWrap = $("offline-bar");
  const barFill = $("offline-fill");
  const statusEl = $("offline-status");
  const usedEl = $("offline-used");
  const chartBtn = $("offline-charts");
  const chartClearBtn = $("offline-charts-clear");
  const chartStatus = $("offline-charts-status");
  const chartList = $("offline-charts-list");
  const capInput = $("offline-cap");

  // The real limiter on bulk tile fetching is the providers' fair-use policy
  // (we stay polite via the CONC=6 throttle below) and browser storage, not a
  // small count, so the cap is a user SETTING (default 50k ≈ 1 GB ≈ a small
  // lake to ~z20). Above WARN_TILES we caution about time/storage but allow it.
  const CAP_KEY = "vanchor-tile-cap";
  let TILE_CAP = 50000;
  try { const s = parseInt(localStorage.getItem(CAP_KEY), 10); if (s >= 1000) TILE_CAP = s; } catch (e) { /* ignore */ }
  const WARN_TILES = 8000;
  const KB_PER_TILE = 22;      // rough average for size/quota estimates
  let area = null;             // { bbox } from box-select
  let picking = false;
  let cancelled = false;
  let downloading = false;

  function setStatus(msg, kind) {
    if (!statusEl) return;
    statusEl.textContent = msg || "";
    statusEl.className = "hint" + (kind ? " " + kind : "");
  }

  function bases() {
    // Populate base layer + zoom selectors from what map.js exposes.
    if (baseSel && VA._baseTemplates) {
      baseSel.innerHTML = "";
      Object.keys(VA._baseTemplates).forEach((name) => {
        const o = document.createElement("option");
        o.value = name; o.textContent = name;
        baseSel.appendChild(o);
      });
    }
    [zminSel, zmaxSel].forEach((sel, i) => {
      if (!sel) return;
      sel.innerHTML = "";
      for (let z = 8; z <= 20; z++) {
        const o = document.createElement("option");
        o.value = z; o.textContent = z;
        sel.appendChild(o);
      }
      sel.value = i === 0 ? 12 : 17;
    });
  }
  bases();

  function refreshEstimate() {
    if (!area || !estEl) { if (estEl) estEl.textContent = ""; return; }
    const zmin = parseInt(zminSel.value, 10);
    const zmax = Math.max(zmin, parseInt(zmaxSel.value, 10));
    const n = countTiles(area.bbox, zmin, zmax);
    const overCap = n > TILE_CAP;
    const mb = Math.round((n * KB_PER_TILE) / 1024);
    estEl.textContent = "~" + n.toLocaleString() + " tiles (~" + mb + " MB)" +
      (overCap ? " — over the " + TILE_CAP.toLocaleString() + "-tile cap; narrow the area or lower the top zoom." :
        n > WARN_TILES ? " — large; will take several minutes (throttled to stay within tile limits)." : "");
    estEl.className = "hint" + (overCap ? " err" : n > WARN_TILES ? " busy" : "");
    if (dlBtn) dlBtn.disabled = overCap || n === 0;
  }
  if (zminSel) zminSel.addEventListener("change", refreshEstimate);
  if (zmaxSel) zmaxSel.addEventListener("change", refreshEstimate);
  if (capInput) {
    capInput.value = TILE_CAP;
    capInput.addEventListener("change", () => {
      let v = parseInt(capInput.value, 10);
      if (!(v >= 1000)) v = 50000;
      v = Math.min(500000, v);
      TILE_CAP = v; capInput.value = v;
      try { localStorage.setItem(CAP_KEY, String(v)); } catch (e) { /* ignore */ }
      refreshEstimate();
    });
  }

  if (pickBtn && VA.map && VA.map.startAreaSelect) {
    pickBtn.addEventListener("click", () => {
      if (picking) { VA.map.cancelAreaSelect(); picking = false; pickBtn.classList.remove("active"); pickBtn.textContent = "Select area on map"; return; }
      picking = true;
      pickBtn.classList.add("active");
      pickBtn.textContent = "Drag a box… (cancel)";
      setStatus("Drag a box over the area to cache.", "busy");
      VA.map.startAreaSelect({
        mode: "box",
        onDone(result) {
          picking = false;
          pickBtn.classList.remove("active");
          pickBtn.textContent = "Re-select area";
          if (!result) { setStatus("No area selected.", "err"); area = null; return; }
          area = result;
          setStatus("Area selected. Pick zoom range and download.", "ok");
          refreshEstimate();
        },
      });
    });
  }

  async function updateUsed() {
    if (!usedEl) return;
    const n = await tileCache.count();
    let extra = "";
    if (navigator.storage && navigator.storage.estimate) {
      try {
        const est = await navigator.storage.estimate();
        if (est && est.usage) extra = " · " + (est.usage / 1048576).toFixed(1) + " MB used";
      } catch (e) { /* ignore */ }
    }
    usedEl.textContent = n.toLocaleString() + " tiles cached" + extra;
  }
  updateUsed();

  async function download() {
    if (!area) { setStatus("Select an area first.", "err"); return; }
    const name = baseSel ? baseSel.value : "Dark";
    const template = VA._baseTemplates && VA._baseTemplates[name];
    if (!template) { setStatus("Unknown base layer.", "err"); return; }
    const zmin = parseInt(zminSel.value, 10);
    const zmax = Math.max(zmin, parseInt(zmaxSel.value, 10));
    const tiles = enumerateTiles(area.bbox, zmin, zmax);
    if (tiles.length > TILE_CAP) { setStatus("Too many tiles (" + tiles.length + "). Narrow the area.", "err"); return; }
    if (!tiles.length) { setStatus("Nothing to download.", "err"); return; }

    downloading = true; cancelled = false;
    if (dlBtn) dlBtn.disabled = true;
    if (cancelBtn) cancelBtn.classList.remove("hidden");
    if (barWrap) barWrap.classList.remove("hidden");

    let done = 0, stored = 0, failed = 0;
    const CONC = 6;             // limit parallel fetches to be gentle on tiles
    let idx = 0;

    async function worker() {
      while (idx < tiles.length && !cancelled) {
        const t = tiles[idx++];
        const url = tileUrl(template, t.z, t.x, t.y);
        try {
          const existing = await tileCache.get(url);
          if (!existing) {
            const r = await fetch(url, { mode: "cors" });
            if (r.ok) { const b = await r.blob(); await tileCache.put(url, b); stored++; }
            else failed++;
          }
        } catch (e) { failed++; }
        done++;
        if (done % 5 === 0 || done === tiles.length) {
          const pct = (done / tiles.length) * 100;
          if (barFill) barFill.style.width = pct.toFixed(1) + "%";
          setStatus("Downloading… " + done + " / " + tiles.length + " (" + stored + " new)", "busy");
        }
      }
    }
    await Promise.all(Array.from({ length: Math.min(CONC, tiles.length) }, worker));

    downloading = false;
    if (cancelBtn) cancelBtn.classList.add("hidden");
    if (dlBtn) dlBtn.disabled = false;
    if (cancelled) setStatus("Cancelled at " + done + " / " + tiles.length + ".", "");
    else setStatus("Done — " + stored + " new tiles cached" + (failed ? ", " + failed + " failed" : "") + ".", "ok");
    VA.logLine("offline tiles: " + stored + " stored, " + failed + " failed, " + name);
    updateUsed();
  }
  if (dlBtn) dlBtn.addEventListener("click", download);
  if (cancelBtn) cancelBtn.addEventListener("click", () => { cancelled = true; });

  if (clearBtn) clearBtn.addEventListener("click", async () => {
    if (downloading) { setStatus("Wait for the download to finish first.", "err"); return; }
    if (!window.confirm("Clear all cached offline map tiles?")) return;
    await tileCache.clear();
    setStatus("Tile cache cleared.", "");
    if (barFill) barFill.style.width = "0%";
    updateUsed();
  });

  // ---- chart prefetch (routing offline) ----------------------------------
  let chartsAvailable = null;
  async function listCharts() {
    if (!chartList) return;
    let r;
    try {
      const resp = await fetch("/api/route/charts");
      if (resp.status === 404) { chartsAvailable = false; if (chartStatus) { chartStatus.textContent = "Chart prefetch not available on this runtime."; chartStatus.className = "hint err"; } if (chartBtn) chartBtn.disabled = true; if (chartClearBtn) chartClearBtn.disabled = true; return; }
      r = await resp.json();
      chartsAvailable = true;
    } catch (e) { return; }
    const items = (r && (r.charts || r.items || (Array.isArray(r) ? r : []))) || [];
    chartList.innerHTML = "";
    if (!items.length) { chartList.textContent = "No charts prefetched."; return; }
    items.forEach((c) => {
      const div = document.createElement("div");
      div.className = "hint";
      const label = typeof c === "string" ? c : (c.name || c.id || JSON.stringify(c));
      div.textContent = "• " + label;
      chartList.appendChild(div);
    });
  }

  if (chartBtn) chartBtn.addEventListener("click", async () => {
    if (chartsAvailable === false) return;
    const a = area || (VA.map && VA.map.leaflet && (function () {
      const b = VA.map.leaflet.getBounds();
      return { bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()] };
    })());
    if (!a) { if (chartStatus) { chartStatus.textContent = "Select an area first."; chartStatus.className = "hint err"; } return; }
    if (chartStatus) { chartStatus.textContent = "Prefetching charts…"; chartStatus.className = "hint busy"; }
    try {
      const resp = await fetch("/api/route/prefetch", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bbox: a.bbox }),
      });
      if (resp.status === 404) { chartsAvailable = false; if (chartStatus) { chartStatus.textContent = "Chart prefetch not available."; chartStatus.className = "hint err"; } return; }
      const r = await resp.json();
      if (chartStatus) { chartStatus.textContent = (r && r.message) || "Charts prefetched."; chartStatus.className = "hint ok"; }
    } catch (e) {
      if (chartStatus) { chartStatus.textContent = "Prefetch failed: " + e; chartStatus.className = "hint err"; }
    }
    listCharts();
  });

  if (chartClearBtn) chartClearBtn.addEventListener("click", async () => {
    if (chartsAvailable === false) return;
    if (!window.confirm("Clear all prefetched charts?")) return;
    try {
      const resp = await fetch("/api/route/charts/clear", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      if (resp.status === 404) { chartsAvailable = false; return; }
      if (chartStatus) { chartStatus.textContent = "Charts cleared."; chartStatus.className = "hint"; }
    } catch (e) { /* ignore */ }
    listCharts();
  });

  // List charts lazily when the card is first opened (no proactive probe spam).
  if (card.tagName === "DETAILS") {
    card.addEventListener("toggle", () => { if (card.open && chartsAvailable === null) listCharts(); }, { once: false });
  }
})();
