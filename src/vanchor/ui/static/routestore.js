/* Vanchor-NG — Save / Load named routes (task #48).
 *
 * Saves the current pending (pre-start) waypoints under a name in localStorage
 * (`vanchor-routes`) and loads them back into the route editor *unstarted* via
 * VA.map.setPending(...) + VA.routeEditor.refresh(). Supports delete and
 * GPX export / import.
 *
 * Routes are stored as { name: [{name,lat,lon}, ...] }.
 */
"use strict";

(function () {
  if (!window.VA || !VA.map) return;
  const $ = (id) => document.getElementById(id);

  const STORE_KEY = "vanchor-routes";
  const sel = $("route-load-select");
  const saveBtn = $("route-save");
  const loadBtn = $("route-load-go");
  const delBtn = $("route-delete");
  const exportBtn = $("route-export");
  const importInput = $("route-import");
  const statusEl = $("route-store-status");
  const stateBadge = $("route-store-state");
  if (!sel) return;

  let routes = {};   // { name: [{name,lat,lon}] }

  function load() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if (raw) { const o = JSON.parse(raw); if (o && typeof o === "object") routes = o; }
    } catch (e) { routes = {}; }
  }
  function save() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(routes)); } catch (e) { /* ignore */ }
    render();
  }
  function setStatus(msg, kind) {
    if (!statusEl) return;
    statusEl.textContent = msg || "";
    statusEl.className = "hint" + (kind ? " " + kind : "");
  }

  function render() {
    const names = Object.keys(routes);
    sel.innerHTML = "";
    if (!names.length) {
      const o = document.createElement("option");
      o.value = ""; o.textContent = "— none saved —";
      sel.appendChild(o);
    } else {
      names.sort((a, b) => a.localeCompare(b)).forEach((n) => {
        const o = document.createElement("option");
        o.value = n; o.textContent = `${n} (${routes[n].length})`;
        sel.appendChild(o);
      });
    }
    if (stateBadge) stateBadge.textContent = names.length ? "● " + names.length : "";
    const has = names.length > 0;
    if (loadBtn) loadBtn.disabled = !has;
    if (delBtn) delBtn.disabled = !has;
    if (exportBtn) exportBtn.disabled = !has;
  }

  if (saveBtn) saveBtn.addEventListener("click", () => {
    const pending = VA.map.pending();
    if (!pending.length) { setStatus("No waypoints to save. Drop some first.", "err"); return; }
    let name = (window.prompt("Save route as:", "Route " + (Object.keys(routes).length + 1)) || "").trim();
    if (!name) return;
    if (routes[name] && !window.confirm(`Overwrite route "${name}"?`)) return;
    routes[name] = pending.map((w) => ({ name: w.name, lat: w.lat, lon: w.lon }));
    save();
    sel.value = name;
    setStatus(`Saved "${name}" (${routes[name].length} waypoints).`, "ok");
  });

  function loadSelected() {
    const name = sel.value;
    if (!name || !routes[name]) { setStatus("Pick a saved route.", "err"); return; }
    const wps = routes[name]
      .filter((w) => w && Number.isFinite(w.lat) && Number.isFinite(w.lon))
      .map((w, i) => ({ name: w.name || ("WP" + (i + 1)), lat: w.lat, lon: w.lon }));
    VA.map.setPending(wps);
    if (VA.routeEditor && VA.routeEditor.clearLoop) VA.routeEditor.clearLoop();
    if (VA.routeEditor && VA.routeEditor.refresh) VA.routeEditor.refresh();
    setStatus(`Loaded "${name}" — review and press Start route.`, "ok");
  }
  if (loadBtn) loadBtn.addEventListener("click", loadSelected);

  if (delBtn) delBtn.addEventListener("click", () => {
    const name = sel.value;
    if (!name || !routes[name]) return;
    if (!window.confirm(`Delete saved route "${name}"?`)) return;
    delete routes[name];
    save();
    setStatus(`Deleted "${name}".`, "");
  });

  // ---- GPX export / import ----
  function toGpx(name, wps) {
    const pts = wps.map((w) =>
      `    <rtept lat="${w.lat}" lon="${w.lon}"><name>${escapeXml(w.name || "")}</name></rtept>`
    ).join("\n");
    return [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<gpx version="1.1" creator="vanchor-ng">',
      `  <rte><name>${escapeXml(name)}</name>`,
      pts,
      "  </rte>", "</gpx>",
    ].join("\n");
  }
  function escapeXml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  if (exportBtn) exportBtn.addEventListener("click", () => {
    const name = sel.value;
    if (!name || !routes[name]) { setStatus("Pick a saved route to export.", "err"); return; }
    const blob = new Blob([toGpx(name, routes[name])], { type: "application/gpx+xml" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name.replace(/[^\w\- ]+/g, "_") + ".gpx";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  function importGpx(text, fname) {
    let doc;
    try { doc = new DOMParser().parseFromString(text, "application/xml"); } catch (e) { return false; }
    if (!doc || doc.getElementsByTagName("parsererror").length) return false;
    const pts = [];
    ["rtept", "wpt", "trkpt"].forEach((tag) => {
      if (pts.length) return; // prefer route points, then waypoints, then track
      const els = doc.getElementsByTagName(tag);
      for (let i = 0; i < els.length; i++) {
        const lat = parseFloat(els[i].getAttribute("lat"));
        const lon = parseFloat(els[i].getAttribute("lon"));
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
        const nm = els[i].getElementsByTagName("name")[0];
        pts.push({ name: nm ? nm.textContent.trim() : "WP" + (pts.length + 1), lat, lon });
      }
    });
    if (!pts.length) return false;
    const rteName = doc.getElementsByTagName("name")[0];
    let name = (rteName ? rteName.textContent.trim() : "") ||
      (fname ? fname.replace(/\.gpx$/i, "") : "") || "Imported route";
    if (routes[name]) name = name + " " + (Object.keys(routes).length + 1);
    routes[name] = pts;
    save();
    sel.value = name;
    // also drop straight into the editor for review
    VA.map.setPending(pts.map((w) => ({ name: w.name, lat: w.lat, lon: w.lon })));
    if (VA.routeEditor && VA.routeEditor.clearLoop) VA.routeEditor.clearLoop();
    if (VA.routeEditor && VA.routeEditor.refresh) VA.routeEditor.refresh();
    setStatus(`Imported "${name}" (${pts.length} waypoints).`, "ok");
    return true;
  }
  if (importInput) importInput.addEventListener("change", () => {
    const file = importInput.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const ok = importGpx(String(reader.result || ""), file.name);
      if (!ok) setStatus("No waypoints found in that GPX file.", "err");
    };
    reader.readAsText(file);
    importInput.value = "";
  });

  load();
  render();
})();
