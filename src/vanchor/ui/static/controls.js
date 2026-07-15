/* Vanchor-NG — manual / hold / anchor / drift / cruise / track controls.
 *
 * The contextual control panels driven from the mode rail: manual thrust/steer
 * sliders, heading hold, follow-APB, drift (speed + heading), anchor hold +
 * jog, cruise speed, and the track recorder (record / replay / backtrack).
 *
 * Each concern registers its own "enter mode" command on VA.ui.modeCommands and
 * its own telemetry handler so panels reflect live state without one giant
 * render() function.
 */
"use strict";

(function () {
  const { $, send, bindSlider, modeCommands } = VA.ui;

  // ===== manual ============================================================
  const thrust = $("thrust"), steer = $("steer");
  // Steering mode: RELATIVE (default; the slider is a normalized deflection
  // off the bow) or ABSOLUTE (the slider is a compass bearing 0..359 the motor
  // head HOLDS server-side while the boat yaws — 0 north, 180 south).
  const STEER_MODE_KEY = "vanchor-steer-mode";
  let steerMode = "relative";
  try { if (localStorage.getItem(STEER_MODE_KEY) === "absolute") steerMode = "absolute"; } catch (e) { /* ignore */ }
  const manual = () => {
    const t = parseFloat(thrust.value);
    if (steerMode === "absolute") send({ type: "manual", thrust: t, steer_bearing: parseFloat(steer.value) });
    else send({ type: "manual", thrust: t, steering: parseFloat(steer.value) });
  };
  // Force the motor-engaging sliders to a dead-stop 0 at load: a browser can
  // restore a non-zero value from bfcache/form-restore across a reload (incl.
  // the service-worker auto-reload), which — combined with any load-time send —
  // would be a hands-free motor command. bindSlider only refreshes the display
  // now, so neither of these sends anything; genuine slider input does.
  [thrust, steer].forEach((el) => { if (el) el.value = "0"; });
  bindSlider("thrust", "thrust-val", manual);
  bindSlider("steer", "steer-val", manual);
  // Snap to dead-center 0 when released near zero (avoids tiny residual
  // nudges). Relative steering only — in absolute mode 0 means "north".
  [thrust, steer].forEach((el) => {
    if (!el) return;
    el.addEventListener("change", () => {
      if (el === steer && steerMode === "absolute") return;
      if (Math.abs(parseFloat(el.value)) < 0.12) { el.value = "0"; el.dispatchEvent(new Event("input")); }
    });
  });
  // Mode toggle: reconfigures the slider WITHOUT sending anything (the motor
  // engages solely from slider input). Absolute seeds the bearing from the
  // live heading so the first touch doesn't swing the head somewhere new.
  const steerSeg = $("steer-mode-seg"), steerLabel = $("steer-label");
  function applySteerMode(mode, persist) {
    steerMode = mode === "absolute" ? "absolute" : "relative";
    if (steerMode === "absolute") {
      const hdg = (VA.last && Number.isFinite(VA.last.heading_deg)) ? Math.round(VA.last.heading_deg) : 0;
      steer.min = "0"; steer.max = "359"; steer.step = "1"; steer.value = String(hdg);
      if (steerLabel) steerLabel.firstChild.textContent = "Motor bearing ° ";
    } else {
      steer.min = "-1"; steer.max = "1"; steer.step = "0.05"; steer.value = "0";
      if (steerLabel) steerLabel.firstChild.textContent = "Steering ";
    }
    const out = $("steer-val");
    if (out) out.textContent = steer.value;
    if (steerSeg) steerSeg.querySelectorAll("button").forEach((b) =>
      b.classList.toggle("on", b.dataset.steermode === steerMode));
    if (persist) { try { localStorage.setItem(STEER_MODE_KEY, steerMode); } catch (e) { /* ignore */ } }
  }
  if (steerSeg) steerSeg.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => applySteerMode(b.dataset.steermode, true)));
  applySteerMode(steerMode, false);
  // NOTE: no modeCommands.manual — tapping the Manual rail button selects the
  // panel only (see appcore.js). The motor engages solely from slider input, so
  // selecting Manual can't re-apply a stale throttle value.

  // ===== heading hold ======================================================
  bindSlider("hdg", "hdg-val");
  // Explicit engage control only — the rail button just opens this panel, so
  // selecting Heading-hold no longer one-taps the motor to 40% throttle.
  $("hdg-go").addEventListener("click", () =>
    send({ type: "heading_hold", heading: parseFloat($("hdg").value), throttle: 0.4 }));

  // ===== follow APB ========================================================
  const apbGo = $("apb-go");
  if (apbGo) apbGo.addEventListener("click", () => send({ type: "follow_apb" }));

  // ===== drift =============================================================
  const driftKnots = $("drift-knots");
  const driftHdg = $("drift-hdg");
  let driftKnotsActive = false, driftHdgActive = false;
  bindSlider("drift-knots", "drift-knots-val", () => {
    if (VA.ui.currentMode === "drift") send({ type: "drift", knots: parseFloat(driftKnots.value) });
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
  // Explicit engage control only — the rail button just opens this panel.
  $("drift-go").addEventListener("click", () =>
    send({ type: "drift", heading: parseFloat(driftHdg.value), knots: parseFloat(driftKnots.value) }));

  function updateDrift(t) {
    if (t.mode !== "drift") return;
    const kn = VA.fin(t.drift_target_knots);
    if (kn !== null && !driftKnotsActive) { driftKnots.value = kn; $("drift-knots-val").textContent = driftKnots.value; }
    const hdg = VA.fin(t.target_heading);
    if (hdg !== null && !driftHdgActive) { driftHdg.value = Math.round(hdg); $("drift-hdg-val").textContent = driftHdg.value; }
  }

  // ===== anchor + jog ======================================================
  const arSlider = $("ar");
  const holdHdgBox = $("hold-hdg");
  const smartBox = $("anchor-smart");
  const leifBox = $("anchor-leif");
  const vectoredBox = $("anchor-vectored");
  function applyAnchor(redrop) {
    // Station-keeper choice: "Leif" (pure full-azimuth learned) > "Smart"
    // (hybrid learned) > PID anchor_hold. The backend falls back automatically
    // if a model isn't loaded. "Vectored" drives the PID keeper through the full
    // rotation (an anchor_hold flag; the learned modes vector on their own).
    const leif = leifBox && leifBox.checked;
    const smart = smartBox && smartBox.checked;
    const type = leif ? "anchor_leif" : (smart ? "anchor_ml" : "anchor_hold");
    const cmd = { type, radius_m: parseFloat(arSlider.value),
                  hold_heading: holdHdgBox.checked,
                  vectored: !!(vectoredBox && vectoredBox.checked) };
    const last = VA.map.getLastAnchor();
    if (!redrop && last) cmd.anchor = { lat: last.lat, lon: last.lon };
    send(cmd);
  }
  // Leif and Smart are alternative learned keepers -- only one at a time.
  function pickKeeper(chosen) {
    if (chosen === smartBox && smartBox.checked && leifBox) leifBox.checked = false;
    if (chosen === leifBox && leifBox.checked && smartBox) smartBox.checked = false;
    if (VA.map.getLastAnchor()) applyAnchor(false);
  }
  bindSlider("ar", "ar-val");
  arSlider.addEventListener("change", () => { if (VA.map.getLastAnchor()) applyAnchor(false); });
  holdHdgBox.addEventListener("change", () => { if (VA.map.getLastAnchor()) applyAnchor(false); });
  if (smartBox) smartBox.addEventListener("change", () => pickKeeper(smartBox));
  if (leifBox) leifBox.addEventListener("change", () => pickKeeper(leifBox));
  if (vectoredBox) vectoredBox.addEventListener("change", () => { if (VA.map.getLastAnchor()) applyAnchor(false); });
  // Explicit engage control only — the rail button just opens this panel, so
  // selecting Anchor no longer drops the anchor and engages station-keeping on
  // a single tap; the user presses "Drop anchor" (#anchor-go) to engage.
  $("anchor-go").addEventListener("click", () => applyAnchor(true));
  [["jog-fwd", "forward"], ["jog-back", "back"], ["jog-left", "left"], ["jog-right", "right"]]
    .forEach(([id, direction]) => {
      const el = $(id);
      if (el) el.addEventListener("click", () => send({ type: "jog", direction }));
    });

  // ===== cruise ============================================================
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

  // ===== track (record / replay / backtrack) ===============================
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
    if (btn) {
      const label = rec ? `● Recording (${count})` : "● Record";
      // classList.toggle + textContent every frame is wasteful; guard both on label change.
      if (btn.textContent !== label) {
        btn.classList.toggle("recording", rec);
        btn.textContent = label;
      }
    }
    const badge = $("track-state");
    if (badge) {
      const badgeText = rec ? "● rec " + count : (count ? count + " pts" : "");
      if (badge.textContent !== badgeText) badge.textContent = badgeText;
    }
  }

  // stop mode is a bare command with no panel of its own. STOP must never gain
  // friction and must be verifiable, so it uses sendCritical (WS + POST, with a
  // telemetry-confirmed banner if the boat doesn't actually stop).
  modeCommands.stop = () => VA.sendCritical({ type: "stop" });

  // ---- telemetry reflection for these panels ----
  VA.onTelemetry(function renderControls(t) {
    updateCruise(t.cruise);
    updateTrack(t.track);
    updateDrift(t);
    // Keep the keeper toggles honest: reflect the live anchor mode.
    if (t.mode === "anchor_ml" || t.mode === "anchor_hold" || t.mode === "anchor_leif") {
      if (smartBox) smartBox.checked = t.mode === "anchor_ml";
      if (leifBox) leifBox.checked = t.mode === "anchor_leif";
    }
  });
})();
