/* Vanchor-NG — safety & power module.
 *
 * Five features, all degrading gracefully when the backend is absent (every
 * telemetry read is guarded; missing values render "—"):
 *
 *  #60 Battery monitor — `battery{soc_pct,voltage_v,current_a,draw_w,range_m,
 *      time_to_empty_s}`. A toggleable HUD widget (data-hud/data-widget
 *      "battery") with a filling/colouring icon, a compact status-bar chip, a
 *      Settings readout, and a test SOC control → `{type:"set_battery",soc_pct}`.
 *
 *  #61 Return-to-Launch — buttons (dock nav bar + remote helm + Settings) →
 *      `{type:"return_to_launch"}`; "Set launch here" → `{type:"set_launch"}`.
 *      `launch{lat,lon,set}` draws a launch marker. `rtl_recommended` shows a
 *      prominent warning banner with a one-tap Return button.
 *
 *  #62 Shallow / no-go zones — draw red hatched polygons (reuses
 *      VA.map.startAreaSelect freehand) → `{type:"set_nogo_zones",zones:[...]}`.
 *      A min-depth input → `{type:"set_min_depth",min_depth_m}`. Zones persist
 *      in localStorage and re-send on load. `safety.shallow_stop` /
 *      `safety.nogo_stop` raise a loud alarm banner.
 *
 *  #63 Man-overboard — a prominent red MOB button (1s hold to fire, so it can't
 *      be hit by accident) → `{type:"mob"}`; also in the remote helm. `mob{
 *      active,lat,lon}` drops a MOB marker + an "MOB — returning" banner with a
 *      Clear (`{type:"mob_clear"}`).
 *
 *  #64 Link-loss failsafe — `link{client_connected,since_s,failsafe_engaged}`.
 *      A status-bar link chip; a "Connection lost — holding position" banner
 *      when failsafe is engaged.
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);

  // Governor-advisory dwell: the banner only appears after the governor has
  // been CONTINUOUSLY blocking for this long, so routine station-keeping
  // reverse-interlock flicker (Smart/Leif, ~2x/sec) never strobes it.
  const GOV_DWELL_MS = 4000;
  let _govSince = 0;
  const send = (c) => VA.send(c);
  const map = window.VA && VA.map && VA.map.leaflet;
  const L = window.L;

  // ======================================================================
  // #60 BATTERY MONITOR
  // ======================================================================
  function battLevel(soc) {
    if (!Number.isFinite(soc)) return "none";
    if (soc < 10) return "crit";
    if (soc < 25) return "low";
    return "ok";
  }
  // Shared helper — mobile.js uses it too (loads after safety.js).
  VA.battLevel = battLevel;

  function fmtDuration(s) {
    if (!Number.isFinite(s) || s < 0) return "—";
    const m = Math.round(s / 60);
    if (m < 60) return m + " min";
    const h = Math.floor(m / 60), mm = m % 60;
    return h + "h " + String(mm).padStart(2, "0") + "m";
  }
  function fmtRange(m) {
    if (!Number.isFinite(m) || m < 0) return "—";
    return m >= 1000 ? (m / 1000).toFixed(1) + " km" : Math.round(m) + " m";
  }

  function renderBattery(t) {
    const b = (t && t.battery) || null;
    const soc = b && Number.isFinite(b.soc_pct) ? b.soc_pct : null;
    const volts = b && Number.isFinite(b.voltage_v) ? b.voltage_v : null;
    const draw = b && Number.isFinite(b.draw_w) ? b.draw_w : null;
    const range = b && Number.isFinite(b.range_m) ? b.range_m : null;
    const tte = b && Number.isFinite(b.time_to_empty_s) ? b.time_to_empty_s : null;
    const level = battLevel(soc);
    const haveBatt = !!b;

    // ---- HUD widget ----
    VA.setText("hud-batt-soc", soc === null ? "—" : Math.round(soc).toString());
    VA.setText("hud-batt-volts", volts === null ? "—" : volts.toFixed(1));
    // prefer range, fall back to time-to-empty
    const rangeTxt = range !== null ? fmtRange(range)
      : (tte !== null ? fmtDuration(tte) + " left" : "—");
    VA.setText("hud-batt-range", rangeTxt);
    const hudIcon = $("hud-batt-icon");
    if (hudIcon) hudIcon.dataset.level = level;
    const hudFill = $("hud-batt-fill");
    if (hudFill) hudFill.style.height = soc === null ? "0%" : Math.max(0, Math.min(100, soc)) + "%";

    // ---- status-bar chip ----
    const chip = $("chip-batt");
    if (chip) chip.classList.toggle("hidden", !haveBatt);
    if (chip) chip.dataset.level = level;
    VA.setText("chip-batt-val", soc === null ? "—" : Math.round(soc).toString());
    const chipFill = $("chip-batt-fill");
    if (chipFill) chipFill.style.width = soc === null ? "0%" : Math.max(0, Math.min(100, soc)) + "%";

    // ---- Settings readout ----
    VA.setText("set-batt-soc", soc === null ? "—" : Math.round(soc) + " %");
    VA.setText("set-batt-volts", volts === null ? "—" : volts.toFixed(1) + " V");
    VA.setText("set-batt-draw", draw === null ? "—" : Math.round(draw) + " W");
    VA.setText("set-batt-range", fmtRange(range));
    VA.setText("set-batt-tte", fmtDuration(tte));
    const badge = $("safety-card-state");
    if (badge) badge.textContent = soc === null ? "" : (level === "ok" ? "" : "⚠ " + Math.round(soc) + "%");

    // ---- Battery warn/alarm strips (D5) + edge-triggered logAlert ----
    // Fixed SOC thresholds (amber <25%, red <10%) INDEPENDENT of the RTL
    // range estimate. Range joins the copy only when the estimate is finite.
    const warnBanner = $("batt-warn-banner");
    const critBanner = $("batt-crit-banner");
    const isLow = level === "low", isCrit = level === "crit";
    const rangeSuffix = range !== null && range > 0 ? " · ~" + fmtRange(range) + " range" : "";
    if (warnBanner) {
      warnBanner.classList.toggle("hidden", !isLow);
      const msg = $("batt-warn-msg");
      if (msg && isLow && soc !== null) msg.textContent = "Battery " + Math.round(soc) + "%" + rangeSuffix;
    }
    if (critBanner) {
      critBanner.classList.toggle("hidden", !isCrit);
      const msg = $("batt-crit-msg");
      if (msg && isCrit && soc !== null) msg.textContent = "BATTERY CRITICAL — " + Math.round(soc) + "%" + rangeSuffix;
    }
    // RTL buttons only when a launch point exists (they're hold-to-engage;
    // wired below with bindHold — RTL drives the boat away).
    const launchSet = !!(t && t.launch && t.launch.set);
    const warnRtl = $("batt-warn-rtl"), critRtl = $("batt-crit-rtl");
    if (warnRtl) warnRtl.classList.toggle("hidden", !launchSet);
    if (critRtl) critRtl.classList.toggle("hidden", !launchSet);
    // Edge-triggered logAlert: only fire on the transition into the bad state.
    const prevLevel = renderBattery._prevLevel;
    if (isCrit && prevLevel !== "crit") {
      if (VA.logAlert) VA.logAlert("alarm", "Battery critical — " + Math.round(soc) + "%", { level: "high" });
    } else if (isLow && prevLevel !== "low" && prevLevel !== "crit") {
      if (VA.logAlert) VA.logAlert("warn", "Battery low — " + Math.round(soc) + "%", { level: "low" });
    }
    renderBattery._prevLevel = level;
  }

  // test SOC control
  const battTest = $("batt-test"), battTestVal = $("batt-test-val");
  if (battTest && battTestVal) {
    battTest.addEventListener("input", () => { battTestVal.textContent = battTest.value; });
  }
  const battTestSend = $("batt-test-send");
  if (battTestSend) battTestSend.addEventListener("click", () => {
    const v = battTest ? parseFloat(battTest.value) : NaN;
    if (Number.isFinite(v)) send({ type: "set_battery", soc_pct: v });
  });

  // ======================================================================
  // #61 RETURN-TO-LAUNCH
  // ======================================================================
  let launchMarker = null;
  function launchIcon() {
    return L.divIcon({ className: "", html: '<div class="launch-pin"><span>⚑</span></div>', iconSize: [26, 26], iconAnchor: [4, 24], popupAnchor: [9, -22] });
  }
  function renderLaunch(t) {
    const l = (t && t.launch) || null;
    const set = !!(l && l.set);
    VA.setText("set-launch-state", set ? "yes" : "no");
    const rtlBtn = $("set-rtl"), navRtl = $("nav-rtl"), rmRtl = $("rm-rtl");
    [rtlBtn, navRtl, rmRtl].forEach((b) => { if (b) b.disabled = !set; });
    if (!map || !L) return;
    if (set && Number.isFinite(l.lat) && Number.isFinite(l.lon)) {
      if (!launchMarker) {
        launchMarker = L.marker([l.lat, l.lon], { icon: launchIcon(), zIndexOffset: 600, interactive: true });
        launchMarker.bindPopup("Launch point");
        launchMarker.addTo(map);
      } else {
        launchMarker.setLatLng([l.lat, l.lon]);
      }
    } else if (launchMarker) {
      map.removeLayer(launchMarker); launchMarker = null;
    }
  }
  function doRtl() { send({ type: "return_to_launch" }); }
  function doSetLaunch() { send({ type: "set_launch" }); }
  // set-rtl (settings panel) stays single-tap (confirmed modal context).
  const setRtlEl = $("set-rtl");
  if (setRtlEl) setRtlEl.addEventListener("click", doRtl);
  // Drive-away RTL buttons get 600 ms hold-to-engage.
  ["nav-rtl", "rm-rtl", "rtl-banner-go"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    if (VA.bindHold) VA.bindHold(el, 600, doRtl);
    else el.addEventListener("click", doRtl);
  });
  ["set-set-launch", "nav-set-launch", "rm-set-launch"].forEach((id) => { const el = $(id); if (el) el.addEventListener("click", doSetLaunch); });
  // Battery-strip RTL buttons are hold-to-engage (600 ms): RTL drives the
  // boat away, so it gets the same hold gate as MOB (owner decision).
  ["batt-warn-rtl", "batt-crit-rtl"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    if (VA.bindHold) VA.bindHold(el, 600, doRtl);
    else el.addEventListener("click", doRtl);
  });

  // ---- Wire STOP buttons in safety banners ----
  function doStop() { if (VA.sendCritical) VA.sendCritical({ type: "stop" }); }
  ["shallow-banner-stop", "link-banner-stop", "rtl-banner-stop", "batt-warn-stop",
   "batt-crit-stop", "mob-banner-stop", "aa-banner-stop", "gov-banner-stop",
  ].forEach((id) => { const el = $(id); if (el) el.addEventListener("click", doStop); });

  // ======================================================================
  // #62 SHALLOW / NO-GO ZONES
  // ======================================================================
  const NOGO_KEY = "vanchor-nogo-zones";
  const NOGO_VIS_KEY = "vanchor-nogo-visible";
  const MINDEPTH_KEY = "vanchor-min-depth";
  const FAILSAFE_KEY = "vanchor-fix-failsafe";
  let zones = [];            // [[ [lat,lon], ... ], ...]
  let nogoVisible = true;
  let minDepth = 0;
  let fixFailsafe = false;   // loss-of-fix failsafe; OFF by default
  const nogoLayer = (map && L) ? L.layerGroup() : null;
  // #23 server-persisted safety geometry: the SERVER is the source of truth,
  // localStorage is only a cache / offline fallback. Once we've seen the
  // server's safety_geometry we ADOPT it (never blindly re-send our stale
  // local copy). ``migrated`` guards a one-time local->server push used only
  // when the server has no geometry yet but this client does.
  let serverGeomSeen = false;
  let migrated = false;

  function loadZones() {
    try { const raw = localStorage.getItem(NOGO_KEY); if (raw) { const a = JSON.parse(raw); if (Array.isArray(a)) zones = a.filter((z) => Array.isArray(z) && z.length >= 3); } }
    catch (e) { /* ignore */ }
    try { nogoVisible = localStorage.getItem(NOGO_VIS_KEY) !== "0"; } catch (e) { /* ignore */ }
    try { const d = parseFloat(localStorage.getItem(MINDEPTH_KEY)); if (Number.isFinite(d)) minDepth = d; } catch (e) { /* ignore */ }
    try { fixFailsafe = localStorage.getItem(FAILSAFE_KEY) === "1"; } catch (e) { /* ignore */ }
  }
  function saveZones() { try { localStorage.setItem(NOGO_KEY, JSON.stringify(zones)); } catch (e) { /* ignore */ } }
  function sendZones() { send({ type: "set_nogo_zones", zones: zones }); }

  // ---- #23 server-geometry adoption (echo-loop-safe) -----------------
  // Value-equality on zone rings so adopting the server's geometry does NOT
  // trigger a redraw or (crucially) a re-send of an identical copy -- that is
  // what would otherwise create an echo loop (server -> client -> server -> ...).
  function zonesEqual(a, b) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) {
      const r1 = a[i], r2 = b[i];
      if (!Array.isArray(r1) || !Array.isArray(r2) || r1.length !== r2.length) return false;
      for (let j = 0; j < r1.length; j++) {
        if (Math.abs(r1[j][0] - r2[j][0]) > 1e-9 || Math.abs(r1[j][1] - r2[j][1]) > 1e-9) return false;
      }
    }
    return true;
  }
  function serverHasGeometry(g) {
    return (Array.isArray(g.nogo_zones) && g.nogo_zones.length > 0)
      || (Number.isFinite(g.min_depth_m) && g.min_depth_m > 0)
      || g.fix_failsafe_enabled === true;
  }
  // Adopt the server's geometry as the local truth. Purely a local update +
  // cache write: it NEVER calls send(), so it cannot echo back to the server.
  function adoptServerGeometry(g) {
    const sz = Array.isArray(g.nogo_zones)
      ? g.nogo_zones.filter((z) => Array.isArray(z) && z.length >= 3) : [];
    if (!zonesEqual(sz, zones)) {
      zones = sz.map((r) => r.map((p) => [p[0], p[1]]));
      saveZones();
      redrawZones();
    }
    if (Number.isFinite(g.min_depth_m) && g.min_depth_m !== minDepth) {
      minDepth = g.min_depth_m;
      try { localStorage.setItem(MINDEPTH_KEY, String(minDepth)); } catch (e) { /* ignore */ }
      applyMinDepthUI();
    }
    const ff = g.fix_failsafe_enabled === true;
    if (ff !== fixFailsafe) {
      fixFailsafe = ff;
      try { localStorage.setItem(FAILSAFE_KEY, ff ? "1" : "0"); } catch (e) { /* ignore */ }
      applyFailsafeUI();
    }
  }
  // Called on every telemetry frame carrying a safety_geometry block.
  function onServerGeometry(g) {
    if (!g || typeof g !== "object") return;
    serverGeomSeen = true;
    if (serverHasGeometry(g)) {
      // Server owns geometry -> adopt it. localStorage is now just a cache.
      adoptServerGeometry(g);
    } else if (!migrated && (zones.length || minDepth > 0 || fixFailsafe)) {
      // One-time migration: the server has NO geometry yet but this client has
      // a local copy -> push it up once so the server becomes the source.
      if (zones.length) sendZones();
      if (minDepth > 0) send({ type: "set_min_depth", min_depth_m: minDepth });
      if (fixFailsafe) send({ type: "set_fix_failsafe", enabled: true });
    }
    // Never migrate again once a geometry frame has been processed.
    migrated = true;
  }

  function redrawZones() {
    if (!nogoLayer) return;
    nogoLayer.clearLayers();
    zones.forEach((z) => {
      try {
        L.polygon(z, { className: "nogo-poly", color: "#ff5d7e", weight: 2, fillColor: "#ff5d7e", fillOpacity: 0.18, dashArray: "4,4" }).addTo(nogoLayer);
      } catch (e) { /* skip bad zone */ }
    });
    VA.setText("nogo-count", String(zones.length));
  }
  // Guard so the shared layers-control's add/remove handler doesn't recurse
  // while setNogoVisible toggles the layer on the map itself.
  let nogoSyncing = false;
  function setNogoVisible(on) {
    nogoVisible = !!on;
    if (nogoLayer && map) {
      nogoSyncing = true;
      if (nogoVisible) { if (!map.hasLayer(nogoLayer)) nogoLayer.addTo(map); }
      else if (map.hasLayer(nogoLayer)) map.removeLayer(nogoLayer);
      nogoSyncing = false;
    }
    try { localStorage.setItem(NOGO_VIS_KEY, nogoVisible ? "1" : "0"); } catch (e) { /* ignore */ }
    const box = $("nogo-show"); if (box) box.checked = nogoVisible;
  }

  let drawing = false;
  function setDrawingUI(on) {
    drawing = on;
    const drawBtn = $("nogo-draw"), cancelBtn = $("nogo-cancel");
    if (drawBtn) drawBtn.classList.toggle("hot", on);
    if (cancelBtn) cancelBtn.classList.toggle("hidden", !on);
    const status = $("nogo-status");
    if (status) status.textContent = on ? "Draw a closed area on the map…" : "";
  }
  const nogoDraw = $("nogo-draw");
  if (nogoDraw) nogoDraw.addEventListener("click", () => {
    if (!VA.map || !VA.map.startAreaSelect) { const s = $("nogo-status"); if (s) s.textContent = "Map not ready."; return; }
    if (drawing) { VA.map.cancelAreaSelect && VA.map.cancelAreaSelect(); setDrawingUI(false); return; }
    setNogoVisible(true);
    setDrawingUI(true);
    // close the settings drawer so the map is visible while drawing
    const drawerScrim = $("settings-scrim"); if (drawerScrim && !drawerScrim.classList.contains("hidden")) drawerScrim.click();
    VA.map.startAreaSelect({
      mode: "freehand",
      onDone(res) {
        setDrawingUI(false);
        if (VA.map.clearAreaShape) VA.map.clearAreaShape();
        if (res && Array.isArray(res.polygon) && res.polygon.length >= 3) {
          zones.push(res.polygon);
          saveZones(); redrawZones(); sendZones();
          const s = $("nogo-status"); if (s) s.textContent = "Added zone (" + zones.length + " total).";
        }
      },
    });
  });
  const nogoCancel = $("nogo-cancel");
  if (nogoCancel) nogoCancel.addEventListener("click", () => {
    if (VA.map && VA.map.cancelAreaSelect) VA.map.cancelAreaSelect();
    setDrawingUI(false);
  });
  const nogoClear = $("nogo-clear");
  if (nogoClear) nogoClear.addEventListener("click", () => {
    if (!zones.length) return;
    if (!window.confirm("Delete all " + zones.length + " no-go zones?")) return;
    zones = []; saveZones(); redrawZones(); sendZones();
    const s = $("nogo-status"); if (s) s.textContent = "Cleared.";
  });
  const nogoShow = $("nogo-show");
  if (nogoShow) nogoShow.addEventListener("change", () => setNogoVisible(nogoShow.checked));

  // min-depth
  const minDepthEl = $("min-depth"), minDepthVal = $("min-depth-val");
  function applyMinDepthUI() {
    if (minDepthEl) minDepthEl.value = minDepth;
    if (minDepthVal) minDepthVal.textContent = minDepth.toFixed(1);
  }
  if (minDepthEl) {
    minDepthEl.addEventListener("input", () => {
      const v = parseFloat(minDepthEl.value);
      if (minDepthVal) minDepthVal.textContent = Number.isFinite(v) ? v.toFixed(1) : "0.0";
    });
    minDepthEl.addEventListener("change", () => {
      const v = parseFloat(minDepthEl.value);
      minDepth = Number.isFinite(v) ? v : 0;
      try { localStorage.setItem(MINDEPTH_KEY, String(minDepth)); } catch (e) { /* ignore */ }
      send({ type: "set_min_depth", min_depth_m: minDepth });
    });
  }

  // loss-of-fix failsafe switch (off by default): stop the motor if GPS drops out
  const failsafeEl = $("fix-failsafe");
  function applyFailsafeUI() { if (failsafeEl) failsafeEl.checked = fixFailsafe; }
  if (failsafeEl) {
    failsafeEl.addEventListener("change", () => {
      fixFailsafe = failsafeEl.checked;
      try { localStorage.setItem(FAILSAFE_KEY, fixFailsafe ? "1" : "0"); } catch (e) { /* ignore */ }
      send({ type: "set_fix_failsafe", enabled: fixFailsafe });
    });
  }

  // Land-collision guard: switch + stop-distance (persisted server-side) and
  // the predicted stop point drawn on the map (amber = watching, red = tripped).
  const lgBox = $("land-guard"), lgSlider = $("land-guard-m"), lgVal = $("land-guard-m-val");
  const lgHint = $("land-guard-hint");
  if (lgBox) lgBox.addEventListener("change", () =>
    send({ type: "set_land_guard", enabled: lgBox.checked }));
  if (lgSlider) lgSlider.addEventListener("change", () => {
    send({ type: "set_land_guard", margin_m: parseFloat(lgSlider.value) });
  });
  if (lgSlider && lgVal) lgSlider.addEventListener("input", () => { lgVal.textContent = lgSlider.value; });
  let landMarker = null;
  function landStopIcon(tripped) {
    return L.divIcon({
      className: "",
      html: `<div class="land-stop-pin${tripped ? " tripped" : ""}"><span>⛔</span></div>`,
      iconSize: [26, 26], iconAnchor: [13, 13],
    });
  }
  VA.onTelemetry((t) => {
    const lg = t && t.safety && t.safety.land_guard;
    if (!lg) return;
    if (lgBox && document.activeElement !== lgBox && lg.enabled !== undefined) lgBox.checked = !!lg.enabled;
    if (lgSlider && document.activeElement !== lgSlider && Number.isFinite(lg.margin_m)) {
      lgSlider.value = String(lg.margin_m);
      if (lgVal) lgVal.textContent = String(Math.round(lg.margin_m));
    }
    if (lgHint) {
      lgHint.textContent = !lg.enabled ? ""
        : (lg.have_chart ? (lg.active ? "" : "Active while driving manually.")
                         : "No offline water chart for this area yet — guard idle.");
    }
    const show = lg.active && lg.stop && map;
    if (show) {
      const ll = [lg.stop.lat, lg.stop.lon];
      if (!landMarker) {
        landMarker = L.marker(ll, { icon: landStopIcon(lg.tripped), zIndexOffset: 900, interactive: false }).addTo(map);
      } else {
        landMarker.setLatLng(ll);
        landMarker.setIcon(landStopIcon(lg.tripped));
      }
    } else if (landMarker) {
      map.removeLayer(landMarker);
      landMarker = null;
    }
    if (lg.tripped && VA.logAlert) VA.logAlert("alarm", "Land ahead — stopped by the land guard", { level: "medium" });
  });

  // Auto Follow-APB (opt-in): the switch reflects + drives the server-side
  // setting (persisted in safety.json); the banner shows while a Follow-APB
  // session was AUTO-engaged, with a one-tap disengage.
  const autoApbEl = $("auto-apb");
  if (autoApbEl) {
    autoApbEl.addEventListener("change", () =>
      send({ type: "set_auto_apb", enabled: autoApbEl.checked }));
  }
  const autoApbBanner = $("auto-apb-banner");
  const autoApbStop = $("auto-apb-banner-stop");
  if (autoApbStop) autoApbStop.addEventListener("click", () => send({ type: "stop" }));
  let prevAutoApb = false;
  VA.onTelemetry((t) => {
    const aa = t && t.auto_apb;
    if (!aa) return;
    if (autoApbEl && document.activeElement !== autoApbEl) autoApbEl.checked = !!aa.enabled;
    const engaged = !!aa.engaged;
    if (autoApbBanner) autoApbBanner.classList.toggle("hidden", !engaged);
    if (engaged && !prevAutoApb && VA.logAlert) {
      VA.logAlert("warn", "External autopilot detected — Follow-APB engaged automatically", { level: "low" });
    }
    prevAutoApb = engaged;
  });

  // ======================================================================
  // #63 MAN-OVERBOARD
  // ======================================================================
  let mobMarker = null;
  function mobIcon() {
    return L.divIcon({ className: "", html: '<div class="mob-pin"><span>🛟</span></div>', iconSize: [30, 30], iconAnchor: [15, 15], popupAnchor: [0, -16] });
  }
  let mobWasActive = false;
  function renderMob(t) {
    const m = (t && t.mob) || null;
    const active = !!(m && m.active);
    // Log to the alert history (#97) on the false→true edge.
    if (active && !mobWasActive && VA.logAlert) VA.logAlert("alarm", "Man overboard — returning", { level: "high" });
    mobWasActive = active;
    const mobBanner = $("mob-banner");
    if (mobBanner) mobBanner.classList.toggle("hidden", !active);
    const rmBanner = $("rm-mob-banner");
    if (rmBanner) rmBanner.classList.toggle("hidden", !active);
    if (!map || !L) return;
    if (active && Number.isFinite(m.lat) && Number.isFinite(m.lon)) {
      if (!mobMarker) {
        mobMarker = L.marker([m.lat, m.lon], { icon: mobIcon(), zIndexOffset: 900 });
        mobMarker.bindPopup("Man overboard");
        mobMarker.addTo(map);
      } else mobMarker.setLatLng([m.lat, m.lon]);
    } else if (mobMarker) {
      map.removeLayer(mobMarker); mobMarker = null;
    }
  }
  // Manual MOB trigger buttons were removed (#99). MOB state is still driven
  // passively from telemetry (`mob.active`), so the banners + marker remain;
  // the Clear buttons stay wired in case a MOB is raised by the backend.
  function clearMob() { send({ type: "mob_clear" }); }
  ["mob-banner-clear", "rm-mob-clear"].forEach((id) => { const el = $(id); if (el) el.addEventListener("click", clearMob); });

  // ======================================================================
  // #61 / #62 / #64 BANNERS
  // ======================================================================
  function renderBanners(t) {
    // RTL recommended (battery just enough to get home). Hidden while a
    // battery threshold strip is up — those carry their own RTL button, and
    // two stacked "return now" strips would just shout twice.
    const rtl = $("rtl-banner");
    const socNow = t && t.battery && Number.isFinite(t.battery.soc_pct) ? t.battery.soc_pct : null;
    const battStripUp = battLevel(socNow) !== "ok" && battLevel(socNow) !== "none";
    if (rtl) rtl.classList.toggle("hidden", !(t && t.rtl_recommended) || battStripUp);

    // shallow / no-go auto-stop — no emoji, text-only. Honest copy: the
    // governor re-evaluates every tick, so thrust RESUMES AUTOMATICALLY when
    // the depth clears — that's why there is no Resume button (only STOP,
    // which disengages the mode entirely).
    const safety = (t && t.safety) || {};
    const shallow = !!safety.shallow_stop, nogo = !!safety.nogo_stop;
    const sb = $("shallow-banner");
    if (sb) {
      sb.classList.toggle("hidden", !(shallow || nogo));
      const msg = $("shallow-banner-msg");
      const resumeTxt = Number.isFinite(minDepth) && minDepth > 0
        ? " · resumes when deeper than " + minDepth.toFixed(1) + " m" : "";
      if (msg) msg.textContent = nogo && !shallow ? "NO-GO ZONE — auto-stopped"
        : shallow && nogo ? "SHALLOW / NO-GO — auto-stopped" + resumeTxt
        : "SHALLOW — auto-stopped" + resumeTxt;
    }

    // link-loss failsafe
    const link = (t && t.link) || null;
    const lb = $("link-banner");
    if (lb) {
      lb.classList.toggle("hidden", !(link && link.failsafe_engaged));
      const msg = $("link-banner-msg");
      if (msg && link && link.failsafe_engaged) {
        const action = link.failsafe_action;
        msg.textContent = action === "stop" ? "Connection lost — motor stopped"
          : action === "continue" ? "Connection lost — continuing mission"
          : "Connection lost — holding position";
      }
    }

    // Safety governor advisory.
    // reverse-block + thrust-slew-limit are ROUTINE, expected mechanics while a
    // station-keeper holds: Smart/Leif vector the motor and flip thrust sign
    // several times a second, and the reverse interlock (a motor-protection
    // dead-time) trips on each flip. Surfacing that per-tick strobed the banner
    // ~2x/second during a perfectly healthy Smart anchor hold. Only advise the
    // user of a SUSTAINED governor intervention (continuously active ≥ the
    // dwell) — a genuinely stuck condition worth acting on — and never the
    // normal sub-second flicker. Any clear tick resets the dwell, so flapping
    // never reaches the threshold.
    const gov = $("gov-banner");
    if (gov) {
      const govActive = !!(safety.thrust_limited || safety.reverse_blocked);
      const now = Date.now();
      if (!govActive) _govSince = 0;
      else if (_govSince === 0) _govSince = now;
      const govShow = govActive && (now - _govSince) >= GOV_DWELL_MS;
      gov.classList.toggle("hidden", !govShow);
      const msg = $("gov-banner-msg");
      if (msg && govShow) {
        msg.textContent = safety.reverse_blocked ? "Reverse blocked by safety governor"
          : "Thrust limited by safety governor";
      }
    }
  }

  // ---- Anchor / drag alarm strip (D2) ----
  // One strip covers BOTH alarm shapes:
  //  - passive watch-circle alarm (t.anchor_alarm.firing): the motor is OFF —
  //    RECOVER (600 ms hold) re-engages anchor_hold at the ALARM point via the
  //    server's anchor_alarm_recover command.
  //  - active-hold drag alarm (t.safety.drag_alarm): the motor is already
  //    fighting for the point — RECOVER is hidden, actions are SILENCE + STOP.
  // Subtitle carries cause + consequence direction + elapsed:
  //   "38 m from anchor · drifting SW · 0:24".
  let _aaFiringSince = 0;   // client-side firing-edge timestamp (either alarm)
  let _aaSilencedUntil = 0; // SILENCE window — sound muted, strip STAYS visible

  function _compass8(fromLat, fromLon, toLat, toLon) {
    const k = Math.PI / 180;
    const dLon = (toLon - fromLon) * k;
    const y = Math.sin(dLon) * Math.cos(toLat * k);
    const x = Math.cos(fromLat * k) * Math.sin(toLat * k)
      - Math.sin(fromLat * k) * Math.cos(toLat * k) * Math.cos(dLon);
    const brg = (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(brg / 45) % 8];
  }
  function _fmtElapsed(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }

  function renderAnchorAlarmStrip(t) {
    const banner = $("anchor-alarm-banner");
    if (!banner) return;
    const aa = (t && t.anchor_alarm) || {};
    const passive = !!aa.firing;
    const drag = !!(t && t.safety && t.safety.drag_alarm);
    const firing = passive || drag;

    if (firing && !_aaFiringSince) _aaFiringSince = Date.now();
    if (!firing) { _aaFiringSince = 0; _aaSilencedUntil = 0; }

    // The strip stays visible while silenced — SILENCE mutes sound only.
    banner.classList.toggle("hidden", !firing);

    const title = $("aa-banner-title");
    const msg = $("aa-banner-msg");
    const recoverBtn = $("aa-banner-recover");
    if (recoverBtn) recoverBtn.classList.toggle("hidden", !passive);
    if (!firing) return;

    if (title) title.textContent = passive ? "ANCHOR ALARM — DRAGGING" : "ANCHOR DRAGGING";

    // Subtitle: distance + drift direction + elapsed since the firing edge.
    const pos = t && t.position;
    const aLat = passive ? aa.lat : (t && t.anchor && t.anchor.lat);
    const aLon = passive ? aa.lon : (t && t.anchor && t.anchor.lon);
    const dist = passive
      ? (Number.isFinite(aa.distance_m) ? aa.distance_m : null)
      : (t && Number.isFinite(t.distance_to_anchor_m) ? t.distance_to_anchor_m : null);
    const parts = [];
    if (dist !== null) parts.push(Math.round(dist) + (passive ? " m from anchor" : " m from hold point"));
    if (pos && Number.isFinite(aLat) && Number.isFinite(aLon)
        && Number.isFinite(pos.lat) && Number.isFinite(pos.lon)) {
      parts.push("drifting " + _compass8(aLat, aLon, pos.lat, pos.lon));
    }
    parts.push(_fmtElapsed(Date.now() - _aaFiringSince));
    if (msg) msg.textContent = parts.join(" · ");

    // SILENCE countdown on the button ("1:43"), reverting when it expires.
    const silenceBtn = $("aa-banner-silence");
    if (silenceBtn) {
      const left = _aaSilencedUntil - Date.now();
      silenceBtn.innerHTML = left > 0
        ? "SILENCED<small>" + _fmtElapsed(left) + "</small>"
        : "SILENCE<small>2 MIN</small>";
    }
  }

  // Wire anchor-alarm RECOVER (600 ms hold), SILENCE (sound-only, 2 min).
  // RECOVER uses the server's anchor_alarm_recover: it engages anchor_hold at
  // the ALARM point (not wherever the boat has drifted to).
  (function () {
    const recoverBtn = $("aa-banner-recover");
    const doRecover = () => send({ type: "anchor_alarm_recover" });
    if (recoverBtn && VA.bindHold) VA.bindHold(recoverBtn, 600, doRecover);
    else if (recoverBtn) recoverBtn.addEventListener("click", doRecover);
    const silenceBtn = $("aa-banner-silence");
    if (silenceBtn) {
      silenceBtn.addEventListener("click", () => {
        _aaSilencedUntil = Date.now() + 120000; // 2 minutes, sound only
        if (VA.sound && VA.sound.silence) VA.sound.silence(120000);
      });
    }
  })();

  // ======================================================================
  // #64 LINK-LOSS INDICATOR (status chip)
  // ======================================================================
  function renderLink(t) {
    const link = (t && t.link) || null;
    const chip = $("chip-link");
    if (chip) chip.classList.toggle("hidden", !link);
    if (!link) return;
    const connected = !!link.client_connected;
    const failsafe = !!link.failsafe_engaged;
    if (chip) chip.dataset.state = failsafe ? "bad" : connected ? "ok" : "warn";
    VA.setText("chip-link-val", failsafe ? "FAILSAFE" : connected ? "OK" : "LOST");
    VA.setText("set-link-state", connected ? ("connected" + (Number.isFinite(link.since_s) ? " (" + fmtDuration(link.since_s) + ")" : "")) : "disconnected");
    VA.setText("set-link-failsafe", failsafe ? "ENGAGED — holding position" : "—");
  }

  // ======================================================================
  // telemetry hook
  // ======================================================================
  VA.onTelemetry(function (t) {
    renderBattery(t);
    renderLaunch(t);
    renderMob(t);
    renderLink(t);
    renderBanners(t);
    renderAnchorAlarmStrip(t);
    // #23: adopt the SERVER's safety geometry (browser is a cache). The
    // immediate snapshot on WS connect carries this, so a freshly-opened client
    // paints the server's zones without waiting.
    if (t && t.safety_geometry) onServerGeometry(t.safety_geometry);
  });

  // ======================================================================
  // init
  // ======================================================================
  loadZones();
  applyMinDepthUI();
  applyFailsafeUI();
  if (nogoLayer) {
    redrawZones();
    // Register the No-go zones overlay into the shared layers control (#86) so
    // it can be toggled from the top-left panel as well as the Settings
    // checkbox. Register BEFORE the initial setNogoVisible so our own
    // NOGO_VIS_KEY restore also reflects in the control.
    if (VA.map && typeof VA.map.addOverlay === "function") {
      VA.map.addOverlay("No-go zones", nogoLayer, {
        persistKey: NOGO_VIS_KEY,
        onToggle(on) { if (!nogoSyncing) setNogoVisible(on); },
      });
    }
    setNogoVisible(nogoVisible);
  }
  // #23: geometry now lives on the SERVER and is delivered in telemetry
  // (`safety_geometry`), which onServerGeometry() adopts / migrates. We no
  // longer blindly re-send our local copy -- that would clobber server truth
  // with a stale client and could echo-loop. This timer is only a FALLBACK for
  // an OLD backend that never sends a safety_geometry block: if none has been
  // seen ~1.5 s after load, push our local copy up once (legacy behaviour).
  setTimeout(() => {
    if (serverGeomSeen) return;   // server owns geometry; don't clobber it
    if (zones.length) sendZones();
    if (minDepth > 0) send({ type: "set_min_depth", min_depth_m: minDepth });
    if (fixFailsafe) send({ type: "set_fix_failsafe", enabled: true });
  }, 1500);

  VA.safety = {
    zones() { return zones.slice(); },
    minDepth() { return minDepth; },
    setNogoVisible, isNogoVisible() { return nogoVisible; },
    // Exposed for tests / debugging: the #23 server-geometry adoption path and
    // its echo-guard equality check.
    onServerGeometry, zonesEqual, serverHasGeometry,
    serverGeomSeen() { return serverGeomSeen; },
  };
})();
