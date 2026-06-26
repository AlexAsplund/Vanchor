/* Vanchor-NG — app wiring module.
 *
 * Ties the UI controls to the command vocabulary (VA.send): mode rail +
 * contextual panels, manual sliders, anchor/jog, heading hold, drift, route
 * editor + go-to, cruise / track tools, the settings drawer (theme, HUD prefs,
 * depth overlay, charts, auto-tuner, simulator), and the remote helm.
 *
 * Map / HUD / steering rendering lives in their own modules; this module owns
 * interaction + the bits of telemetry reflection that drive control state.
 */
"use strict";

const send = VA.send;
const $ = (id) => document.getElementById(id);

// ===== mode rail + contextual panels =======================================
let currentMode = null;

function applyModePanels(mode) {
  if (mode === currentMode) return;
  currentMode = mode;
  document.querySelectorAll(".ctx-panel").forEach((p) =>
    p.classList.toggle("active", p.dataset.for === mode));
  // a new mode supersedes any pending go-to destination
  VA.map.clearGotoMarker();
}

// stop has no panel of its own; show the manual panel when stopped.
const panelFor = (m) => (m === "stop" ? "manual" : m);

function highlightRail() {
  document.querySelectorAll(".mode-btn[data-mode]").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === currentMode));
}

document.querySelectorAll(".mode-btn[data-mode]").forEach((b) =>
  b.addEventListener("click", () => {
    const m = b.dataset.mode;
    if (m === "manual") send({ type: "manual", thrust: parseFloat(thrust.value), steering: parseFloat(steer.value) });
    else if (m === "anchor_hold") applyAnchor(true);
    else if (m === "heading_hold") send({ type: "heading_hold", throttle: 0.4 });
    else if (m === "waypoint") startRoute();
    else if (m === "follow_apb") send({ type: "follow_apb" });
    else if (m === "drift") send({ type: "drift", knots: parseFloat(driftKnots.value) });
    else if (m === "stop") send({ type: "stop" });
    // Show the selected mode's panel immediately (user intent). This is the key
    // fix for setup-style modes like Route/Waypoint: the boat doesn't enter
    // "waypoint" until a route exists, so the panel must follow the click — not
    // the backend mode — or you could never reach the route-building controls.
    applyModePanels(panelFor(m));
    highlightRail();
  }));

// Telemetry reflects the live mode: only switch the panel when the BACKEND mode
// genuinely changes (e.g. arrival -> manual), so it never fights a user who is
// mid-setup in a panel. The rail highlight tracks the shown panel.
let lastTelemetryMode = null;
VA.onTelemetry(function renderModes(t) {
  if (t.mode !== lastTelemetryMode) {
    lastTelemetryMode = t.mode;
    applyModePanels(panelFor(t.mode));
  }
  highlightRail();
  updateCruise(t.cruise);
  updateTrack(t.track);
  updateDrift(t);
});

// ===== slider helper =======================================================
function bindSlider(id, outId, fn) {
  const el = $(id), out = $(outId);
  if (!el) return;
  const update = () => { if (out) out.textContent = el.value; if (fn) fn(parseFloat(el.value)); };
  el.addEventListener("input", update);
  update();
}

// ===== manual ==============================================================
const thrust = $("thrust"), steer = $("steer");
const manual = () => send({ type: "manual", thrust: parseFloat(thrust.value), steering: parseFloat(steer.value) });
bindSlider("thrust", "thrust-val", manual);
bindSlider("steer", "steer-val", manual);
// Snap to dead-center 0 when released near zero (avoids tiny residual nudges).
[thrust, steer].forEach((el) => {
  if (!el) return;
  el.addEventListener("change", () => {
    if (Math.abs(parseFloat(el.value)) < 0.12) { el.value = "0"; el.dispatchEvent(new Event("input")); }
  });
});

// ===== heading hold ========================================================
bindSlider("hdg", "hdg-val");
$("hdg-go").addEventListener("click", () =>
  send({ type: "heading_hold", heading: parseFloat($("hdg").value), throttle: 0.4 }));

// ===== follow APB ==========================================================
const apbGo = $("apb-go");
if (apbGo) apbGo.addEventListener("click", () => send({ type: "follow_apb" }));

// ===== drift ===============================================================
const driftKnots = $("drift-knots");
const driftHdg = $("drift-hdg");
let driftKnotsActive = false, driftHdgActive = false;
bindSlider("drift-knots", "drift-knots-val", () => {
  if (currentMode === "drift") send({ type: "drift", knots: parseFloat(driftKnots.value) });
});
bindSlider("drift-hdg", "drift-hdg-val");
[["drift-knots", (v) => driftKnotsActive = v], ["drift-hdg", (v) => driftHdgActive = v]]
  .forEach(([id, set]) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("pointerdown", () => set(true));
    el.addEventListener("pointerup", () => set(false));
    el.addEventListener("pointercancel", () => set(false));
    el.addEventListener("blur", () => set(false));
  });
$("drift-go").addEventListener("click", () =>
  send({ type: "drift", heading: parseFloat(driftHdg.value), knots: parseFloat(driftKnots.value) }));

function updateDrift(t) {
  if (t.mode !== "drift") return;
  const kn = VA.fin(t.drift_target_knots);
  if (kn !== null && !driftKnotsActive) { driftKnots.value = kn; $("drift-knots-val").textContent = driftKnots.value; }
  const hdg = VA.fin(t.target_heading);
  if (hdg !== null && !driftHdgActive) { driftHdg.value = Math.round(hdg); $("drift-hdg-val").textContent = driftHdg.value; }
}

// ===== anchor + jog ========================================================
const arSlider = $("ar");
const holdHdgBox = $("hold-hdg");
function applyAnchor(redrop) {
  const cmd = { type: "anchor_hold", radius_m: parseFloat(arSlider.value), hold_heading: holdHdgBox.checked };
  const last = VA.map.getLastAnchor();
  if (!redrop && last) cmd.anchor = { lat: last.lat, lon: last.lon };
  send(cmd);
}
bindSlider("ar", "ar-val");
arSlider.addEventListener("change", () => { if (VA.map.getLastAnchor()) applyAnchor(false); });
holdHdgBox.addEventListener("change", () => { if (VA.map.getLastAnchor()) applyAnchor(false); });
$("anchor-go").addEventListener("click", () => applyAnchor(true));
[["jog-fwd", "forward"], ["jog-back", "back"], ["jog-left", "left"], ["jog-right", "right"]]
  .forEach(([id, direction]) => {
    const el = $(id);
    if (el) el.addEventListener("click", () => send({ type: "jog", direction }));
  });

// ===== cruise ==============================================================
const cruiseOn = $("cruise-on");
const cruiseKn = $("cruise-kn");
function sendCruise() { send({ type: "cruise", knots: cruiseOn.checked ? parseFloat(cruiseKn.value) : 0 }); }
bindSlider("cruise-kn", "cruise-val", () => { if (cruiseOn.checked) sendCruise(); });
cruiseOn.addEventListener("change", sendCruise);
function updateCruise(cruise) {
  const enabled = !!(cruise && cruise.enabled);
  const target = cruise && Number.isFinite(cruise.target_knots) ? cruise.target_knots : null;
  VA.setText("r-cruise", enabled ? VA.fmt(target, 1) + " kn" : "off");
  const badge = $("cruise-state");
  if (badge) badge.textContent = enabled ? "● " + VA.fmt(target, 1) + " kn" : "";
}

// ===== track (record / replay / backtrack) =================================
$("track-rec").addEventListener("click", () => {
  const recording = $("track-rec").classList.contains("recording");
  send({ type: "record", action: recording ? "stop" : "start" });
});
$("track-replay").addEventListener("click", () => send({ type: "replay" }));
$("track-back").addEventListener("click", () => send({ type: "backtrack" }));
$("track-clear").addEventListener("click", () => send({ type: "record", action: "clear" }));
function updateTrack(track) {
  const rec = !!(track && track.recording);
  const count = track && Number.isFinite(track.count) ? track.count : 0;
  const btn = $("track-rec");
  if (btn) { btn.classList.toggle("recording", rec); btn.textContent = rec ? `● Recording (${count})` : "● Record"; }
  const badge = $("track-state");
  if (badge) badge.textContent = rec ? "● rec " + count : (count ? count + " pts" : "");
}

// ===== route editor + go-to ===============================================
// Waypoints are only dropped while "Add waypoints" mode is armed, so an ordinary
// map tap (panning, deselecting) doesn't litter the route with pins.
let wpArmed = false;
function setWpArmed(on) {
  wpArmed = on;
  const b = $("wp-arm");
  if (b) { b.classList.toggle("active", on); b.textContent = on ? "✓ Tap map to add — done" : "＋ Add waypoints"; }
  if (on) setGotoArmed(false);  // mutually exclusive with Go-to
}
VA.map.setOnMapClick((lat, lon, armed) => {
  if (armed) { gotoTo(lat, lon); setGotoArmed(false); return; }
  if (!wpArmed) return;          // not in add-waypoint mode -> ignore the tap
  VA.map.addPending(lat, lon);
  renderWpList();
});
const wpArmBtn = $("wp-arm");
if (wpArmBtn) wpArmBtn.addEventListener("click", () => setWpArmed(!wpArmed));

function renderWpList() {
  const list = $("wp-list");
  if (!list) return;
  list.innerHTML = "";
  const pending = VA.map.pending();
  if (!pending.length) {
    const li = document.createElement("li");
    li.className = "wp-empty"; li.textContent = "No pending waypoints.";
    list.appendChild(li);
    VA.map.redrawWaypoints();
    return;
  }
  pending.forEach((w, i) => {
    const li = document.createElement("li");
    const ix = document.createElement("span");
    ix.className = "ix"; ix.textContent = (i + 1) + ".";
    const name = document.createElement("input");
    name.className = "wp-name"; name.type = "text"; name.value = w.name;
    name.setAttribute("aria-label", "waypoint name");
    name.addEventListener("input", () => { w.name = name.value; });
    const del = document.createElement("button");
    del.className = "del"; del.textContent = "✕";
    del.setAttribute("aria-label", "delete waypoint");
    del.addEventListener("click", () => { pending.splice(i, 1); renderWpList(); });
    li.append(ix, name, del);
    list.appendChild(li);
  });
  VA.map.redrawWaypoints();
}

// Loop-route flag: when an "island loop" route is loaded into the editor, the
// start command must carry `loop:true` so the boat circles continuously. The
// island module (island.js) sets this via VA.routeEditor.setLoop(true). It is
// cleared whenever a *normal* route is loaded/started or the route is cleared.
let routeIsLoop = false;
function setLoopFlag(on) {
  routeIsLoop = !!on;
  updateLoopIndicator();
}
function updateLoopIndicator(active) {
  // Show while a loop route is pending (loaded, unstarted) or active (boat in
  // waypoint mode running a loop route the user just started).
  const show = routeIsLoop || !!active;
  const el = $("loop-indicator");
  if (el) el.classList.toggle("hidden", !show);
  // Keep the island card's summary badge in sync (it's set when a loop loads).
  const badge = $("island-loop-badge");
  if (badge) badge.classList.toggle("hidden", !show);
}

function startRoute() {
  const pending = VA.map.pending();
  if (!pending.length) { VA.logLine("start route: no waypoints"); return; }
  const cmd = { type: "goto", waypoints: pending.map((w) => ({ name: w.name, lat: w.lat, lon: w.lon })), throttle: 0.6 };
  if (routeIsLoop) cmd.loop = true;       // circle continuously around the island
  send(cmd);
  // The route is now ACTIVE — its committed waypoints come back via telemetry
  // and render as the active (coloured) route. Clear the editable "not started"
  // pins so they don't sit on top of and hide the active ones.
  VA.map.setPending([]);
  renderWpList();
  setWpArmed(false);
  // Keep routeIsLoop set so the live route-edit re-send (below) preserves the
  // loop, and the indicator stays lit while the loop route runs.
}

// Let other modules (smart routing, saved routes, live route editing) refresh
// the editor list after they inject pending waypoints via VA.map.setPending(...),
// or re-send the active route after an edit. setLoop()/clearLoop() let the
// island module mark the pending route as a continuous loop.
VA.routeEditor = {
  refresh: renderWpList,
  startRoute,
  setLoop: setLoopFlag,
  clearLoop: () => setLoopFlag(false),
  isLoop: () => routeIsLoop,
};

// ---- top-bar route progress: traveled ▸ remaining (#69) ----
function fmtDist(m) {
  if (!Number.isFinite(m) || m < 0) return "—";
  return m < 1000 ? Math.round(m) + " m" : (m / 1000).toFixed(m < 10000 ? 1 : 0) + " km";
}
function haversineM(aLat, aLon, bLat, bLon) {
  const R = 6371000, rad = Math.PI / 180;
  const dLat = (bLat - aLat) * rad, dLon = (bLon - aLon) * rad;
  const h = Math.sin(dLat / 2) ** 2 +
    Math.cos(aLat * rad) * Math.cos(bLat * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
}
// mm:ss (or h:mm:ss) clock formatting for the route chip ETA (#100).
function fmtClock(s) {
  if (!Number.isFinite(s) || s < 0) return "—";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(ss)}` : `${m}:${pad(ss)}`;
}
// Route ETA state (#100): start time of the active route + a rolling SOG window.
let routeStartMs = null;          // when the route first became active
const SOG_WINDOW_MS = 45000;      // rolling-average window
const SOG_MIN_MS = 0.05;          // ignore near-zero samples (~0.1 kn)
const sogSamples = [];            // [{ ms, mps }]
function rollingSpeedMps(nowMs) {
  // Drop samples older than the window, then average the (non-near-zero) ones.
  while (sogSamples.length && nowMs - sogSamples[0].ms > SOG_WINDOW_MS) sogSamples.shift();
  let sum = 0, n = 0;
  for (const s of sogSamples) { if (s.mps > SOG_MIN_MS) { sum += s.mps; n++; } }
  return n ? sum / n : null;
}
VA.onTelemetry((t) => {
  const chip = $("chip-route");
  if (!chip) return;
  const nowMs = Date.now();
  // Always feed the rolling speed window from SOG (knots → m/s) so it's warm.
  const sogKn = VA.fin ? VA.fin(t.sog_knots) : (Number.isFinite(t.sog_knots) ? t.sog_knots : null);
  if (sogKn !== null) sogSamples.push({ ms: nowMs, mps: sogKn * 0.514444 });

  const wps = Array.isArray(t.waypoints) ? t.waypoints : [];
  const pos = t.position || t.truth;
  if (t.mode !== "waypoint" || wps.length < 1 || !pos) {
    chip.classList.add("hidden");
    routeStartMs = null;   // route no longer active — reset elapsed clock
    return;
  }
  const active = Math.max(0, Math.min(wps.length - 1, Number.isInteger(t.active_waypoint) ? t.active_waypoint : 0));
  let traveled = 0, remainingLegs = 0;
  for (let i = 0; i < wps.length - 1; i++) {
    const d = haversineM(wps[i].lat, wps[i].lon, wps[i + 1].lat, wps[i + 1].lon);
    if (i < active) traveled += d; else remainingLegs += d;
  }
  const remaining = haversineM(pos.lat, pos.lon, wps[active].lat, wps[active].lon) + remainingLegs;
  VA.setText("chip-route-trav", fmtDist(traveled));
  VA.setText("chip-route-left", fmtDist(remaining));

  // ---- time taken / time left (#100) ----
  // Mark the route active the first frame remaining distance is finite.
  if (routeStartMs === null && Number.isFinite(remaining)) routeStartMs = nowMs;
  const takenS = routeStartMs !== null ? (nowMs - routeStartMs) / 1000 : null;
  const avgMps = rollingSpeedMps(nowMs);
  const leftS = (avgMps && Number.isFinite(remaining)) ? remaining / avgMps : null;
  VA.setText("chip-route-taken", takenS === null ? "—" : fmtClock(takenS));
  VA.setText("chip-route-tleft", leftS === null ? "—" : fmtClock(leftS));

  chip.classList.remove("hidden");
});

// Keep the loop indicator lit while an active loop route is running on the boat
// (telemetry reflects it). Falls back to the pending flag when not in waypoint.
VA.onTelemetry((t) => {
  const activeLoop = t && t.mode === "waypoint" && t.loop === true;
  updateLoopIndicator(activeLoop);
});

$("wp-go").addEventListener("click", startRoute);
$("wp-clear").addEventListener("click", () => {
  VA.map.setPending([]); renderWpList(); setWpArmed(false);
  setLoopFlag(false);   // clearing the route drops any island-loop flag
});

// Live editing of an ACTIVE route: when the user drags or edits a committed
// waypoint on the map, re-send the whole route so navigation adjusts live (#51).
if (VA.map.onRouteEdit) VA.map.onRouteEdit((waypoints) => {
  if (!waypoints || !waypoints.length) { send({ type: "stop" }); setLoopFlag(false); return; }
  const cmd = { type: "goto", waypoints, throttle: 0.6 };
  if (routeIsLoop) cmd.loop = true;   // preserve continuous loop across live edits
  send(cmd);
});

// go-to (tap map)
const gotoArm = $("goto-arm");
const gotoAction = $("goto-action");
function setGotoArmed(on) {
  VA.map.setGotoArmed(on);
  if (gotoArm) {
    gotoArm.classList.toggle("active", on);
    gotoArm.textContent = on ? "Tap the map… (cancel)" : "Tap map to go";
  }
}
function gotoTo(lat, lon) {
  const on_arrival = gotoAction ? gotoAction.value : "anchor";
  VA.map.setGotoMarker(lat, lon);
  send({ type: "goto", waypoints: [{ name: "GOTO", lat, lon }], throttle: 0.6, on_arrival });
}
if (gotoArm) gotoArm.addEventListener("click", () => setGotoArmed(!VA.map.isGotoArmed()));

// ===== settings drawer =====================================================
const drawer = $("settings");
const drawerScrim = $("settings-scrim");
function setDrawer(on) {
  drawer.classList.toggle("hidden", !on);
  drawerScrim.classList.toggle("hidden", !on);
}
$("settings-open").addEventListener("click", () => setDrawer(true));
$("settings-close").addEventListener("click", () => setDrawer(false));
drawerScrim.addEventListener("click", () => setDrawer(false));

// ===== theme (persisted; default dark for the marine aesthetic) ===========
const THEME_KEY = "vanchor-theme";
function applyTheme(theme) {
  const dark = theme !== "light";
  document.body.classList.toggle("light", !dark);
  const box = $("theme-toggle-box");
  if (box) box.checked = dark;
  // (charts repaint on next open; avoid touching chart state declared later)
}
(function initTheme() {
  let saved;
  try { saved = localStorage.getItem(THEME_KEY); } catch (e) { saved = null; }
  applyTheme(saved || "dark");
})();
$("theme-toggle-box").addEventListener("change", () => {
  const dark = $("theme-toggle-box").checked;
  applyTheme(dark ? "dark" : "light");
  try { localStorage.setItem(THEME_KEY, dark ? "dark" : "light"); } catch (e) { /* ignore */ }
});

// ===== depth overlay toggle ================================================
const depthShowBox = $("depth-show");
if (depthShowBox) depthShowBox.addEventListener("change", () => VA.map.setDepthShow(depthShowBox.checked));

// Sonar cone angle (#47): drives the depth-dot footprint sizing. The slider
// writes an override into VA.sonarCone (localStorage-backed); telemetry supplies
// the default when no override is set. We reflect the effective angle on first
// telemetry so the slider shows the boat default until the user overrides it.
const coneInput = $("sonar-cone");
const coneOut = $("sonar-cone-val");
if (coneInput && VA.sonarCone) {
  let coneTouched = VA.sonarCone.override !== null;
  const syncOut = () => { if (coneOut) coneOut.textContent = coneInput.value; };
  if (coneTouched) { coneInput.value = VA.sonarCone.override; }
  syncOut();
  coneInput.addEventListener("input", syncOut);
  coneInput.addEventListener("change", () => {
    coneTouched = true;
    VA.sonarCone.set(parseFloat(coneInput.value));
  });
  // Until the user touches it, follow the boat's telemetry default.
  VA.onTelemetry(function (t) {
    if (coneTouched) return;
    const def = t && t.boat && Number(t.boat.sonar_cone_deg);
    if (Number.isFinite(def) && def > 0 && Math.abs(parseFloat(coneInput.value) - def) > 0.01) {
      coneInput.value = def; syncOut();
    }
  });
}

// ===== HUD prefs (persisted) ===============================================
const HUD_KEY = "vanchor-hud";
const HUD_WIDGETS = ["speed", "heading", "depth", "anchor", "battery", "steering", "catch"];
let hudPrefs = { show: true, opacity: 1, fadeText: false };
HUD_WIDGETS.forEach((w) => { hudPrefs[w] = true; });
const clampOpacity = (v) => Math.min(1, Math.max(0.3, v));
function loadHudPrefs() {
  try {
    const raw = localStorage.getItem(HUD_KEY);
    if (raw) {
      const o = JSON.parse(raw);
      if (o && typeof o === "object") {
        if (typeof o.show === "boolean") hudPrefs.show = o.show;
        if (typeof o.opacity === "number") hudPrefs.opacity = clampOpacity(o.opacity);
        if (typeof o.fadeText === "boolean") hudPrefs.fadeText = o.fadeText;
        HUD_WIDGETS.forEach((w) => { if (typeof o[w] === "boolean") hudPrefs[w] = o[w]; });
      }
    }
  } catch (e) { /* ignore */ }
}
function saveHudPrefs() { try { localStorage.setItem(HUD_KEY, JSON.stringify(hudPrefs)); } catch (e) { /* ignore */ } }
function applyHudPrefs() {
  const hud = $("hud");
  if (hud) hud.classList.toggle("hidden", !hudPrefs.show);
  // speed/heading/depth/anchor are widgets inside #hud; steering is its own panel
  ["speed", "heading", "depth", "anchor", "battery"].forEach((w) => {
    const el = document.querySelector(`.hud-widget[data-hud="${w}"]`);
    if (el) el.classList.toggle("hidden", !(hudPrefs.show && hudPrefs[w]));
  });
  // catch is a standalone floating launcher (not inside #hud) — its visibility
  // follows its own pref, independent of whether the numeric HUD row is shown.
  const catchEl = document.querySelector(`[data-hud="catch"]`);
  if (catchEl) catchEl.classList.toggle("hidden", !hudPrefs.catch);
  const sg = $("steering-gauge");
  if (sg) sg.classList.toggle("hidden", !hudPrefs.steering);
  const showBox = $("hud-show");
  if (showBox) showBox.checked = hudPrefs.show;
  document.querySelectorAll(".hud-toggle").forEach((cb) => {
    const w = cb.dataset.widget;
    if (w in hudPrefs) cb.checked = hudPrefs[w];
  });
  // HUD tile opacity (applies to #hud, the steering gauge and the catch FAB
  // via the --hud-opacity CSS var).
  document.documentElement.style.setProperty("--hud-opacity", String(hudPrefs.opacity));
  document.body.classList.toggle("hud-fade-text", !!hudPrefs.fadeText);
  const opS = $("hud-opacity"), opV = $("hud-opacity-val");
  const pct = Math.round(hudPrefs.opacity * 100);
  if (opS) opS.value = pct;
  if (opV) opV.textContent = pct;
  const ftEl = $("hud-fade-text");
  if (ftEl) ftEl.checked = !!hudPrefs.fadeText;
}
loadHudPrefs();
applyHudPrefs();
const hudShowBox = $("hud-show");
if (hudShowBox) hudShowBox.addEventListener("change", () => { hudPrefs.show = hudShowBox.checked; saveHudPrefs(); applyHudPrefs(); });
document.querySelectorAll(".hud-toggle").forEach((cb) => cb.addEventListener("change", () => {
  hudPrefs[cb.dataset.widget] = cb.checked; saveHudPrefs(); applyHudPrefs();
}));
const hudOpacityEl = $("hud-opacity");
if (hudOpacityEl) hudOpacityEl.addEventListener("input", () => {
  hudPrefs.opacity = clampOpacity((parseInt(hudOpacityEl.value, 10) || 100) / 100);
  saveHudPrefs(); applyHudPrefs();
});
const hudFadeTextEl = $("hud-fade-text");
if (hudFadeTextEl) hudFadeTextEl.addEventListener("change", () => {
  hudPrefs.fadeText = hudFadeTextEl.checked; saveHudPrefs(); applyHudPrefs();
});

// ===== simulator controls (shown only when sim_enabled) ====================
function env() {
  send({
    type: "set_environment",
    current_speed: parseFloat(cs.value), current_dir: parseFloat(cd.value),
    wind_speed: parseFloat(windSpeed.value), wind_dir: parseFloat(wd.value),
    gust_amplitude_mps: gust ? parseFloat(gust.value) : 0,
  });
}
const cs = $("cs"), cd = $("cd"), windSpeed = $("ws"), wd = $("wd"), gust = $("ga");
["cs:cs-val", "cd:cd-val", "ws:ws-val", "wd:wd-val", "ga:ga-val"].forEach((p) => {
  const [id, out] = p.split(":"); if ($(id)) bindSlider(id, out, env);
});

// ---- weather presets (task #44) ----
// Populated from GET /api/weather/presets; selecting one fires weather_preset.
const presetSel = $("weather-preset");
async function loadWeatherPresets() {
  if (!presetSel) return;
  let d;
  try { d = await VA.getJSON("/api/weather/presets"); } catch (e) { d = null; }
  const presets = d && Array.isArray(d.presets) ? d.presets : null;
  const row = $("weather-preset-row");
  if (!presets || !presets.length) { if (row) row.classList.add("hidden"); return; }
  if (row) row.classList.remove("hidden");
  presetSel.innerHTML = '<option value="">— choose —</option>';
  presets.forEach((p) => {
    if (!p || p.id === undefined) return;
    const o = document.createElement("option");
    o.value = String(p.id); o.textContent = p.label || String(p.id);
    presetSel.appendChild(o);
  });
}
if (presetSel) presetSel.addEventListener("change", () => {
  if (presetSel.value !== "") VA.send({ type: "weather_preset", id: presetSel.value });
});

// ---- wind variability slider (sends set_environment {wind_variability}) ----
const windVar = $("wv");
let windVarActive = false;
bindSlider("wv", "wv-val", (v) => {
  if (windVarActive) send({ type: "set_environment", wind_variability: v });
});
if (windVar) {
  windVar.addEventListener("pointerdown", () => { windVarActive = true; });
  ["pointerup", "pointercancel", "blur"].forEach((ev) => windVar.addEventListener(ev, () => { windVarActive = false; }));
  windVar.addEventListener("change", () => send({ type: "set_environment", wind_variability: parseFloat(windVar.value) }));
}

let simShown = false;
let presetsLoaded = false;
VA.onTelemetry(function renderSim(t) {
  const on = !!t.sim_enabled;
  if (on !== simShown) {
    simShown = on;
    const card = $("sim-card");
    if (card) card.classList.toggle("hidden", !on);
  }
  if (on && !presetsLoaded) { presetsLoaded = true; loadWeatherPresets(); }
  const e = t.environment;
  if (on && e) {
    const gn = Number.isFinite(e.wind_gust_now) ? e.wind_gust_now.toFixed(1) : "—";
    VA.setText("env-now", `wind now: ${gn} m/s`);
    // reflect wind_variability from telemetry (when not actively dragging)
    if (windVar && Number.isFinite(e.wind_variability) && !windVarActive) {
      windVar.value = e.wind_variability;
      VA.setText("wv-val", windVar.value);
    }
  }
});

// ===== console clear =======================================================
$("log-clear").addEventListener("click", () => VA.clearLog());

// ===== charts (uPlot) ======================================================
const CHART_CAP = 600;
const chartT = [];
const series = { heading: [], target: [], thrust: [], steering: [], sog: [], dist: [], xte: [] };
let chartT0 = null;
let charts = [];
let chartsBuilt = false;
function defNum(v) { return Number.isFinite(v) ? v : null; }

VA.onTelemetry(function pushChartSample(t) {
  const card = $("charts-card");
  if (!card || !card.open) return;
  const now = performance.now() / 1000;
  if (chartT0 === null) chartT0 = now;
  chartT.push(now - chartT0);
  series.heading.push(defNum(t.heading_deg));
  series.target.push(defNum(t.target_heading));
  const motor = t.motor || {};
  series.thrust.push(defNum(motor.thrust));
  series.steering.push(defNum(motor.steering));
  series.sog.push(defNum(t.sog_knots));
  series.dist.push(defNum(t.distance_to_anchor_m));
  series.xte.push(defNum(t.cross_track_m));
  if (chartT.length > CHART_CAP) { chartT.shift(); for (const k in series) series[k].shift(); }
  redrawCharts();
});

function cssVar(name, fallback) {
  const v = getComputedStyle(document.body).getPropertyValue(name).trim();
  return v || fallback;
}
function chartTheme() {
  const dark = !document.body.classList.contains("light");
  return {
    accent: cssVar("--accent", "#1be4ff"), active: cssVar("--active", "#22d3a6"),
    text: cssVar("--text", "#e7eaee"), muted: cssVar("--muted", "#9aa3ad"),
    grid: dark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.10)",
    warn: "#ffb454", stop: cssVar("--stop", "#ff5a7a"),
  };
}
function buildCharts() {
  const host = $("charts");
  if (!host || typeof uPlot === "undefined") return;
  host.innerHTML = "";
  charts.forEach((c) => c.destroy());
  charts = [];
  const th = chartTheme();
  const W = 300, H = 96;
  const axis = (label) => ({
    stroke: th.muted, grid: { stroke: th.grid, width: 1 }, ticks: { stroke: th.grid, width: 1 },
    font: "10px Inter, system-ui, sans-serif", size: 30, label,
    labelFont: "10px Inter, system-ui, sans-serif", labelSize: 14,
  });
  const xAxis = {
    stroke: th.muted, grid: { stroke: th.grid, width: 1 }, ticks: { stroke: th.grid, width: 1 },
    font: "10px Inter, system-ui, sans-serif", size: 22,
  };
  const base = (title, seriesDefs, dataKeys) => ({
    opts: {
      title, width: W, height: H, cursor: { show: true }, legend: { show: false },
      padding: [6, 6, 2, 0], axes: [xAxis, axis()], series: [{}].concat(seriesDefs),
    },
    keys: dataKeys,
  });
  const defs = [
    base("Heading / target (°)", [{ stroke: th.accent, width: 1.4 }, { stroke: th.warn, width: 1.4, dash: [4, 3] }], ["heading", "target"]),
    base("Thrust / steering", [{ stroke: th.active, width: 1.4 }, { stroke: th.accent, width: 1.4 }], ["thrust", "steering"]),
    base("SOG (kn)", [{ stroke: th.active, width: 1.4 }], ["sog"]),
    base("Dist→anchor / XTE (m)", [{ stroke: th.accent, width: 1.4 }, { stroke: th.stop, width: 1.4 }], ["dist", "xte"]),
  ];
  charts = defs.map((d) => { const u = new uPlot(d.opts, chartData(d.keys), host); u._keys = d.keys; return u; });
  chartsBuilt = true;
}
function chartData(keys) { return [chartT].concat(keys.map((k) => series[k])); }
function redrawCharts() {
  const card = $("charts-card");
  if (!card || !card.open) return;
  if (!chartsBuilt) buildCharts();
  charts.forEach((u) => u.setData(chartData(u._keys)));
}
const chartsCard = $("charts-card");
if (chartsCard) chartsCard.addEventListener("toggle", () => { if (chartsCard.open && !chartsBuilt) buildCharts(); });
$("charts-clear").addEventListener("click", () => {
  chartT.length = 0; for (const k in series) series[k].length = 0; chartT0 = null; redrawCharts();
});

// ===== auto-tune (PID) =====================================================
const tuneJobBtns = document.querySelectorAll("#tune-jobs button[data-job]");
const tuneStatus = $("tune-status");
const tuneResult = $("tune-result");
const tuneApply = $("tune-apply");
let tuneBusy = false, tuneLastJob = null;
function tuneNum(v) {
  if (v === null || v === undefined || (typeof v === "number" && !Number.isFinite(v))) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  if (n === 0) return "0";
  const a = Math.abs(n);
  let s = (a < 0.001 || a >= 100000) ? n.toExponential(2) : n.toPrecision(4);
  if (s.indexOf("e") === -1 && s.indexOf(".") !== -1) s = s.replace(/\.?0+$/, "");
  return s;
}
function setTuneBusy(busy) {
  tuneBusy = busy;
  tuneJobBtns.forEach((b) => { b.disabled = busy; });
  if (tuneApply) tuneApply.disabled = busy || !tuneLastJob;
}
function tuneGrid(parent, baseline, tuned) {
  const grid = document.createElement("div");
  grid.className = "tune-grid";
  const keys = Object.keys(tuned || {});
  if (!keys.length) { parent.appendChild(document.createTextNode("—")); return; }
  keys.forEach((k) => {
    const kEl = document.createElement("span"); kEl.className = "k"; kEl.textContent = k;
    const bEl = document.createElement("span"); bEl.className = "base"; bEl.textContent = baseline ? tuneNum(baseline[k]) : "—";
    const tEl = document.createElement("span"); tEl.className = "tuned"; tEl.textContent = "→ " + tuneNum(tuned[k]);
    grid.append(kEl, bEl, tEl);
  });
  parent.appendChild(grid);
}
function tuneInfoGrid(parent, baseInfo, tunedInfo) {
  const keys = Object.keys(tunedInfo || baseInfo || {});
  if (!keys.length) return;
  const grid = document.createElement("div");
  grid.className = "tune-grid";
  keys.forEach((k) => {
    const kEl = document.createElement("span"); kEl.className = "k"; kEl.textContent = k;
    const bEl = document.createElement("span"); bEl.className = "base"; bEl.textContent = baseInfo ? tuneNum(baseInfo[k]) : "—";
    const tEl = document.createElement("span"); tEl.className = "tuned"; tEl.textContent = "→ " + tuneNum(tunedInfo ? tunedInfo[k] : undefined);
    grid.append(kEl, bEl, tEl);
  });
  parent.appendChild(grid);
}
function renderTuneResult(r) {
  tuneResult.innerHTML = "";
  const ph = document.createElement("div");
  ph.className = "tune-section"; ph.textContent = "Parameters (baseline → tuned)";
  tuneResult.appendChild(ph);
  tuneGrid(tuneResult, r.baseline_params, r.tuned_params);
  const bc = Number(r.baseline_cost), tc = Number(r.tuned_cost);
  const cost = document.createElement("div");
  cost.className = "tune-cost";
  cost.append(document.createTextNode("Cost "));
  const bcb = document.createElement("b"); bcb.textContent = tuneNum(bc);
  const tcb = document.createElement("b"); tcb.textContent = tuneNum(tc);
  cost.append(bcb, document.createTextNode(" → "), tcb);
  if (Number.isFinite(bc) && Number.isFinite(tc) && bc !== 0) {
    const imp = (1 - tc / bc) * 100;
    const span = document.createElement("span");
    span.className = "tune-imp " + (imp >= 0 ? "good" : "bad");
    span.textContent = "  (" + (imp >= 0 ? "−" : "+") + Math.abs(imp).toFixed(1) + "% cost)";
    cost.appendChild(span);
  }
  tuneResult.appendChild(cost);
  const evals = document.createElement("div");
  evals.className = "hint";
  evals.textContent = (Number.isFinite(Number(r.evals)) ? r.evals : "?") + " evaluations";
  tuneResult.appendChild(evals);
  if ((r.baseline_info && Object.keys(r.baseline_info).length) || (r.tuned_info && Object.keys(r.tuned_info).length)) {
    const ih = document.createElement("div");
    ih.className = "tune-section"; ih.textContent = "Metrics (baseline → tuned)";
    tuneResult.appendChild(ih);
    tuneInfoGrid(tuneResult, r.baseline_info, r.tuned_info);
  }
}
async function runTune(job, apply) {
  if (tuneBusy) return;
  setTuneBusy(true);
  tuneStatus.className = "hint busy";
  tuneStatus.textContent = (apply ? "applying " : "tuning ") + job + "… (a few seconds)";
  VA.logLine("» tune " + job + (apply ? " (apply)" : ""));
  try {
    const r = await VA.postJSON("/api/tune", { job, max_evals: 50, apply: !!apply });
    if (r && r.error) {
      tuneStatus.className = "hint err"; tuneStatus.textContent = "error: " + r.error;
      VA.logLine("tune error: " + r.error); return;
    }
    if (apply && r && r.applied) {
      tuneStatus.className = "hint ok"; tuneStatus.textContent = "✓ applied to the live controller";
      VA.logLine("tune applied: " + job);
    } else {
      tuneStatus.className = "hint";
      tuneStatus.textContent = "tuned " + job + " in " + (Number.isFinite(Number(r.evals)) ? r.evals : "?") + " evals";
    }
    tuneLastJob = job;
    renderTuneResult(r);
    if (tuneApply) tuneApply.hidden = false;
  } catch (err) {
    tuneStatus.className = "hint err"; tuneStatus.textContent = "request failed: " + err;
    VA.logLine("tune request failed: " + err);
  } finally { setTuneBusy(false); }
}
tuneJobBtns.forEach((b) => b.addEventListener("click", () => runTune(b.dataset.job, false)));
if (tuneApply) tuneApply.addEventListener("click", () => { if (tuneLastJob) runTune(tuneLastJob, true); });

// ===== remote helm =========================================================
let rmThrust = 0, rmSteer = 0;
function clampUnit(v) { return Math.max(-1, Math.min(1, v)); }
function rmUpdateManualState() { VA.setText("rm-manual-state", `thr ${rmThrust.toFixed(1)} · str ${rmSteer.toFixed(1)}`); }
function rmSendManual() { send({ type: "manual", thrust: rmThrust, steering: rmSteer }); rmUpdateManualState(); }
function setRemote(on) {
  const overlay = $("remote");
  if (overlay) overlay.classList.toggle("hidden", !on);
  const btn = $("remote-toggle");
  if (btn) btn.classList.toggle("active", on);
}
const remoteToggle = $("remote-toggle");
if (remoteToggle) remoteToggle.addEventListener("click", () => setRemote(true));
const rmExit = $("rm-exit");
if (rmExit) rmExit.addEventListener("click", () => setRemote(false));
const rmBind = (id, fn) => { const el = $(id); if (el) el.addEventListener("click", fn); };
rmBind("rm-anchor-here", () => send({ type: "anchor_hold", radius_m: 5 }));
rmBind("rm-stop", () => { rmThrust = 0; rmSteer = 0; rmUpdateManualState(); send({ type: "stop" }); });
rmBind("rm-hold-hdg", () => send({ type: "heading_hold", throttle: 0.4 }));
[["rm-jog-fwd", "forward"], ["rm-jog-back", "back"], ["rm-jog-left", "left"], ["rm-jog-right", "right"]]
  .forEach(([id, direction]) => rmBind(id, () => send({ type: "jog", direction })));
rmBind("rm-thr-up", () => { rmThrust = clampUnit(rmThrust + 0.2); rmSendManual(); });
rmBind("rm-thr-dn", () => { rmThrust = clampUnit(rmThrust - 0.2); rmSendManual(); });
rmBind("rm-str-l", () => { rmSteer = clampUnit(rmSteer - 0.2); rmSendManual(); });
rmBind("rm-str-r", () => { rmSteer = clampUnit(rmSteer + 0.2); rmSendManual(); });
rmUpdateManualState();

// ===== init ================================================================
renderWpList();
VA.connect();
