/* Vanchor-NG — alert history (#97).
 *
 * Safety banners (battery RTL / shallow / no-go / link-loss) flash and are easy
 * to miss. This module keeps a persistent log of recent alerts:
 *
 *   - a bell button in the top bar with an unread badge,
 *   - a dialog listing recent alerts (newest first) with timestamp + severity +
 *     message, keeping the last ~50 (persisted to localStorage),
 *   - a tiny `VA.logAlert(severity, message)` helper (callable from anywhere,
 *     e.g. safety.js) that de-duplicates a still-active condition so it isn't
 *     re-logged every 5 Hz telemetry frame,
 *   - a telemetry hook that watches the very conditions that raise the banners,
 *     so an alert is recorded even if its banner was on-screen too briefly.
 *
 * Everything degrades gracefully if the markup is missing.
 */
"use strict";

(function () {
  const VA = (window.VA = window.VA || {});
  const $ = (id) => document.getElementById(id);

  const KEY = "vanchor-alerts";
  const MAX = 50;
  // A still-active condition keyed by `severity|message` is only re-logged once
  // it has been clear for this long (so a persistent banner logs once, not 5x/s).
  const REPEAT_MS = 60000;

  let alerts = [];          // [{ ts, severity, message }]  newest LAST in storage
  let unread = 0;
  const lastSeen = Object.create(null);  // key -> last logged epoch ms

  function load() {
    try {
      const raw = localStorage.getItem(KEY);
      if (raw) {
        const o = JSON.parse(raw);
        if (o && Array.isArray(o.alerts)) {
          alerts = o.alerts.filter((a) => a && typeof a.message === "string").slice(-MAX);
        }
        if (o && Number.isFinite(o.unread)) unread = Math.max(0, o.unread | 0);
      }
    } catch (e) { /* ignore corrupt storage */ }
  }
  function save() {
    try { localStorage.setItem(KEY, JSON.stringify({ alerts: alerts.slice(-MAX), unread })); }
    catch (e) { /* ignore */ }
  }

  const SEV_RANK = { info: 0, warn: 1, alarm: 2 };
  function normSeverity(s) {
    s = String(s || "info").toLowerCase();
    return s in SEV_RANK ? s : "info";
  }

  function fmtTime(ts) {
    try { return new Date(ts).toLocaleTimeString(); } catch (e) { return ""; }
  }
  function fmtDate(ts) {
    try { return new Date(ts).toLocaleDateString(); } catch (e) { return ""; }
  }

  // ---- public: log an alert ------------------------------------------------
  VA.logAlert = function (severity, message) {
    severity = normSeverity(severity);
    message = String(message == null ? "" : message).trim();
    if (!message) return;
    const key = severity + "|" + message;
    const now = Date.now();
    // De-duplicate a still-active / rapidly-repeating condition.
    if (lastSeen[key] && now - lastSeen[key] < REPEAT_MS) { lastSeen[key] = now; return; }
    lastSeen[key] = now;

    alerts.push({ ts: now, severity, message });
    if (alerts.length > MAX) alerts = alerts.slice(-MAX);
    // If the dialog is open, treat as read; otherwise bump the unread badge.
    const dlg = $("alerts-dialog");
    const open = dlg && dlg.open;
    if (!open) unread++;
    save();
    renderBadge();
    if (open) renderList();
  };

  // ---- badge ---------------------------------------------------------------
  function renderBadge() {
    const badge = $("alerts-badge");
    if (!badge) return;
    if (unread > 0) {
      badge.textContent = unread > 99 ? "99+" : String(unread);
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
  }

  // ---- list ----------------------------------------------------------------
  function renderList() {
    const list = $("alerts-list");
    const empty = $("alerts-empty");
    if (!list) return;
    list.textContent = "";
    if (!alerts.length) {
      if (empty) empty.classList.remove("hidden");
      return;
    }
    if (empty) empty.classList.add("hidden");
    // newest first
    for (let i = alerts.length - 1; i >= 0; i--) {
      const a = alerts[i];
      const li = document.createElement("li");
      li.className = "alert-item alert-" + a.severity;

      const dot = document.createElement("span");
      dot.className = "alert-sev";
      dot.setAttribute("aria-hidden", "true");

      const body = document.createElement("div");
      body.className = "alert-body";

      const msg = document.createElement("div");
      msg.className = "alert-msg";
      msg.textContent = a.message;

      const meta = document.createElement("div");
      meta.className = "alert-meta";
      meta.textContent = a.severity.toUpperCase() + " · " + fmtTime(a.ts) + " · " + fmtDate(a.ts);

      body.appendChild(msg);
      body.appendChild(meta);
      li.appendChild(dot);
      li.appendChild(body);
      list.appendChild(li);
    }
  }

  // ---- open / close --------------------------------------------------------
  function open() {
    const dlg = $("alerts-dialog");
    if (!dlg) return;
    unread = 0; save(); renderBadge();
    renderList();
    if (typeof dlg.showModal === "function") { if (!dlg.open) dlg.showModal(); }
    else dlg.setAttribute("open", "");
  }
  function close() {
    const dlg = $("alerts-dialog");
    if (!dlg) return;
    if (typeof dlg.close === "function" && dlg.open) dlg.close();
    else dlg.removeAttribute("open");
  }

  const openBtn = $("alerts-open");
  if (openBtn) openBtn.addEventListener("click", open);
  const closeBtn = $("alerts-close");
  if (closeBtn) closeBtn.addEventListener("click", close);
  const clearBtn = $("alerts-clear");
  if (clearBtn) clearBtn.addEventListener("click", () => {
    alerts = []; unread = 0;
    for (const k in lastSeen) delete lastSeen[k];
    save(); renderBadge(); renderList();
  });
  const dlg = $("alerts-dialog");
  if (dlg) {
    // Backdrop click closes (native <dialog> click target is the dialog itself).
    dlg.addEventListener("click", (e) => { if (e.target === dlg) close(); });
  }

  // ---- telemetry watcher: log the banner conditions ------------------------
  // Edge-triggered: log only on the false→true transition of each condition so
  // we don't spam, but VA.logAlert's own de-dup is a second safety net.
  const prev = { rtl: false, shallow: false, nogo: false, failsafe: false, fixLost: false, drag: false };
  VA.onTelemetry(function (t) {
    if (!t || typeof t !== "object") return;
    const safety = t.safety || {};
    const link = t.link || null;

    const rtl = !!t.rtl_recommended;
    if (rtl && !prev.rtl) VA.logAlert("warn", "Low battery — return to launch recommended");
    prev.rtl = rtl;

    const shallow = !!safety.shallow_stop;
    if (shallow && !prev.shallow) VA.logAlert("alarm", "Shallow water — auto-stopped");
    prev.shallow = shallow;

    const nogo = !!safety.nogo_stop;
    if (nogo && !prev.nogo) VA.logAlert("alarm", "No-go zone — auto-stopped");
    prev.nogo = nogo;

    const failsafe = !!(link && link.failsafe_engaged);
    if (failsafe && !prev.failsafe) VA.logAlert("alarm", "Connection lost — holding position (failsafe)");
    prev.failsafe = failsafe;

    const fixLost = !!safety.fix_lost;
    if (fixLost && !prev.fixLost) VA.logAlert("alarm", "GPS fix lost");
    prev.fixLost = fixLost;

    const drag = !!safety.drag_alarm;
    if (drag && !prev.drag) VA.logAlert("alarm", "Anchor drag alarm");
    prev.drag = drag;
  });

  // ---- init ----------------------------------------------------------------
  load();
  renderBadge();
  renderList();

  VA.alerts = {
    open, close,
    list() { return alerts.slice(); },
    count() { return alerts.length; },
    unread() { return unread; },
  };
})();
