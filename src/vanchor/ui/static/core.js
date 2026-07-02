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
let _critical = null;  // { id, confirm, seq }

// ---- versioned WS envelope + command acks (#21) --------------------------
// Every server->client message carries a top-level `type` and `v`. Commands we
// send may carry an incrementing `seq`; the server replies {ack}/{nack} which
// resolves a pending entry here. Telemetry frames keep their flat shape (see
// dispatch) so the ~30 modules reading t.mode/t.sog directly are unaffected.
const _CLIENT_PROTOCOL_V = 1;
let _vWarned = false;
let _sendSeq = 0;
const _pending = new Map();  // seq -> { onResult, timer }

function _clearPending(seq) {
  const p = _pending.get(seq);
  if (p) { clearTimeout(p.timer); _pending.delete(seq); }
}
// Register a command awaiting an ack/nack. `onResult(ok, error)` fires on the
// reply; a 3 s timeout drops it and logs (acks themselves are NOT logged, to
// keep the NMEA console clean).
function _registerPending(seq, onResult) {
  const timer = setTimeout(() => {
    _pending.delete(seq);
    VA.logLine("command #" + seq + " not acked within 3s");
    if (onResult) try { onResult(false, "timeout"); } catch (e) { /* ignore */ }
  }, 3000);
  _pending.set(seq, { onResult, timer });
}
function _resolvePending(seq, ok, error) {
  const p = _pending.get(seq);
  if (!p) return;
  _pending.delete(seq);
  clearTimeout(p.timer);
  if (p.onResult) try { p.onResult(ok, error); } catch (e) { /* ignore */ }
}
function _stopConfirmed(t) {
  if (!t) return false;
  const thr = t.motor ? Number(t.motor.thrust) : NaN;
  return t.mode === "manual" && Number.isFinite(thr) && Math.abs(thr) < 0.05;
}

// ---- multi-client roles (#24) --------------------------------------------
// The server designates one connected client the HELM; the rest are OBSERVERS.
// A `{type:"role"}` message arrives on connect and on every role change with
// this client's role plus the shared presence scalars (clients/helm_present).
// STOP always works regardless of role (server-enforced safety floor).
VA.role = "observer";        // "helm" | "observer" — until the first role frame
VA.helmPresent = false;      // is ANY client currently the helm?
VA.clientCount = 1;          // number of connected clients
VA.isHelm = function () { return VA.role === "helm"; };
// Ask the server to transfer the helm to this client (cooperative, no auth).
VA.takeHelm = function () {
  if (ws && ws.readyState === 1) ws.send('{"type":"take_helm"}');
};
const roleListeners = [];
// Subscribe to role/presence changes: fn({role, helmPresent, clients}).
VA.onRole = function (fn) { roleListeners.push(fn); };
function dispatchRole(m) {
  VA.role = m.role === "helm" ? "helm" : "observer";
  VA.helmPresent = !!m.helm_present;
  if (Number.isFinite(m.clients)) VA.clientCount = m.clients;
  const info = { role: VA.role, helmPresent: VA.helmPresent, clients: VA.clientCount };
  for (const fn of roleListeners) {
    try { fn(info); } catch (err) { VA.logLine("role handler error: " + err); }
  }
}

// ---- render-subscriber registry ------------------------------------------
const subscribers = [];
VA.onTelemetry = function (fn) { subscribers.push(fn); };

function dispatch(t) {
  VA.last = t;
  VA.simEnabled = !!t.sim_enabled;
  // Shared presence scalars ride the high-rate broadcast frame (#24) — keep the
  // live count/helm-present fresh between the (rarer) role messages. Role itself
  // is NOT in telemetry; it only changes via a `{type:"role"}` message.
  if ("clients" in t || "helm_present" in t) {
    let changed = false;
    if (Number.isFinite(t.clients) && t.clients !== VA.clientCount) {
      VA.clientCount = t.clients; changed = true;
    }
    if ("helm_present" in t && !!t.helm_present !== VA.helmPresent) {
      VA.helmPresent = !!t.helm_present; changed = true;
    }
    if (changed) {
      const info = { role: VA.role, helmPresent: VA.helmPresent, clients: VA.clientCount };
      for (const fn of roleListeners) {
        try { fn(info); } catch (err) { VA.logLine("role handler error: " + err); }
      }
    }
  }
  // Fresh frame: clear any staleness banner and confirm pending critical cmds.
  _lastFrameMs = Date.now();
  _banner(STALE_BANNER_ID, { show: false });
  if (_critical && _critical.confirm(t)) {
    // Telemetry-frame confirmation arrived first; drop the pending ack watcher
    // so it doesn't later log a spurious "not acked" for a command that worked.
    if (_critical.seq != null) _clearPending(_critical.seq);
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
    // Offline-first command queue (#26): the link is back, so replay commands
    // buffered while it was down (dropping any past their TTL) and re-send once
    // any command that was sent-but-unacked when the socket dropped.
    _flushQueue();
    _resendUnacked();
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
    // Snapshot sent-but-unacked commands so they can be re-sent once on the next
    // connect (#26, rule c) — before their ack timers expire.
    _markSentForResend();
    setConn("disconnected", "reconnecting…");
    setTimeout(VA.connect, 1000);
  };
  ws.onerror = () => setConn("disconnected", "connection error");
  ws.onmessage = (ev) => {
    let t;
    try { t = JSON.parse(ev.data); } catch (err) { VA.logLine("bad telemetry: " + err); return; }
    if (typeof t.v === "number" && t.v !== _CLIENT_PROTOCOL_V && !_vWarned) {
      _vWarned = true;
      console.warn("vanchor: WS protocol version mismatch — client " +
        _CLIENT_PROTOCOL_V + ", server " + t.v);
    }
    switch (t.type) {
      case "pong": return;                       // heartbeat reply; not telemetry
      case "ack":  _resolvePending(t.seq, true); return;
      case "nack": _resolvePending(t.seq, false, t.error); return;
      case "role": dispatchRole(t); return;      // multi-client role/presence (#24)
      case "role_denied":                        // command rejected: not the helm
        VA.logLine("⛔ " + (t.error || "observer — take the helm to command"));
        if (t.seq != null) _resolvePending(t.seq, false, t.error);
        return;
      default:     dispatch(t); return;          // "telemetry" or legacy no-type
    }
  };
};

// ---- offline-first command queue + per-command state machine (#26) -------
// Every VA.send() command moves through an explicit state machine:
//   queued    — the WS is not open; the command is buffered CLIENT-SIDE
//               (not POSTed) until the link comes back.
//   sent      — written to the WS, awaiting the server's {ack}/{nack}.
//   confirmed — {ack} received (the server ran it).
//   failed    — {nack}, a 3 s ack timeout, or it expired while queued.
// `VA.commandLog` is a bounded list of recent commands with their state +
// timestamps; `VA.onCommandState(fn)` fires on every transition.
//
// SAFETY RULES on reconnect (see _flushQueue / _resendUnacked):
//  (a) `stop` is NEVER queued — VA.sendCritical dual-paths it immediately over
//      WS + POST (unchanged below), so a STOP can never sit in a buffer.
//  (b) a command that sat QUEUED (never sent) longer than QUEUE_TTL_MS is NOT
//      auto-replayed — it is marked failed("expired"). This stops a stale
//      motor-engage tapped during an outage from firing minutes later.
//  (c) a command that was SENT but unacked when the link dropped MAY be re-sent
//      ONCE on reconnect to obtain confirmation. VA.send carries idempotent
//      mode/setpoint commands (e.g. heading_hold, anchor_hold, cruise), which
//      are safe to repeat; a genuinely non-idempotent action must not rely on
//      this path.
const QUEUE_TTL_MS = 5000;   // rule (b): max age a queued command may be replayed
const MAX_CMD_LOG = 50;
VA.commandLog = [];          // bounded list of recent commands (with state)
const _cmdQueue = [];        // entries in "queued" state awaiting a live socket
const _resend = [];          // rule (c): sent-but-unacked entries to retry once
const cmdStateListeners = [];
// Subscribe to command-state transitions: fn(entry, VA.commandLog).
VA.onCommandState = function (fn) { cmdStateListeners.push(fn); };
function _emitCmdState(entry) {
  for (const fn of cmdStateListeners) {
    try { fn(entry, VA.commandLog); } catch (e) { /* ignore */ }
  }
}
function _logCmd(entry) {
  VA.commandLog.push(entry);
  while (VA.commandLog.length > MAX_CMD_LOG) VA.commandLog.shift();
}
function _setCmdState(entry, state, extra) {
  entry.state = state;
  entry.tState = Date.now();
  if (extra) Object.assign(entry, extra);
  _emitCmdState(entry);
}
// Write an entry to the (open) WS and register its ack/nack handler. Clears any
// prior pending for this seq first so a stale 3 s timer from a previous attempt
// (a resend re-uses the same seq) can't delete the fresh pending entry.
function _sendEntry(entry) {
  _clearPending(entry.seq);
  _registerPending(entry.seq, (ok, error) => {
    if (ok) _setCmdState(entry, "confirmed", { tConfirmed: Date.now() });
    else _setCmdState(entry, "failed", { error: error || "failed" });
  });
  try {
    ws.send(JSON.stringify(Object.assign({}, entry.cmd, { seq: entry.seq })));
    _setCmdState(entry, "sent", { tSent: Date.now() });
  } catch (e) {
    _clearPending(entry.seq);
    _setCmdState(entry, "failed", { error: "send error" });
    VA.logLine("command send failed: " + e);
  }
}
// Reconnect: flush queued commands, dropping any that outlived QUEUE_TTL_MS.
function _flushQueue() {
  if (!_cmdQueue.length) return;
  const now = Date.now();
  const pending = _cmdQueue.splice(0, _cmdQueue.length);
  for (const entry of pending) {
    if (now - (entry.tQueued || entry.tCreated) > QUEUE_TTL_MS) {
      _setCmdState(entry, "failed", { error: "expired" });   // rule (b)
      VA.logLine("queued command expired (not replayed): " + (entry.type || "?"));
    } else if (ws && ws.readyState === 1) {
      _sendEntry(entry);
    } else {
      _cmdQueue.push(entry);   // socket died again mid-flush; keep it buffered
    }
  }
}
// Link dropped: snapshot the SENT-but-unacked commands so they can be re-sent
// once on reconnect (rule c). Called from ws.onclose before their ack timers
// expire; their state may transition to failed(timeout) meanwhile — the resend
// still fires and re-drives them to confirmed.
function _markSentForResend() {
  for (const e of VA.commandLog) {
    if (e.state === "sent" && (e.resends || 0) < 1 && _resend.indexOf(e) === -1) {
      _resend.push(e);
    }
  }
}
// Reconnect: re-send each sent-but-unacked command exactly once (rule c).
function _resendUnacked() {
  const items = _resend.splice(0, _resend.length);
  for (const entry of items) {
    if (ws && ws.readyState === 1) {
      entry.resends = (entry.resends || 0) + 1;
      VA.logLine("re-sending unacked command (idempotent): " + (entry.type || "?"));
      _sendEntry(entry);
    }
  }
}

// Send a command. When the WS is open it goes straight to "sent" and is ack'd;
// when the socket is down it is QUEUED client-side (state machine + safety rules
// above) rather than POSTed, so an offline tap is replayed on reconnect (subject
// to the TTL) instead of silently lost. Returns the command's seq.
VA.send = function (cmd) {
  VA.logLine("» " + JSON.stringify(cmd));
  const seq = ++_sendSeq;
  const entry = {
    seq, type: cmd && cmd.type, cmd, state: "new",
    tCreated: Date.now(), resends: 0,
  };
  _logCmd(entry);
  if (ws && ws.readyState === 1) {
    _sendEntry(entry);
  } else {
    entry.tQueued = Date.now();
    _cmdQueue.push(entry);
    _setCmdState(entry, "queued", { tQueued: entry.tQueued });
    VA.logLine("command queued (offline): " + ((cmd && cmd.type) || "?"));
  }
  return seq;
};

// Send a SAFETY-CRITICAL command (STOP). Fires over the WS AND POSTs to
// /api/command simultaneously (both best-effort, so a dead socket can't swallow
// it), then watches telemetry: if the boat doesn't reflect the stop within
// ~1.5 s, a prominent red banner stays up until a frame confirms it. `confirmFn`
// defaults to "mode is manual and thrust ~0" (what a stop produces).
VA.sendCritical = function (cmd, confirmFn) {
  VA.logLine("»! " + JSON.stringify(cmd));
  const seq = ++_sendSeq;
  const msg = Object.assign({}, cmd, { seq });
  const id = ++_criticalSeq;
  _critical = { id, confirm: confirmFn || _stopConfirmed, seq };
  // Fire over the WS AND POST simultaneously — both best-effort so a dead socket
  // can't swallow the STOP.
  let sentWs = false;
  try {
    if (ws && ws.readyState === 1) { ws.send(JSON.stringify(msg)); sentWs = true; }
  } catch (e) { VA.logLine("critical WS send failed: " + e); }
  // Prefer the ACK as positive confirmation: clear the red banner the instant
  // the server acks, without waiting for a telemetry frame. The telemetry-frame
  // check (mode manual & thrust~0) in dispatch() remains as the fallback, so
  // STOP is confirmed by whichever arrives first.
  if (sentWs) {
    _registerPending(seq, (ok) => {
      if (ok && _critical && _critical.id === id) {
        _critical = null;
        _banner(STOP_BANNER_ID, { show: false });
      }
    });
  }
  fetch("/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cmd),
  }).catch((e) => VA.logLine("critical command POST failed: " + e));
  setTimeout(() => {
    // Only alarms if NEITHER the ack nor a confirming telemetry frame arrived.
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
