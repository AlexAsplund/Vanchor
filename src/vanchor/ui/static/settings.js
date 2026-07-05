/* Vanchor-NG — settings drawer module.
 *
 * The settings drawer and everything inside it: theme toggle (persisted), depth
 * overlay + sonar-cone slider, the HUD preference panel (visibility / per-widget
 * toggles / opacity / fade-text, persisted), the simulator environment controls
 * (current / wind / gust / variability, weather presets), and the console clear.
 */
"use strict";

(function () {
  const { $, send, bindSlider } = VA.ui;

  // ===== command menu (centred modal; nav lives in menu.js) ================
  // We still own open/close: setDrawer toggles the modal + backdrop and, on
  // open, resets the modal to its home tile grid (via menu.js).
  const drawer = $("settings");
  const drawerScrim = $("settings-scrim");
  function setDrawer(on) {
    drawer.classList.toggle("hidden", !on);
    drawerScrim.classList.toggle("hidden", !on);
    if (on && window.VA && VA.menu && VA.menu.showHome) VA.menu.showHome();
  }
  $("settings-open").addEventListener("click", () => setDrawer(true));
  $("settings-close").addEventListener("click", () => setDrawer(false));
  drawerScrim.addEventListener("click", () => setDrawer(false));

  // ===== theme (persisted; default dark for the marine aesthetic) ==========
  // Two themes: "dark" (default) and "daylight" (bright high-contrast for direct
  // sun). Daylight keys its palette on html[data-theme="daylight"] (also set
  // pre-paint by the inline <head> script, so no flash) and additionally carries
  // body.light so charts.js / light-aware code adapt. "light" is treated as a
  // legacy alias for daylight.
  const THEME_KEY = "vanchor-theme";
  const themeSeg = $("theme-seg");
  const themeBox = $("theme-toggle-box"); // hidden; kept for layout.js profiles
  function applyTheme(theme) {
    const daylight = theme === "daylight" || theme === "light";
    if (daylight) document.documentElement.setAttribute("data-theme", "daylight");
    else document.documentElement.removeAttribute("data-theme");
    document.body.classList.toggle("theme-daylight", daylight);
    document.body.classList.toggle("light", daylight);
    if (themeBox) themeBox.checked = !daylight;
    if (themeSeg) themeSeg.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("on", (b.dataset.theme === "daylight") === daylight);
    });
  }
  function setTheme(theme) {
    const t = (theme === "daylight" || theme === "light") ? "daylight" : "dark";
    applyTheme(t);
    try { localStorage.setItem(THEME_KEY, t); } catch (e) { /* ignore */ }
  }
  (function initTheme() {
    let saved;
    try { saved = localStorage.getItem(THEME_KEY); } catch (e) { saved = null; }
    applyTheme(saved || "dark");
  })();
  if (themeSeg) themeSeg.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => setTheme(b.dataset.theme))
  );
  // The hidden checkbox is driven by layout.js when restoring a profile.
  if (themeBox) themeBox.addEventListener("change", () =>
    setTheme(themeBox.checked ? "dark" : "daylight")
  );

  // ===== depth overlay toggle ==============================================
  const depthShowBox = $("depth-show");
  if (depthShowBox) depthShowBox.addEventListener("change", () => VA.map.setDepthShow(depthShowBox.checked));

  // ===== map boat source (sim vs real GPS) =================================
  const boatSourceSel = $("map-boat-source");
  if (boatSourceSel) {
    try { boatSourceSel.value = localStorage.getItem("mapBoatSource") || "auto"; } catch (e) { /* ignore */ }
    boatSourceSel.addEventListener("change", () => {
      const v = boatSourceSel.value || "auto";
      try { localStorage.setItem("mapBoatSource", v); } catch (e) { /* ignore */ }
      window.dispatchEvent(new CustomEvent("va:boatsource", { detail: v }));
    });
  }

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

  // ===== HUD prefs (persisted) =============================================
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

  // ===== simulator controls (shown only when sim_enabled) ==================
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

  // ===== console clear =====================================================
  $("log-clear").addEventListener("click", () => VA.clearLog());
})();
