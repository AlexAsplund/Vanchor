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
    // HOLD toggle: true = keep thrust on release (trolling), false = snap to 0.
    holdThrust() {
      const el = document.getElementById("wheel-hold");
      return !!(el && el.checked);
    },
  };
  // NOTE: no modeCommands.manual — tapping the Manual rail button selects the
  // panel only (see appcore.js). The motor engages solely from slider input, so
  // selecting Manual can't re-apply a stale throttle value.

  // ===== heading hold ======================================================
  // REMOVED from the UI (2026-07-15): superseded by Manual mode's Absolute
  // (hold a compass bearing) and Course (follow the track line) steering.
  // The heading_hold COMMAND remains for the API / RF remotes / connectors.

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
  const vectoredBox = $("anchor-vectored");
  // Hidden legacy checkboxes (kept for telemetry reflection compat below)
  const smartBox = $("anchor-smart");
  const leifBox = $("anchor-leif");

  // Anchor style segmented control (WP7 item 24).
  let anchorStyle = "classic";  // "classic" | "smart" | "leif"
  const ANCHOR_STYLE_KEY = "vanchor-anchor-style";
  try {
    const saved = localStorage.getItem(ANCHOR_STYLE_KEY);
    if (saved === "classic" || saved === "smart" || saved === "leif") anchorStyle = saved;
  } catch (e) { /* ignore */ }

  const ANCHOR_STYLE_DESCS = {
    classic: "Classic — steady PID hold at the point.",
    smart: "Smart — learned station-keeping, uses less battery.",
    leif: "Leif — experimental: never idles, expect constant motion.",
  };

  function styleType() {
    return anchorStyle === "leif" ? "anchor_leif"
      : anchorStyle === "smart" ? "anchor_ml" : "anchor_hold";
  }

  const anchorStyleSeg = $("anchor-style-seg");
  const anchorStyleDesc = $("anchor-style-desc");

  function applyAnchorStyleUI() {
    if (anchorStyleSeg) anchorStyleSeg.querySelectorAll("button").forEach((b) =>
      b.classList.toggle("on", b.dataset.astyle === anchorStyle));
    if (anchorStyleDesc) anchorStyleDesc.textContent = ANCHOR_STYLE_DESCS[anchorStyle] || "";
    // Keep legacy hidden checkboxes in sync (telemetry reflection below still reads them).
    if (smartBox) smartBox.checked = anchorStyle === "smart";
    if (leifBox) leifBox.checked = anchorStyle === "leif";
  }

  function isAnchorModeActive() {
    return !!(VA.last && typeof VA.last.mode === "string" && /^anchor_/.test(VA.last.mode));
  }

  function applyAnchor(redrop) {
    const type = styleType();
    const cmd = {
      type,
      radius_m: parseFloat(arSlider.value),
      hold_heading: holdHdgBox ? holdHdgBox.checked : false,
      vectored: !!(vectoredBox && vectoredBox.checked),
    };
    const last = VA.map.getLastAnchor();
    if (!redrop && last) cmd.anchor = { lat: last.lat, lon: last.lon };
    send(cmd);
  }

  // Seg clicks: update style, persist, re-send only if live.
  if (anchorStyleSeg) anchorStyleSeg.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      anchorStyle = b.dataset.astyle || "classic";
      try { localStorage.setItem(ANCHOR_STYLE_KEY, anchorStyle); } catch (e) { /* ignore */ }
      applyAnchorStyleUI();
      if (isAnchorModeActive()) applyAnchor(false);
    }));

  // ⓘ info sheet (anchor-style-info).
  const anchorInfoBtn = $("anchor-style-info");
  if (anchorInfoBtn) anchorInfoBtn.addEventListener("click", () => {
    if (VA.infoSheet) VA.infoSheet("Anchor styles", [
      "<p><b>Classic</b> — " + ANCHOR_STYLE_DESCS.classic + "</p>",
      "<p><b>Smart</b> — " + ANCHOR_STYLE_DESCS.smart + "</p>",
      "<p><b>Leif</b> — " + ANCHOR_STYLE_DESCS.leif + "</p>",
      "<p style='margin-top:8px;font-size:12px;opacity:.8'>Leif detail: trained for a 5 m watch circle; holds ~2.5–3 m of active wander and NEVER idles, so for tighter radii use Classic or Smart. No PID fallback — an opt-in research mode.</p>",
    ].join(""));
  });

  applyAnchorStyleUI();

  bindSlider("ar", "ar-val");
  // Advanced controls: only re-send if an anchor mode is LIVE.
  if (arSlider) arSlider.addEventListener("change", () => { if (isAnchorModeActive()) applyAnchor(false); });
  if (holdHdgBox) holdHdgBox.addEventListener("change", () => { if (isAnchorModeActive()) applyAnchor(false); });
  if (vectoredBox) vectoredBox.addEventListener("change", () => { if (isAnchorModeActive()) applyAnchor(false); });

  // "Drop anchor here" — the ONLY cold engage; single-tap (owner decision).
  $("anchor-go").addEventListener("click", () => applyAnchor(true));

  // ---- Anchor engaged state (D10) ----
  const anchorEngaged = $("anchor-engaged");
  const aeStatus = $("ae-status");
  const aeRelease = $("ae-release");
  const aeRedrop = $("ae-redrop");
  let _curAnchor = null;  // current server anchor point (from telemetry)
  let _curBoat = null;    // current boat position (from telemetry)

  function _havM(aLat, aLon, bLat, bLon) {
    const R = 6371000, k = Math.PI / 180;
    const dLat = (bLat - aLat) * k, dLon = (bLon - aLon) * k;
    const s = Math.sin(dLat / 2) ** 2
      + Math.cos(aLat * k) * Math.cos(bLat * k) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(s));
  }

  // RELEASE = the same instant STOP as everywhere (mode → manual, thrust 0).
  if (aeRelease) aeRelease.addEventListener("click", () => {
    VA.sendCritical({ type: "stop" });
  });
  // Re-drop moves the hold point to the boat. Undo restores the PREVIOUS
  // point via anchor_hold's anchor:{lat,lon} (the server adopts the point).
  if (aeRedrop) aeRedrop.addEventListener("click", () => {
    const prev = _curAnchor ? { lat: _curAnchor.lat, lon: _curAnchor.lon } : null;
    applyAnchor(true);
    const moved = prev && _curBoat
      ? _havM(prev.lat, prev.lon, _curBoat.lat, _curBoat.lon) : null;
    if (VA.toast && prev) {
      VA.toast("Anchor moved" + (moved !== null ? " " + Math.round(moved) + " m" : ""), {
        actionLabel: "UNDO",
        onAction: () => send({ type: "anchor_hold", anchor: { lat: prev.lat, lon: prev.lon } }),
        ttl: 10000,
      });
    }
  });
  [["jog-fwd", "forward"], ["jog-back", "back"], ["jog-left", "left"], ["jog-right", "right"]]
    .forEach(([id, direction]) => {
      const el = $(id);
      if (el) el.addEventListener("click", () => send({ type: "jog", direction }));
    });

  // ===== passive anchor alarm (motor off, adoption #10) ====================
  const aaRadius = $("aa-radius");
  bindSlider("aa-radius", "aa-radius-val");
  const aaSet = $("aa-set"), aaClear = $("aa-clear"),
        aaRecover = $("aa-recover"), aaStatus = $("aa-status");
  if (aaSet) aaSet.addEventListener("click", () =>
    send({ type: "anchor_alarm_set", radius_m: parseFloat(aaRadius.value) }));
  if (aaClear) aaClear.addEventListener("click", () =>
    send({ type: "anchor_alarm_clear" }));
  if (aaRecover) aaRecover.addEventListener("click", () =>
    send({ type: "anchor_alarm_recover" }));

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
    // Reflect the live anchor mode into the style segmented control.
    if (t.mode === "anchor_ml" || t.mode === "anchor_hold" || t.mode === "anchor_leif") {
      const newStyle = t.mode === "anchor_ml" ? "smart"
        : t.mode === "anchor_leif" ? "leif" : "classic";
      if (newStyle !== anchorStyle) {
        anchorStyle = newStyle;
        applyAnchorStyleUI();
      }
    }

    // ---- Anchor engaged state block ----
    const isAnchored = (t.mode === "anchor_hold" || t.mode === "anchor_ml" || t.mode === "anchor_leif");
    if (anchorEngaged) anchorEngaged.classList.toggle("hidden", !isAnchored);
    const anchorGo = $("anchor-go");
    if (anchorGo) anchorGo.classList.toggle("hidden", isAnchored);
    if (isAnchored && aeStatus) {
      const dist = Number.isFinite(t.distance_to_anchor_m)
        ? " — " + t.distance_to_anchor_m.toFixed(1) + " m from point" : "";
      const circle = Number.isFinite(t.anchor_radius_m)
        ? " · " + Math.round(t.anchor_radius_m) + " m circle" : "";
      const mode = t.mode === "anchor_ml" ? "SMART HOLDING" : t.mode === "anchor_leif" ? "LEIF HOLDING" : "HOLDING";
      aeStatus.textContent = mode + dist + circle;
    }
    // Track anchor + boat position for the re-drop UNDO toast.
    if (t.anchor && Number.isFinite(t.anchor.lat)) _curAnchor = { lat: t.anchor.lat, lon: t.anchor.lon };
    else if (!isAnchored) _curAnchor = null;
    if (t.position && Number.isFinite(t.position.lat)) _curBoat = { lat: t.position.lat, lon: t.position.lon };

    // Passive anchor alarm status + button visibility.
    const aa = t.anchor_alarm || {};
    if (aaSet) aaSet.textContent = aa.armed ? "Move alarm here" : "Set alarm here";
    if (aaClear) aaClear.classList.toggle("hidden", !aa.armed);
    if (aaRecover) aaRecover.classList.toggle("hidden", !aa.armed);
    if (aaStatus) {
      let txt = "";
      if (aa.armed) {
        const d = Number.isFinite(aa.distance_m) ? aa.distance_m.toFixed(0) : "—";
        txt = (aa.firing ? "DRAGGING — " : "watching · ")
            + d + " / " + (aa.radius_m || 0) + " m";
        if (aa.stale) txt += " · GPS stale";
      }
      if (aaStatus.textContent !== txt) aaStatus.textContent = txt;
    }
  });
})();
