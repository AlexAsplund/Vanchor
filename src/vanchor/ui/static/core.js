/* Vanchor-NG — core module.
 *
 * Owns the single WebSocket to /ws (5 Hz telemetry down, command JSON up),
 * the command sender, defensive formatting helpers, the NMEA/log console,
 * the continuous (unwrapped) angle accumulator, and a tiny render-subscriber
 * registry so the feature modules (map / hud / steering / app) can each hook
 * the telemetry frame without one giant render() function.
 *
 * Everything is hung off a global `VA` namespace so the non-module <script>
 * files can share it without a build step.
 */
"use strict";

const VA = (window.VA = window.VA || {});

// Latest telemetry frame (so late-loading UI, e.g. the wizard, can read it).
VA.last = null;
VA.simEnabled = false;

// ---- defensive formatting ------------------------------------------------
VA.fmt = function (v, d = 1) {
  return v === null || v === undefined || !Number.isFinite(Number(v))
    ? "—" : Number(v).toFixed(d);
};
VA.num = function (v, d) {
  return v === null || v === undefined || !Number.isFinite(Number(v))
    ? "—" : Number(v).toFixed(d);
};
VA.fin = function (v) { return Number.isFinite(v) ? v : null; };
// setText: cache element lookup and last-written value to avoid redundant DOM
// writes.  ~60-70 calls/frame app-wide with mostly-unchanged values.
// Map<id, {el, last}> — invalidated via isConnected when panels are rebuilt.
const _textCache = new Map();
VA.setText = function (id, v) {
  const s = String(v);
  let entry = _textCache.get(id);
  if (!entry || !entry.el.isConnected) {
    const el = document.getElementById(id);
    if (!el) { _textCache.delete(id); return; }
    entry = { el, last: null };   // null forces first write
    _textCache.set(id, entry);
  }
  if (entry.last === s) return;
  entry.el.textContent = s;
  entry.last = s;
};

// HTML-escape for safe interpolation into innerHTML (text OR attribute context).
// Escapes the five characters that matter so values that arrive from an
// unauthenticated POST (e.g. a debug session `name`) can't inject markup.
VA.escapeHtml = function (s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
};

// ---- continuous unwrapped angle (short-way rotation) ---------------------
// Accumulates the shortest signed delta each frame, keyed per element, so a
// 359°→0° wrap animates the short way instead of spinning all the way around.
const _contRot = {};
VA.continuousAngle = function (key, deg) {
  if (!Number.isFinite(deg)) return _contRot[key] || 0;
  const last = _contRot[key];
  if (last === undefined) { _contRot[key] = deg; return deg; }
  const delta = ((deg - (last % 360) + 540) % 360) - 180; // shortest, [-180,180)
  _contRot[key] = last + delta;
  return _contRot[key];
};

// ---- NMEA / event console ------------------------------------------------
const MAX_LOG = 40;
const logBuf = [];
// Lazy reference to the <details id="console-card"> element; stable once found.
let _consoleCard = null;
function _consoleOpen() {
  if (!_consoleCard) _consoleCard = document.getElementById("console-card");
  return !!(_consoleCard && _consoleCard.open);
}
VA.logLine = function (text) {
  const stamp = new Date().toLocaleTimeString();
  logBuf.push(`[${stamp}] ${text}`);
  while (logBuf.length > MAX_LOG) logBuf.shift();
  const el = document.getElementById("log");
  if (el) {
    el.textContent = logBuf.join("\n");
    // scrollHeight forces a synchronous layout — only pay that cost when the
    // console <details> is actually open and the user can see it.
    if (_consoleOpen()) el.scrollTop = el.scrollHeight;
  }
};
VA.clearLog = function () {
  logBuf.length = 0;
  const el = document.getElementById("log");
  if (el) el.textContent = "";
};
let lastApbStr = null;
function logTelemetry(t) {
  // APB sentences are edge-triggered (rare state change) — always log them
  // regardless of console visibility so they land in the buffer.
  if (t.last_apb && t.last_apb !== lastApbStr) {
    lastApbStr = t.last_apb;
    VA.logLine("APB: " + t.last_apb);
  }
  // The per-frame telemetry summary is only useful when the console is open.
  // Skipping it avoids: toLocaleTimeString(), a ~3 KB string join, a
  // textContent write, and the forced-reflow scrollHeight read — every frame.
  if (!_consoleOpen()) return;
  const pos = t.position ? `${VA.num(t.position.lat, 5)},${VA.num(t.position.lon, 5)}` : "no-fix";
  VA.logLine(
    `${t.mode || "?"} hdg=${VA.num(t.heading_deg, 0)} sog=${VA.num(t.sog_knots, 2)} ` +
    `xte=${VA.num(t.cross_track_m, 1)} pos=${pos}`
  );
}

// ---- full-width safety banners (critical-send + staleness) ---------------
// Minimal, self-contained banners styled inline so they render even if the
// cached CSS is stale. STOP-not-confirmed pins to the top (red); DATA STALE
// pins to the bottom (amber) so the two never overlap.
const STOP_BANNER_ID = "critical-stop-banner";
const STALE_BANNER_ID = "stale-data-banner";
function _banner(id, opts) {
  let el = document.getElementById(id);
  if (!opts.show) { if (el) el.style.display = "none"; return; }
  if (!el) {
    el = document.createElement("div");
    el.id = id;
    el.setAttribute("role", "alert");
    el.setAttribute("aria-live", "assertive");
    el.style.cssText =
      "position:fixed;left:0;right:0;z-index:100000;padding:12px 16px;" +
      "font:700 15px/1.3 system-ui,-apple-system,sans-serif;text-align:center;" +
      "color:#fff;box-shadow:0 2px 10px rgba(0,0,0,.45);letter-spacing:.02em;";
    (document.body || document.documentElement).appendChild(el);
  }
  if (opts.bottom) { el.style.bottom = "0"; el.style.top = ""; }
  else { el.style.top = "0"; el.style.bottom = ""; }
  el.style.background = opts.bg;
  el.style.display = "block";
  el.textContent = opts.text;
}

// ---- staleness watchdog --------------------------------------------------
// Telemetry arrives ~5 Hz. After a silent WiFi drop the socket may not close,
// so the UI would render the last frame forever. Track the last-frame time and
// surface a "DATA STALE" banner once frames stop for >3 s; a fresh frame clears
// it (in dispatch()).
let _lastFrameMs = 0;
const STALE_MS = 3000;
setInterval(() => {
  if (!_lastFrameMs) return;
  const age = Date.now() - _lastFrameMs;
  if (age > STALE_MS) {
    _banner(STALE_BANNER_ID, {
      show: true, bottom: true, bg: "#b45309",
      text: "DATA STALE (" + Math.round(age / 1000) + "s old) — link may be down",
    });
  }
}, 1000);

// ---- critical-command confirmation (STOP) --------------------------------
// A STOP that silently fails during a WiFi drop is a safety hazard. sendCritical
// fires the command over BOTH channels and then watches telemetry: if the boat
// doesn't reflect the stop within ~1.5 s, a red banner stays up until a frame
// confirms it.
let _criticalSeq = 0;
let _critical = null;  // { id, confirm }
function _stopConfirmed(t) {
  if (!t) return false;
  const thr = t.motor ? Number(t.motor.thrust) : NaN;
  return t.mode === "manual" && Number.isFinite(thr) && Math.abs(thr) < 0.05;
}

// ---- render-subscriber registry ------------------------------------------
const subscribers = [];
VA.onTelemetry = function (fn) { subscribers.push(fn); };

function dispatch(t) {
  VA.last = t;
  VA.simEnabled = !!t.sim_enabled;
  // Fresh frame: clear any staleness banner and confirm pending critical cmds.
  _lastFrameMs = Date.now();
  _banner(STALE_BANNER_ID, { show: false });
  if (_critical && _critical.confirm(t)) {
    _critical = null;
    _banner(STOP_BANNER_ID, { show: false });
  }
  for (const fn of subscribers) {
    try { fn(t); } catch (err) { VA.logLine("render error: " + err); }
  }
  logTelemetry(t);
}

// ---- websocket -----------------------------------------------------------
let ws;
const connListeners = [];
VA.onConnState = function (fn) { connListeners.push(fn); };
function setConn(state, text) {
  for (const fn of connListeners) { try { fn(state, text); } catch (e) { /* ignore */ } }
}

VA.connect = function () {
  let _pingInterval = null;
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => {
    setConn("connected", "connected");
    // Application-level heartbeat: send a ping every 2 s so the server's
    // _last_client_seen advances while the socket is live.  This ensures the
    // link-loss failsafe fires within link_loss_timeout_s of a true half-open
    // connection, not delayed by the ~40 s transport-level WS ping.
    // Sent directly on the socket (not via VA.send) to avoid spamming the log.
    _pingInterval = setInterval(() => {
      if (ws && ws.readyState === 1) ws.send('{"type":"ping"}');
    }, 2000);
  };
  ws.onclose = () => {
    clearInterval(_pingInterval);
    _pingInterval = null;
    setConn("disconnected", "reconnecting…");
    setTimeout(VA.connect, 1000);
  };
  ws.onerror = () => setConn("disconnected", "connection error");
  ws.onmessage = (ev) => {
    let t;
    try { t = JSON.parse(ev.data); } catch (err) { VA.logLine("bad telemetry: " + err); return; }
    if (t.type === "pong") return;  // heartbeat reply; not a telemetry frame
    dispatch(t);
  };
};

// Send a command (WS up; falls back to POST /api/command if the socket is down).
VA.send = function (cmd) {
  VA.logLine("» " + JSON.stringify(cmd));
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(cmd));
  else fetch("/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cmd),
  }).catch((e) => VA.logLine("command POST failed: " + e));
};

// Send a SAFETY-CRITICAL command (STOP). Fires over the WS AND POSTs to
// /api/command simultaneously (both best-effort, so a dead socket can't swallow
// it), then watches telemetry: if the boat doesn't reflect the stop within
// ~1.5 s, a prominent red banner stays up until a frame confirms it. `confirmFn`
// defaults to "mode is manual and thrust ~0" (what a stop produces).
VA.sendCritical = function (cmd, confirmFn) {
  VA.logLine("»! " + JSON.stringify(cmd));
  try { if (ws && ws.readyState === 1) ws.send(JSON.stringify(cmd)); }
  catch (e) { VA.logLine("critical WS send failed: " + e); }
  fetch("/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cmd),
  }).catch((e) => VA.logLine("critical command POST failed: " + e));
  const id = ++_criticalSeq;
  _critical = { id, confirm: confirmFn || _stopConfirmed };
  setTimeout(() => {
    if (_critical && _critical.id === id) {
      _banner(STOP_BANNER_ID, {
        show: true, bg: "#c81e1e",
        text: "STOP NOT CONFIRMED — check link",
      });
    }
  }, 1500);
};

// Convenience JSON REST helpers used by the wizard / tuner.
VA.getJSON = async function (url) {
  const r = await fetch(url);
  return r.json();
};
VA.postJSON = async function (url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return r.json();
};

// ---- PWA service worker (#82) -------------------------------------------
// Register the service worker so the app shell + vendored libs are precached
// and the page loads fully offline on the boat. Served at root scope by the
// server so it can control the whole origin. Guarded for support / failures.
if ("serviceWorker" in navigator) {
  // When a NEW service worker activates and takes control (after an update), the
  // page is still showing the OLD cached shell/CSS/JS. Reload once so it swaps to
  // the fresh assets instead of lingering stale — the reason a UI change could
  // "not show up" until a manual hard-refresh. Guarded against reload loops.
  let _swReloading = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (_swReloading) return;
    _swReloading = true;
    window.location.reload();
  });
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((err) => {
      console.warn("[vanchor] service worker registration failed:", err);
    });
  });
}
