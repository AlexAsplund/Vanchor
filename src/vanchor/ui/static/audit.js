/* Command audit view (#26).
 *
 * Two views in one collapsible card (Settings -> Command audit):
 *   1. THIS DEVICE — the client-side command queue/state machine from core.js
 *      (VA.commandLog / VA.onCommandState): queued / sent / confirmed / failed.
 *   2. SERVER — GET /api/audit: what every client commanded and whether it was
 *      accepted / denied / errored (source: helm | observer | rest).
 *
 * A compact badge in the summary always shows queued/failed counts so the
 * operator notices buffered or dropped commands even with the card closed.
 * All user-derived text (command type, error, source) is escaped via
 * VA.escapeHtml before it touches innerHTML. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const card = $("audit-card");
  if (!card || !window.VA) return;

  const badge = $("audit-badge");
  const localList = $("audit-local"), localEmpty = $("audit-local-empty");
  const serverList = $("audit-server"), serverEmpty = $("audit-server-empty");
  let timer = null;

  function fmtTime(t) {
    // Accept epoch seconds (server) or ms (Date.now()).
    const ms = t > 1e11 ? t : t * 1000;
    const d = new Date(ms);
    return d.toLocaleTimeString([], { hour12: false });
  }

  // Colour chip for a client-side command state.
  function stateChip(state) {
    const bg = {
      queued: "#b45309", sent: "#2563eb", confirmed: "#15803d",
      failed: "#c81e1e",
    }[state] || "#555";
    return '<span class="badge" style="background:' + bg + '">' +
      VA.escapeHtml(state || "?") + "</span>";
  }

  // Colour chip for a server outcome.
  function outcomeChip(outcome) {
    const bg = {
      accepted: "#15803d", denied: "#b45309", error: "#c81e1e",
    }[outcome] || "#555";
    return '<span class="badge" style="background:' + bg + '">' +
      VA.escapeHtml(outcome || "?") + "</span>";
  }

  function updateBadge() {
    const log = VA.commandLog || [];
    let queued = 0, failed = 0;
    for (const e of log) {
      if (e.state === "queued") queued++;
      else if (e.state === "failed") failed++;
    }
    const parts = [];
    if (queued) parts.push(queued + " queued");
    if (failed) parts.push(failed + " failed");
    badge.textContent = parts.join(" · ");
    badge.style.display = parts.length ? "" : "none";
    badge.style.background = queued ? "#b45309" : (failed ? "#c81e1e" : "");
  }

  function renderLocal() {
    const log = (VA.commandLog || []).slice().reverse();   // newest first
    localList.innerHTML = "";
    localEmpty.style.display = log.length ? "none" : "";
    for (const e of log) {
      const li = document.createElement("li");
      const when = e.tState || e.tCreated || Date.now();
      const extra = e.error ? ' <span class="hint">' + VA.escapeHtml(e.error) + "</span>" : "";
      li.innerHTML =
        '<span class="log-time">' + fmtTime(when) + "</span> " +
        stateChip(e.state) + " " +
        "<b>" + VA.escapeHtml(e.type || "?") + "</b>" + extra;
      localList.appendChild(li);
    }
    updateBadge();
  }

  async function refreshServer() {
    let data;
    try {
      data = await VA.getJSON("/api/audit?n=50");
    } catch (err) {
      serverEmpty.textContent = "Offline — server audit unavailable.";
      serverEmpty.style.display = "";
      serverList.innerHTML = "";
      return;
    }
    const cmds = ((data && data.commands) || []).slice().reverse();  // newest first
    serverList.innerHTML = "";
    if (!cmds.length) {
      serverEmpty.textContent = "No commands recorded.";
      serverEmpty.style.display = "";
      return;
    }
    serverEmpty.style.display = "none";
    for (const c of cmds) {
      const li = document.createElement("li");
      const detail = c.detail ? ' <span class="hint">' + VA.escapeHtml(c.detail) + "</span>" : "";
      li.innerHTML =
        '<span class="log-time">' + fmtTime(c.ts) + "</span> " +
        outcomeChip(c.outcome) + " " +
        "<b>" + VA.escapeHtml(c.type || "?") + "</b> " +
        '<span class="hint">' + VA.escapeHtml(c.source || "?") + "</span>" + detail;
      serverList.appendChild(li);
    }
  }

  // Live: re-render the local list on every command-state transition and keep
  // the badge current even when the card is closed.
  VA.onCommandState(() => {
    updateBadge();
    if (card.open) renderLocal();
  });

  function open() {
    renderLocal();
    refreshServer();
    clearInterval(timer);
    timer = setInterval(() => {
      if (card.open) refreshServer(); else clearInterval(timer);
    }, 3000);
  }

  card.addEventListener("toggle", () => {
    if (card.open) open(); else clearInterval(timer);
  });

  updateBadge();
})();
