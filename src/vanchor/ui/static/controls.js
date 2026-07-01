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
  const manual = () => send({ type: "manual", thrust: parseFloat(thrust.value), steering: parseFloat(steer.value) });
  // Force the motor-engaging sliders to a dead-stop 0 at load: a browser can
  // restore a non-zero value from bfcache/form-restore across a reload (incl.
  // the service-worker auto-reload), which — combined with any load-time send —
  // would be a hands-free motor command. bindSlider only refreshes the display
  // now, so neither of these sends anything; genuine slider input does.
  [thrust, steer].forEach((el) => { if (el) el.value = "0"; });
  bindSlider("thrust", "thrust-val", manual);
  bindSlider("steer", "steer-val", manual);
  // Snap to dead-center 0 when released near zero (avoids tiny residual nudges).
  [thrust, steer].forEach((el) => {
    if (!el) return;
    el.addEventListener("change", () => {
      if (Math.abs(parseFloat(el.value)) < 0.12) { el.value = "0"; el.dispatchEvent(new Event("input")); }
    });
  });
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
  function applyAnchor(redrop) {
    // "Smart" -> the learned NN station-keeper (anchor_ml); the backend falls
    // back to the PID anchor_hold automatically if the model isn't loaded.
    const smart = smartBox && smartBox.checked;
    const cmd = { type: smart ? "anchor_ml" : "anchor_hold",
                  radius_m: parseFloat(arSlider.value), hold_heading: holdHdgBox.checked };
    const last = VA.map.getLastAnchor();
    if (!redrop && last) cmd.anchor = { lat: last.lat, lon: last.lon };
    send(cmd);
  }
  bindSlider("ar", "ar-val");
  arSlider.addEventListener("change", () => { if (VA.map.getLastAnchor()) applyAnchor(false); });
  holdHdgBox.addEventListener("change", () => { if (VA.map.getLastAnchor()) applyAnchor(false); });
  if (smartBox) smartBox.addEventListener("change", () => { if (VA.map.getLastAnchor()) applyAnchor(false); });
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
    if (btn) { btn.classList.toggle("recording", rec); btn.textContent = rec ? `● Recording (${count})` : "● Record"; }
    const badge = $("track-state");
    if (badge) badge.textContent = rec ? "● rec " + count : (count ? count + " pts" : "");
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
    // Keep the "Smart" toggle honest: reflect the live anchor mode.
    if (smartBox && (t.mode === "anchor_ml" || t.mode === "anchor_hold")) {
      smartBox.checked = t.mode === "anchor_ml";
    }
  });
})();
