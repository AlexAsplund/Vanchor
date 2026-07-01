/* View logs: the in-memory application log (GET /api/logs), filterable by
 * severity + text. Opened from Settings -> Data & diagnostics -> "View logs"
 * (and VA.viewLogs()). Newest first; auto-refreshes while open. */
(function () {
  const $ = (id) => document.getElementById(id);
  const dlg = $("logs-dialog");
  if (!dlg) return;
  const list = $("logs-list"), empty = $("logs-empty");
  const levelSel = $("logs-level"), search = $("logs-search");
  let timer = null, searchDebounce = null;

  function fmtTime(t) {
    const d = new Date(t * 1000);
    return d.toLocaleTimeString([], { hour12: false }) +
      "." + String(d.getMilliseconds()).padStart(3, "0");
  }

  async function refresh() {
    const level = levelSel ? levelSel.value : "INFO";
    const q = (search && search.value.trim())
      ? "&contains=" + encodeURIComponent(search.value.trim()) : "";
    let r;
    try {
      r = await VA.getJSON("/api/logs?n=400&level=" + level + q);
    } catch (e) {
      empty.textContent = "Could not load logs."; empty.classList.remove("hidden");
      list.innerHTML = ""; return;
    }
    const recs = (r && r.records) || [];
    list.innerHTML = "";
    if (!recs.length) {
      empty.textContent = "No log records."; empty.classList.remove("hidden"); return;
    }
    empty.classList.add("hidden");
    for (let i = recs.length - 1; i >= 0; i--) {   // newest first
      const rec = recs[i];
      const li = document.createElement("li");
      li.className = "log-row log-" + String(rec.level || "INFO").toLowerCase();
      const tm = document.createElement("span"); tm.className = "log-time"; tm.textContent = fmtTime(rec.t);
      const lvl = document.createElement("span"); lvl.className = "log-lvl"; lvl.textContent = rec.level;
      const nm = document.createElement("span"); nm.className = "log-name";
      nm.textContent = String(rec.name || "").replace(/^vanchor\.?/, "");
      const msg = document.createElement("span"); msg.className = "log-msg"; msg.textContent = rec.msg;
      li.append(tm, lvl, nm, msg);
      list.appendChild(li);
    }
  }

  function open() {
    refresh();
    if (typeof dlg.showModal === "function") { if (!dlg.open) dlg.showModal(); }
    else dlg.setAttribute("open", "");
    clearInterval(timer);
    timer = setInterval(() => { if (dlg.open) refresh(); else clearInterval(timer); }, 3000);
  }
  function close() { clearInterval(timer); if (dlg.open) dlg.close(); else dlg.removeAttribute("open"); }

  if ($("logs-open")) $("logs-open").addEventListener("click", open);
  if ($("logs-close")) $("logs-close").addEventListener("click", close);
  if ($("logs-refresh")) $("logs-refresh").addEventListener("click", refresh);
  if (levelSel) levelSel.addEventListener("change", refresh);
  if (search) search.addEventListener("input", () => {
    clearTimeout(searchDebounce); searchDebounce = setTimeout(refresh, 250);
  });
  dlg.addEventListener("close", () => clearInterval(timer));

  VA.viewLogs = open;   // let other UI (menu items) open the log viewer
})();
