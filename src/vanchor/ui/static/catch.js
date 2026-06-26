/* Vanchor-NG — Catch logger (task #56).
 *
 * A 🎣 HUD button opens a touch-friendly panel: pick a species (list editable
 * in Settings, default Pike/Perch/Trout), enter length + weight via steppers,
 * and save a log {date, species, length, weight, lat, lon} taken at the current
 * telemetry position.
 *
 * Logged catches show on their OWN toggleable "Catches" map overlay (separate
 * from the generic markers system) with a species/size/date popup, plus a
 * catch-log list. Persisted in localStorage: `vanchor-catches`, `vanchor-species`.
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const map = window.VA && VA.map && VA.map.leaflet;
  const L = window.L;

  const CATCH_KEY = "vanchor-catches";
  const SPECIES_KEY = "vanchor-species";
  const VIS_KEY = "vanchor-catches-visible";
  const DEFAULT_SPECIES = ["Pike", "Perch", "Trout"];

  let species = DEFAULT_SPECIES.slice();
  let catches = [];       // [{id,date,species,length,weight,lat,lon,depth?}]
  let seq = 0;
  const changeFns = [];   // observers (analytics) notified when catches change
  function notifyChange() { for (const fn of changeFns) { try { fn(catches); } catch (e) { /* ignore */ } } }
  let selected = null;    // currently-chosen species in the entry flow
  let overlayVisible = true;

  const layer = map && L ? L.layerGroup() : null;
  const objs = new Map();

  // ---- persistence -------------------------------------------------------
  function loadSpecies() {
    try {
      const raw = localStorage.getItem(SPECIES_KEY);
      if (raw) { const a = JSON.parse(raw); if (Array.isArray(a) && a.length) species = a.filter((s) => typeof s === "string" && s.trim()); }
    } catch (e) { /* ignore */ }
    if (!species.length) species = DEFAULT_SPECIES.slice();
  }
  function saveSpecies() { try { localStorage.setItem(SPECIES_KEY, JSON.stringify(species)); } catch (e) { /* ignore */ } }
  function loadCatches() {
    try {
      const raw = localStorage.getItem(CATCH_KEY);
      if (raw) { const a = JSON.parse(raw); if (Array.isArray(a)) catches = a.filter((c) => c && typeof c === "object"); }
    } catch (e) { /* ignore */ }
    catches.forEach((c) => { if (!c.id) c.id = "c" + (++seq); const n = parseInt(String(c.id).replace(/\D/g, ""), 10); if (n > seq) seq = n; });
  }
  function saveCatches() { try { localStorage.setItem(CATCH_KEY, JSON.stringify(catches)); } catch (e) { /* ignore */ } }

  // ---- map overlay -------------------------------------------------------
  function popupHtml(c) {
    return `<div class="catch-pop"><b>${escapeHtml(c.species)}</b>` +
      `<div>${VA.fmt(c.length, 0)} cm · ${VA.fmt(c.weight, 1)} kg</div>` +
      `<div class="catch-pop-date">${escapeHtml(c.date || "")}</div></div>`;
  }
  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function catchIcon() {
    return L.divIcon({ className: "", html: `<div class="catch-pin"><span>🎣</span></div>`, iconSize: [26, 26], iconAnchor: [13, 26], popupAnchor: [0, -24] });
  }
  function addCatchObj(c) {
    if (!layer || !Number.isFinite(c.lat) || !Number.isFinite(c.lon)) return;
    const mk = L.marker([c.lat, c.lon], { icon: catchIcon(), zIndexOffset: 500 });
    mk.bindPopup(() => popupHtml(c), { className: "catch-pop-wrap", minWidth: 130 });
    mk.addTo(layer);
    objs.set(c.id, mk);
  }
  function redrawOverlay() {
    if (!layer) return;
    layer.clearLayers(); objs.clear();
    catches.forEach(addCatchObj);
  }
  // Guard so the shared layers-control's add/remove handler (which calls back
  // into setOverlayVisible) doesn't recurse while setOverlayVisible itself
  // toggles the layer on the map.
  let overlaySyncing = false;
  function setOverlayVisible(on) {
    overlayVisible = !!on;
    if (layer && map) {
      overlaySyncing = true;
      if (overlayVisible) { if (!map.hasLayer(layer)) layer.addTo(map); }
      else if (map.hasLayer(layer)) map.removeLayer(layer);
      overlaySyncing = false;
    }
    try { localStorage.setItem(VIS_KEY, overlayVisible ? "1" : "0"); } catch (e) { /* ignore */ }
    const box = $("catch-overlay-show");
    if (box) box.checked = overlayVisible;
  }

  // ---- counts / badges ---------------------------------------------------
  function updateCounts() {
    const cnt = catches.length;
    VA.setText("catch-count", String(cnt));
    const badge = $("catch-card-state");
    if (badge) badge.textContent = cnt ? "● " + cnt : "";
  }

  // ---- panel (open/close) ------------------------------------------------
  const panel = $("catch-panel");
  const scrim = $("catch-scrim");
  function setPanel(on) {
    if (panel) panel.classList.toggle("hidden", !on);
    if (scrim) scrim.classList.toggle("hidden", !on);
    if (on) { renderSpeciesGrid(); renderLogList(); showStep("species"); }
  }
  const openBtn = $("catch-open");
  if (openBtn) openBtn.addEventListener("click", () => setPanel(true));
  const closeBtn = $("catch-close");
  if (closeBtn) closeBtn.addEventListener("click", () => setPanel(false));
  if (scrim) scrim.addEventListener("click", () => setPanel(false));

  function showStep(which) {
    const sp = $("catch-species-step"), en = $("catch-entry-step");
    if (sp) sp.classList.toggle("hidden", which !== "species");
    if (en) en.classList.toggle("hidden", which !== "entry");
  }

  // ---- species grid (in panel) ------------------------------------------
  function renderSpeciesGrid() {
    const grid = $("catch-species-grid");
    if (!grid) return;
    grid.innerHTML = "";
    species.forEach((s) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "catch-species-btn"; b.textContent = s;
      b.addEventListener("click", () => { selected = s; openEntry(s); });
      grid.appendChild(b);
    });
    if (!species.length) {
      const p = document.createElement("div");
      p.className = "hint"; p.textContent = "No species — add some in Settings.";
      grid.appendChild(p);
    }
  }
  function openEntry(s) {
    VA.setText("catch-entry-name", s);
    showStep("entry");
    setNum("len", 30); setNum("wt", 1.0);
  }
  const backBtn = $("catch-back");
  if (backBtn) backBtn.addEventListener("click", () => showStep("species"));

  // ---- length / weight steppers -----------------------------------------
  function numEl(t) { return $(t === "len" ? "catch-len" : "catch-wt"); }
  function outEl(t) { return $(t === "len" ? "catch-len-val" : "catch-wt-val"); }
  function setNum(t, v) {
    const el = numEl(t);
    if (!el) return;
    v = Math.max(0, v);
    const dec = t === "len" ? 0 : 1;
    el.value = v.toFixed(dec);
    const out = outEl(t);
    if (out) out.textContent = v.toFixed(dec);
  }
  function getNum(t) { const el = numEl(t); const v = el ? parseFloat(el.value) : NaN; return Number.isFinite(v) ? v : 0; }
  document.querySelectorAll(".catch-step").forEach((b) => b.addEventListener("click", () => {
    const t = b.dataset.target, d = parseFloat(b.dataset.d);
    setNum(t, getNum(t) + d);
  }));
  ["catch-len", "catch-wt"].forEach((id) => {
    const el = $(id); if (!el) return;
    el.addEventListener("input", () => { const t = id === "catch-len" ? "len" : "wt"; const out = outEl(t); if (out) out.textContent = el.value; });
  });

  // ---- save a catch ------------------------------------------------------
  const saveBtn = $("catch-save");
  if (saveBtn) saveBtn.addEventListener("click", () => {
    if (!selected) { showStep("species"); return; }
    const t = VA.last || {};
    const pos = t.position || (t.truth ? { lat: t.truth.lat, lon: t.truth.lon } : null);
    // Capture the current sounder depth (depth_m) at save time so the catch
    // analytics "best depth" stat can bin catches by depth band. Backward
    // compatible: older catches simply omit `depth` and are skipped in that stat.
    const depth = Number.isFinite(Number(t.depth_m)) ? Number(t.depth_m) : null;
    const c = {
      id: "c" + (++seq),
      date: new Date().toISOString().slice(0, 16).replace("T", " "),
      species: selected,
      length: getNum("len"),
      weight: getNum("wt"),
      lat: pos && Number.isFinite(pos.lat) ? pos.lat : null,
      lon: pos && Number.isFinite(pos.lon) ? pos.lon : null,
      depth: depth,
    };
    catches.push(c);
    saveCatches();
    addCatchObj(c);
    updateCounts();
    renderLogList();
    notifyChange();
    const status = $("catch-status");
    if (status) {
      status.className = "hint ok";
      status.textContent = c.lat === null
        ? `Logged ${c.species} (no GPS position).`
        : `Logged ${c.species} at ${VA.fmt(c.lat, 4)}, ${VA.fmt(c.lon, 4)}.`;
    }
    showStep("species");
  });

  // ---- catch log list (in panel) ----------------------------------------
  function renderLogList() {
    const list = $("catch-log-list");
    if (!list) return;
    list.innerHTML = "";
    if (!catches.length) {
      const li = document.createElement("li");
      li.className = "wp-empty"; li.textContent = "No catches logged yet.";
      list.appendChild(li);
      return;
    }
    catches.slice().reverse().forEach((c) => {
      const li = document.createElement("li");
      const info = document.createElement("span");
      info.className = "catch-log-info";
      info.innerHTML = `<b>${escapeHtml(c.species)}</b> ${VA.fmt(c.length, 0)} cm · ${VA.fmt(c.weight, 1)} kg<br><small>${escapeHtml(c.date || "")}</small>`;
      const go = document.createElement("button");
      go.className = "catch-log-go"; go.textContent = "◎"; go.title = "Show on map";
      go.addEventListener("click", () => {
        if (map && Number.isFinite(c.lat) && Number.isFinite(c.lon)) {
          setOverlayVisible(true);
          map.setView([c.lat, c.lon], Math.max(map.getZoom(), 16));
          const mk = objs.get(c.id); if (mk) mk.openPopup();
          setPanel(false);
        }
      });
      const del = document.createElement("button");
      del.className = "catch-log-del"; del.textContent = "✕"; del.title = "Delete";
      del.addEventListener("click", () => removeCatch(c.id));
      li.append(info, go, del);
      list.appendChild(li);
    });
  }
  function removeCatch(id) {
    const ix = catches.findIndex((c) => c.id === id);
    if (ix === -1) return;
    const mk = objs.get(id);
    if (mk && layer) { layer.removeLayer(mk); objs.delete(id); }
    catches.splice(ix, 1);
    saveCatches(); updateCounts(); renderLogList(); notifyChange();
  }

  // ---- species editor (Settings) ----------------------------------------
  function renderSpeciesEditor() {
    const list = $("species-list");
    if (!list) return;
    list.innerHTML = "";
    species.forEach((s, i) => {
      const li = document.createElement("li");
      const nm = document.createElement("input");
      nm.type = "text"; nm.className = "species-name"; nm.value = s;
      nm.addEventListener("input", () => { species[i] = nm.value; saveSpecies(); });
      nm.addEventListener("blur", () => { species = species.filter((x) => x && x.trim()); saveSpecies(); renderSpeciesEditor(); });
      const del = document.createElement("button");
      del.className = "del"; del.textContent = "✕"; del.setAttribute("aria-label", "delete species");
      del.addEventListener("click", () => { species.splice(i, 1); saveSpecies(); renderSpeciesEditor(); });
      li.append(nm, del);
      list.appendChild(li);
    });
  }
  const speciesAdd = $("species-add"), speciesNew = $("species-new"), speciesReset = $("species-reset");
  function addSpecies() {
    const v = (speciesNew ? speciesNew.value : "").trim();
    if (!v) return;
    species.push(v); saveSpecies();
    if (speciesNew) speciesNew.value = "";
    renderSpeciesEditor();
  }
  if (speciesAdd) speciesAdd.addEventListener("click", addSpecies);
  if (speciesNew) speciesNew.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addSpecies(); } });
  if (speciesReset) speciesReset.addEventListener("click", () => {
    if (!window.confirm("Reset species list to defaults (Pike, Perch, Trout)?")) return;
    species = DEFAULT_SPECIES.slice(); saveSpecies(); renderSpeciesEditor();
  });

  // ---- overlay toggle + export/clear (Settings) -------------------------
  const overlayBox = $("catch-overlay-show");
  if (overlayBox) overlayBox.addEventListener("change", () => setOverlayVisible(overlayBox.checked));
  const exportBtn = $("catch-export");
  if (exportBtn) exportBtn.addEventListener("click", () => {
    const blob = new Blob([JSON.stringify(catches, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "vanchor-catches.json";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  const clearBtn = $("catch-clear");
  if (clearBtn) clearBtn.addEventListener("click", () => {
    if (!catches.length) return;
    if (!window.confirm("Delete all " + catches.length + " logged catches?")) return;
    catches = []; saveCatches(); redrawOverlay(); updateCounts(); renderLogList(); notifyChange();
    const st = $("catch-card-status"); if (st) st.textContent = "";
  });

  // ---- init --------------------------------------------------------------
  loadSpecies();
  loadCatches();
  try { overlayVisible = localStorage.getItem(VIS_KEY) !== "0"; } catch (e) { /* ignore */ }
  renderSpeciesEditor();
  redrawOverlay();
  updateCounts();

  // Register the Catches overlay into the shared layers control (#86) so it can
  // be toggled from the top-left panel as well as the Settings checkbox. The
  // control's add/remove drives setOverlayVisible (guarded against recursion),
  // which mirrors the checkbox + persists. We register BEFORE the initial
  // setOverlayVisible so restoring our own VIS_KEY state also reflects in the
  // control.
  if (layer && VA.map && typeof VA.map.addOverlay === "function") {
    VA.map.addOverlay("Catches", layer, {
      persistKey: VIS_KEY,
      onToggle(on) { if (!overlaySyncing) setOverlayVisible(on); },
    });
  }
  setOverlayVisible(overlayVisible);

  VA.catchLog = {
    count() { return catches.length; },
    setOverlayVisible, isOverlayVisible() { return overlayVisible; },
    // Snapshot of all catches (defensive copy) for the analytics module (#65).
    getAll() { return catches.map((c) => Object.assign({}, c)); },
    // Subscribe to catch-set changes (add / delete / clear). Fires immediately
    // is the caller's job; here we just register.
    onChange(fn) { if (typeof fn === "function") changeFns.push(fn); },
  };
})();
