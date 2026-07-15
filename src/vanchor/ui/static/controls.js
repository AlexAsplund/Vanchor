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
  // The steering slider is replaced by the steering WHEEL (steerwheel.js);
  // this section owns the shared manual state, the thrust slider and the
  // Relative/Absolute mode toggle, exposed to the wheel via VA.manualCtl.
  const thrust = $("thrust");
  // Steering mode: RELATIVE (default; the wheel handle is a deflection off
  // the bow), ABSOLUTE (the handle is a compass bearing 0..359 the motor
  // head HOLDS server-side while the boat yaws — 0 north, 180 south), or
  // COURSE (the boat FOLLOWS the ground-track line drawn from the engage
  // position along the bearing — cross-track corrected server-side).
  const STEER_MODE_KEY = "vanchor-steer-mode";
  const STEER_MODES = ["relative", "absolute", "course"];
  let steerMode = "relative";
  try {
    const saved = localStorage.getItem(STEER_MODE_KEY);
    if (STEER_MODES.includes(saved)) steerMode = saved;
  } catch (e) { /* ignore */ }
  // Shared manual state: the wheel and the thrust slider both read/write it;
  // sendManual() turns it into the mode-appropriate command. steerBearing is
  // the compass value shared by BOTH absolute and course modes.
  const mstate = { thrust: 0.0, steerNorm: 0.0, steerBearing: 0.0 };
  const wrap180 = (d) => ((d + 180) % 360 + 360) % 360 - 180;
  const heading = () => (VA.last && Number.isFinite(VA.last.heading_deg)) ? VA.last.heading_deg : 0;
  function sendManual() {
    mstate.lastSentMs = Date.now();
    const brg = Math.round(((mstate.steerBearing % 360) + 360) % 360);
    if (steerMode === "course") {
      send({ type: "manual", thrust: mstate.thrust, steer_course: brg });
    } else if (steerMode === "absolute") {
      send({ type: "manual", thrust: mstate.thrust, steer_bearing: brg });
    } else {
      send({ type: "manual", thrust: mstate.thrust,
             steering: Math.round(mstate.steerNorm * 1000) / 1000 });
    }
  }
  // Force the motor-engaging slider to a dead-stop 0 at load: a browser can
  // restore a non-zero value from bfcache/form-restore across a reload (incl.
  // the service-worker auto-reload), which — combined with any load-time send —
  // would be a hands-free motor command. bindSlider only refreshes the display
  // now, so this sends nothing; genuine slider input does.
  if (thrust) thrust.value = "0";
  bindSlider("thrust", "thrust-val", () => {
    mstate.thrust = parseFloat(thrust.value);
    sendManual();
    if (onStateEdit) onStateEdit();
  });
  // Snap to dead-center 0 when released near zero (avoids tiny residual nudges).
  if (thrust) thrust.addEventListener("change", () => {
    if (Math.abs(parseFloat(thrust.value)) < 0.12) { thrust.value = "0"; thrust.dispatchEvent(new Event("input")); }
  });
  // Mode toggle: converts the CURRENT head direction between frames (so
  // switching never moves the head). While ACTIVELY driving in manual it
  // re-sends the converted command, so the server's hold semantics switch
  // immediately (e.g. the head starts holding its compass bearing without
  // waiting for the next wheel touch). When idle it sends nothing — the
  // motor never ENGAGES from a mode switch.
  const steerSeg = $("steer-mode-seg");
  const modeListeners = [];
  let onStateEdit = null;   // wheel's re-render hook (set via VA.manualCtl)
  function applySteerMode(mode, persist) {
    const prev = steerMode;
    steerMode = STEER_MODES.includes(mode) ? mode : "relative";
    if (steerMode !== prev) {
      // "Actively driving": the boat is in manual AND something is commanded
      // (our thrust, or a live motor command from this manual session).
      const motor = (VA.last && VA.last.motor) || {};
      const driving = VA.last && VA.last.mode === "manual" &&
        (mstate.thrust !== 0 || Math.abs(motor.thrust || 0) > 0.005 ||
         Math.abs(motor.steering || 0) > 0.005);
      // Convert the current direction between frames so the head/track never
      // jumps on a switch. absolute<->course share the compass bearing.
      if (prev === "relative" && steerMode !== "relative") {
        mstate.steerBearing = ((heading() + mstate.steerNorm * 180) % 360 + 360) % 360;
      } else if (steerMode === "relative" && prev !== "relative") {
        mstate.steerNorm = wrap180(mstate.steerBearing - heading()) / 180;
      }
      if (driving) sendManual();
    }
    if (steerSeg) steerSeg.querySelectorAll("button").forEach((b) =>
      b.classList.toggle("on", b.dataset.steermode === steerMode));
    if (persist) { try { localStorage.setItem(STEER_MODE_KEY, steerMode); } catch (e) { /* ignore */ } }
    modeListeners.forEach((cb) => { try { cb(steerMode); } catch (e) { /* ignore */ } });
  }
  if (steerSeg) steerSeg.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => applySteerMode(b.dataset.steermode, true)));
  applySteerMode(steerMode, false);
  // API for the steering wheel (steerwheel.js).
  VA.manualCtl = {
    state: mstate,
    mode: () => steerMode,
    sendManual,
    onModeChange(cb) { modeListeners.push(cb); },
    onStateEdit(cb) { onStateEdit = cb; },
    // The wheel's radial thrust gesture drives the slider display (without
    // re-triggering its input handler, which would double-send).
    setThrust(v) {
      mstate.thrust = v;
      if (thrust) thrust.value = String(v);
      const out = $("thrust-val");
      if (out) out.textContent = v.toFixed(2);
    },
  };
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
