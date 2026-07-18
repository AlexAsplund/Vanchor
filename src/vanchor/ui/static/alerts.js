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
  let unreadSev = "info";   // highest severity among unread alerts (for bell color)
  let _aaFiringNow = false;  // live anchor-alarm state (per-entry Recover gate)
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
        if (o && typeof o.unreadSev === "string" && o.unreadSev in SEV_RANK) unreadSev = o.unreadSev;
      }
    } catch (e) { /* ignore corrupt storage */ }
  }
  function save() {
    try { localStorage.setItem(KEY, JSON.stringify({ alerts: alerts.slice(-MAX), unread, unreadSev })); }
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
  // opts (optional): { level: "low"|"medium"|"high", kind: "depth" } — grades
  // the ALARM SOUND only (the log badge stays on severity). kind:"depth" plays
  // the distinct sonar-ping regardless of level; an alarm without a level
  // sounds as medium.
  VA.logAlert = function (severity, message, opts) {
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
    // A newly-logged ALARM gets a distinct haptic buzz + a severity-graded
    // alarm sound (low/medium/high, or the sonar ping for depth); warn and
    // info get the notification chimes (haptics.js / sounds.js; no-ops when
    // unsupported/disabled) — the de-dup above keeps them from repeating.
    if (severity === "alarm" && VA.haptic) VA.haptic("alert");
    if (VA.sound) {
      const o = opts || {};
      if (o.kind === "depth") VA.sound.play("alarm.depth");
      else if (severity === "alarm") {
        const level = (o.level === "low" || o.level === "high") ? o.level : "medium";
        VA.sound.play("alarm." + level);
      } else if (o.level === "low") VA.sound.play("alarm.low");  // graded warn
      else VA.sound.play(severity === "warn" ? "notify" : "notify.info");
    }
    // If the dialog is open, treat as read; otherwise bump the unread badge.
    const dlg = $("alerts-dialog");
    const open = dlg && dlg.open;
    if (!open) {
      unread++;
      // Track highest severity among unread alerts.
      if (SEV_RANK[severity] > SEV_RANK[unreadSev]) unreadSev = severity;
    }
    save();
    renderBadge();
    if (open) renderList();
  };

  // ---- badge ---------------------------------------------------------------
  function renderBadge() {
    const badge = $("alerts-badge");
    const openBtn = $("alerts-open");
    if (!badge) return;
    if (unread > 0) {
      badge.textContent = unread > 99 ? "99+" : String(unread);
      badge.classList.remove("hidden");
      if (openBtn) openBtn.dataset.sev = unreadSev;
    } else {
      badge.classList.add("hidden");
      if (openBtn) delete openBtn.dataset.sev;
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

      // Per-entry actions: "Show on map" if location is available; hold-to-
      // Recover on an anchor-alarm entry while that alarm is STILL firing.
      const wantRecover = a.kind === "anchor_alarm" && _aaFiringNow;
      if ((Number.isFinite(a.lat) && Number.isFinite(a.lon)) || wantRecover) {
        const actions = document.createElement("div");
        actions.className = "alert-actions";
        if (Number.isFinite(a.lat) && Number.isFinite(a.lon)) {
          const showBtn = document.createElement("button");
          showBtn.className = "alerts-action-btn";
          showBtn.textContent = "Show on map";
          showBtn.addEventListener("click", () => {
            close();
            try {
              if (VA.map && VA.map.leaflet) {
                VA.map.leaflet.setView([a.lat, a.lon], 16);
              }
            } catch (_) {}
          });
          actions.appendChild(showBtn);
        }
        if (wantRecover) {
          const recBtn = document.createElement("button");
          recBtn.className = "alerts-action-btn";
          recBtn.textContent = "Recover (hold)";
          const fire = () => { close(); VA.send({ type: "anchor_alarm_recover" }); };
          if (VA.bindHold) VA.bindHold(recBtn, 600, fire);
          else recBtn.addEventListener("click", fire);
          actions.appendChild(recBtn);
        }
        body.appendChild(actions);
      }

      li.appendChild(dot);
      li.appendChild(body);
      list.appendChild(li);
    }
  }

  // ---- open / close --------------------------------------------------------
  function open() {
    const dlg = $("alerts-dialog");
    if (!dlg) return;
    unread = 0; unreadSev = "info"; save(); renderBadge();
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

  // Inline confirm for clear: first tap swaps to "Confirm clear?" UI.
  const clearBtn = $("alerts-clear");
  let _clearPending = false;
  let _clearResetT = null;
  function resetClearBtn() {
    _clearPending = false;
    clearTimeout(_clearResetT);
    if (clearBtn) clearBtn.textContent = "Clear all";
  }
  if (clearBtn) {
    clearBtn.textContent = "Clear all";
    clearBtn.addEventListener("click", () => {
      if (!_clearPending) {
        _clearPending = true;
        clearBtn.textContent = "Confirm clear?";
        _clearResetT = setTimeout(resetClearBtn, 3000);
      } else {
        resetClearBtn();
        // Clear locally.
        alerts = []; unread = 0; unreadSev = "info";
        for (const k in lastSeen) delete lastSeen[k];
        save(); renderBadge(); renderList();
        // Sync to server (best-effort).
        fetch("/api/alerts/clear", { method: "POST" }).catch(() => {});
      }
    });
  }
  const dlg = $("alerts-dialog");
  if (dlg) {
    // Backdrop click closes (native <dialog> click target is the dialog itself).
    dlg.addEventListener("click", (e) => { if (e.target === dlg) close(); });
  }

  // ---- telemetry watcher: log the banner conditions ------------------------
  // Edge-triggered: log only on the false→true transition of each condition so
  // we don't spam, but VA.logAlert's own de-dup is a second safety net.
  // Severity grading (drives the alarm SOUND, see logAlert opts): high = the
  // boat did/needs something drastic right now; low = attention, no urgency.
  // Depth conditions play the distinct sonar ping instead (kind:"depth").
  const prev = { rtl: false, shallow: false, nogo: false, failsafe: false,
                 fixLost: false, drag: false, diverge: false,
                 aalarm: false, aastale: false };
  VA.onTelemetry(function (t) {
    if (!t || typeof t !== "object") return;
    const safety = t.safety || {};
    const link = t.link || null;

    const rtl = !!t.rtl_recommended;
    if (rtl && !prev.rtl) VA.logAlert("warn", "Low battery — return to launch recommended", { level: "low" });
    prev.rtl = rtl;

    const shallow = !!safety.shallow_stop;
    if (shallow && !prev.shallow) VA.logAlert("alarm", "Shallow water — auto-stopped", { kind: "depth" });
    prev.shallow = shallow;

    // Live sounder reads materially shallower than the chart (#45) — a
    // possible uncharted shoal ahead of the shallow-stop tripping.
    const diverge = !!(t.sonar && t.sonar.divergence_alert);
    if (diverge && !prev.diverge) VA.logAlert("warn", "Sounder disagrees with chart — possible uncharted shoal", { kind: "depth" });
    prev.diverge = diverge;

    const nogo = !!safety.nogo_stop;
    if (nogo && !prev.nogo) VA.logAlert("alarm", "No-go zone — auto-stopped", { level: "high" });
    prev.nogo = nogo;

    const failsafe = !!(link && link.failsafe_engaged);
    if (failsafe && !prev.failsafe) {
      // What the failsafe DID depends on the mode + continue-mission setting.
      const action = link.failsafe_action;
      if (action === "continue") {
        VA.logAlert("warn", "Connection lost — continuing mission unsupervised");
      } else if (action === "stop") {
        VA.logAlert("alarm", "Connection lost while driving — motor stopped", { level: "high" });
      } else {
        VA.logAlert("alarm", "Connection lost — holding position (failsafe)", { level: "high" });
      }
    }
    prev.failsafe = failsafe;

    const fixLost = !!safety.fix_lost;
    if (fixLost && !prev.fixLost) VA.logAlert("alarm", "GPS fix lost", { level: "high" });
    prev.fixLost = fixLost;

    const drag = !!safety.drag_alarm;
    if (drag && !prev.drag) VA.logAlert("alarm", "Anchor drag alarm", { level: "high" });
    prev.drag = drag;

    const aa = t.anchor_alarm || {};
    const aalarm = !!aa.firing;
    _aaFiringNow = !!(aa.armed && aalarm);  // gates the per-entry Recover action
    if (aalarm && !prev.aalarm) VA.logAlert("alarm", "Anchor alarm — boat outside the watch circle", { level: "high" });
    prev.aalarm = aalarm;

    const aastale = !!(aa.armed && aa.stale);
    if (aastale && !prev.aastale) VA.logAlert("warn", "Anchor alarm watching blind — GPS stale");
    prev.aastale = aastale;
  });

  // ---- API hydration on boot -----------------------------------------------
  // Merge server-persisted alerts with the localStorage copy so alerts survive
  // a page reload even if localStorage was cleared (e.g. PWA reinstall).
  function hydrateFromAPI() {
    fetch("/api/alerts")
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (!data || !Array.isArray(data.alerts)) return;
        let changed = false;
        for (const a of data.alerts) {
          if (!a || typeof a.message !== "string") continue;
          // Only add entries not already present (by ts+message).
          const dup = alerts.some((x) => x.ts === a.ts && x.message === a.message);
          if (!dup) { alerts.push(a); changed = true; }
        }
        if (changed) {
          alerts.sort((a, b) => a.ts - b.ts);
          if (alerts.length > MAX) alerts = alerts.slice(-MAX);
          save(); renderBadge(); renderList();
        }
      })
      .catch(() => {}); // not available in offline/demo — silent
  }

  // ---- init ----------------------------------------------------------------
  load();
  renderBadge();
  renderList();
  hydrateFromAPI();

  VA.alerts = {
    open, close,
    list() { return alerts.slice(); },
    count() { return alerts.length; },
    unread() { return unread; },
  };
})();
