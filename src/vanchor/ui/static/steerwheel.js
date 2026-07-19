/* Vanchor-NG — manual steering wheel (dual-ring gyro dial).
 *
 * Replaces the manual steering slider. Two concentric frames around a bow-up
 * boat: the OUTER compass card rotates live with the real heading (world
 * frame), the INNER tick ring is the boat frame. ONE draggable handle sets
 * the motor azimuth — its position always reads in both frames at once (the
 * hub shows rel° · true° · thrust). Dragging the handle AROUND the dial sets
 * direction; dragging it OUTWARD sets thrust (radial rings = 25/50/75/100%),
 * so direction + power is a single thumb gesture. A ghost tick on the inner
 * rim shows the ACTUAL head angle from steering feedback.
 *
 * Relative / Absolute / Course (the toggle above the wheel, see controls.js):
 * the dial looks identical in all three — the mode only decides which ring
 * the handle is GLUED to while the boat yaws, and what the server holds.
 * Relative: boat-frame angle (stays put on screen). Absolute: compass bearing
 * (rides the card; `manual {steer_bearing}` holds the head there). Course:
 * compass bearing too, but the server follows the ground-track LINE drawn
 * from the engage position (`manual {steer_course}`, XTE-corrected).
 *
 * Safety: knob-grab (on the knob ± 12 px) gives fine control; dial-grab
 * (anywhere on the face) arms a drag but does NOT send until 6+ viewBox units
 * of movement, so accidental taps never engage the motor. When the boat leaves
 * manual mode (STOP, any autopilot) the wheel zeroes its own state so a later
 * touch can't re-apply a stale command.
 * Snap-to-zero deadman: on pointerup, thrust snaps to 0 unless the HOLD toggle
 * is on. Thrust DECREASES are never ramped. Grace ramp only for INCREASES >0.25.
 */
"use strict";

(function () {
  if (!window.VA || !VA.manualCtl) return;
  const host = document.getElementById("steer-wheel");
  if (!host) return;
  const ctl = VA.manualCtl;

  // Geometry (viewBox units).
  const S = 270, C = 135;
  const R_OUT = 132, R_CARD = 112, R_IN = 100;
  const R_H_MIN = 36, R_H_MAX = 124;     // handle radius ↔ thrust 0..100%
  const KNOB_R = 13;

  const wrap180 = (d) => ((d + 180) % 360 + 360) % 360 - 180;
  const norm360 = (d) => ((d % 360) + 360) % 360;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  // ---- build the dial ------------------------------------------------------
  function ticks(r0, r1, every, stroke, w) {
    let out = "";
    for (let a = 0; a < 360; a += every) {
      out += `<line x1="${C}" y1="${C - r1}" x2="${C}" y2="${C - r0}" stroke="${stroke}" stroke-width="${w}" transform="rotate(${a} ${C} ${C})"/>`;
    }
    return out;
  }
  const thrustRings = [0.25, 0.5, 0.75, 1.0].map((f) =>
    `<circle class="sw-thrust-ring" cx="${C}" cy="${C}" r="${R_H_MIN + (R_H_MAX - R_H_MIN) * f}" fill="none" stroke="#0f1e2e" stroke-width="1"/>`).join("");

  host.innerHTML =
    `<svg viewBox="0 0 ${S} ${S}" class="sw-svg" aria-label="steering wheel">` +
    `<defs><filter id="sw-glow" x="-60%" y="-60%" width="220%" height="220%">` +
    `<feGaussianBlur stdDeviation="2.6" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>` +
    `<radialGradient id="sw-dial" cx="50%" cy="45%">` +
    `<stop offset="0%" stop-color="#0e1a28"/><stop offset="80%" stop-color="#081120"/><stop offset="100%" stop-color="#060c14"/></radialGradient></defs>` +
    // F6: id="sw-face" exposes the dial background to daylight CSS overrides.
    `<circle id="sw-face" cx="${C}" cy="${C}" r="${R_OUT}" fill="url(#sw-dial)" stroke="#16283a" stroke-width="1.5"/>` +
    // OUTER: compass card (rotated by -heading each frame)
    `<g id="sw-card" style="transition: transform 0.18s linear">` +
    ticks(R_CARD + 4, R_OUT - 6, 45, "#7a5c1d", 2) +
    `<text x="${C}" y="${C - R_CARD - 8}" fill="#ffb020" font-size="13" font-weight="700" text-anchor="middle">N</text>` +
    `<text x="${C + R_CARD + 9}" y="${C + 4}" fill="#a58733" font-size="10" text-anchor="middle">E</text>` +
    `<text x="${C}" y="${C + R_CARD + 13}" fill="#a58733" font-size="10" text-anchor="middle">S</text>` +
    `<text x="${C - R_CARD - 9}" y="${C + 4}" fill="#a58733" font-size="10" text-anchor="middle">W</text>` +
    `</g>` +
    // INNER: boat frame ring + ticks wrapped for daylight CSS targeting.
    `<g id="sw-inner-frame">` +
    `<circle cx="${C}" cy="${C}" r="${R_CARD}" fill="none" stroke="#1d3348" stroke-width="1.5"/>` +
    ticks(R_IN - 8, R_IN, 30, "#28425c", 1.5) +
    `</g>` +
    `<text class="sw-tick-lbl" x="${C}" y="${C - R_IN + 16}" fill="#8fa8bd" font-size="9" text-anchor="middle">0°</text>` +
    `<text class="sw-tick-lbl" x="${C + R_IN - 14}" y="${C + 3}" fill="#5d7893" font-size="9" text-anchor="middle">90</text>` +
    `<text class="sw-tick-lbl" x="${C}" y="${C + R_IN - 9}" fill="#5d7893" font-size="9" text-anchor="middle">180</text>` +
    `<text class="sw-tick-lbl" x="${C - R_IN + 14}" y="${C + 3}" fill="#5d7893" font-size="9" text-anchor="middle">90</text>` +
    thrustRings +
    // heading lubber line (bow reference at the top of the card ring)
    `<path d="M ${C - 6} ${C - R_OUT + 2} L ${C + 6} ${C - R_OUT + 2} L ${C} ${C - R_OUT + 13} Z" fill="#e8f4fb"/>` +
    // boat silhouette — class="sw-boat" for daylight fill override
    `<path class="sw-boat" d="M ${C} ${C - 26} C ${C + 9} ${C - 15} ${C + 11} ${C + 1} ${C + 10} ${C + 16} L ${C - 10} ${C + 16} C ${C - 11} ${C + 1} ${C - 9} ${C - 15} ${C} ${C - 26} Z"` +
    ` fill="#14324a" stroke="#2d5b7c" stroke-width="1.2"/>` +
    // ghost tick: ACTUAL head angle (steering feedback)
    `<g id="sw-ghost" opacity="0.55"><line x1="${C}" y1="${C - R_IN}" x2="${C}" y2="${C - R_IN + 14}" stroke="#9fb4c6" stroke-width="3.5" stroke-linecap="round"/></g>` +
    // command spoke + knob (the ONE control)
    `<line id="sw-spoke" x1="${C}" y1="${C}" x2="${C}" y2="${C - R_H_MIN}" stroke="#1be4ff" stroke-width="2.5" opacity="0.55" stroke-dasharray="3,5"/>` +
    `<g id="sw-knob" role="button" aria-label="steering handle" filter="url(#sw-glow)" style="cursor:grab">` +
    `<circle id="sw-knob-c" cx="${C}" cy="${C - R_H_MIN}" r="${KNOB_R}" fill="#0b2a33" stroke="#1be4ff" stroke-width="2.5"/>` +
    `<circle id="sw-knob-dot" cx="${C}" cy="${C - R_H_MIN}" r="3.5" fill="#1be4ff"/>` +
    `</g>` +
    // hub readouts (below the boat, inside the deadzone)
    `<text id="sw-rel" x="${C}" y="${C + 34}" fill="#1be4ff" font-size="13" font-weight="700" text-anchor="middle">0°</text>` +
    `<text id="sw-true" x="${C}" y="${C + 47}" fill="#ffb020" font-size="9" font-weight="600" text-anchor="middle">000° TRUE</text>` +
    `<text id="sw-thr" x="${C}" y="${C - 34}" fill="#27f5b1" font-size="11" font-weight="700" text-anchor="middle">0%</text>` +
    `<text id="sw-deadman" x="${C}" y="${C + 60}" fill="#4a6b7a" font-size="7.5" font-weight="600" text-anchor="middle">RELEASE → 0</text>` +
    `</svg>`;

  const svg = host.querySelector("svg");
  const el = (id) => host.querySelector("#" + id);
  const card = el("sw-card"), ghost = el("sw-ghost"), spoke = el("sw-spoke");
  const knob = el("sw-knob"), knobC = el("sw-knob-c"), knobDot = el("sw-knob-dot");
  const relTxt = el("sw-rel"), trueTxt = el("sw-true"), thrTxt = el("sw-thr");

  let heading = 0;          // live heading from telemetry
  let ghostDeg = null;      // actual head angle (boat frame), from feedback
  let dragging = false;
  let armed = false;        // dial-grab: armed but not yet dragging (< 6 unit move)
  let grabKind = null;      // "knob" | "dial"
  let grabStart = null;     // { dx, dy } at pointerdown for movement threshold
  let sentThrust = 0;       // last commanded thrust (for grace ramp)
  let lastSent = 0;
  let prevMode = null;      // control mode, to zero the wheel on manual exit

  // Handle's boat-frame angle for the current steering mode.
  function screenAngle() {
    // Absolute AND course modes are compass-frame (the handle rides the card).
    return ctl.mode() !== "relative"
      ? wrap180(ctl.state.steerBearing - heading)
      : ctl.state.steerNorm * 180;
  }

  let lastSig = null;
  function render() {
    // Display heading: low-passed + 0.5°-quantized so compass jitter doesn't
    // restart the card's transform transition (and repaint the whole SVG) on
    // every telemetry frame. Command math elsewhere keeps the RAW heading;
    // the same hd drives both the card and the knob so they stay consistent.
    const hd = Math.round(VA.smoothAngle("wheel-heading", heading) * 2) / 2;
    const a = ctl.mode() !== "relative"
      ? wrap180(ctl.state.steerBearing - hd)
      : ctl.state.steerNorm * 180;
    const t = Math.abs(ctl.state.thrust);
    // Repaint gate: telemetry re-renders at 5-10 Hz with mostly-unchanged
    // values; skip the ~13 attribute writes (each an SVG style invalidation)
    // unless something visible moved by at least its display resolution. (perf)
    const sig = `${hd}|${a.toFixed(1)}|${t.toFixed(2)}|` +
      `${ctl.state.thrust < 0}|${ghostDeg === null ? "-" : ghostDeg.toFixed(1)}`;
    if (sig === lastSig) return;
    lastSig = sig;
    card.setAttribute("transform", `rotate(${-hd} ${C} ${C})`);
    const r = R_H_MIN + (R_H_MAX - R_H_MIN) * clamp(t, 0, 1);
    const rad = a * Math.PI / 180;
    const x = C + r * Math.sin(rad), y = C - r * Math.cos(rad);
    knobC.setAttribute("cx", x); knobC.setAttribute("cy", y);
    knobDot.setAttribute("cx", x); knobDot.setAttribute("cy", y);
    spoke.setAttribute("x2", x); spoke.setAttribute("y2", y);
    // Reverse thrust (set via the slider) tints the knob amber.
    const rev = ctl.state.thrust < 0;
    knobC.setAttribute("stroke", rev ? "#ffb020" : "#1be4ff");
    knobDot.setAttribute("fill", rev ? "#ffb020" : "#1be4ff");
    relTxt.textContent = `${a >= 0 ? "+" : ""}${Math.round(a)}°`;
    trueTxt.textContent = `${String(Math.round(norm360(a + hd))).padStart(3, "0")}° TRUE`;
    thrTxt.textContent = `${rev ? "−" : ""}${Math.round(t * 100)}%`;
    if (ghostDeg === null) { ghost.setAttribute("opacity", "0"); }
    else {
      ghost.setAttribute("opacity", "0.55");
      ghost.setAttribute("transform", `rotate(${ghostDeg} ${C} ${C})`);
    }
    const deadman = el("sw-deadman");
    if (deadman) {
      const hold = ctl.holdThrust && ctl.holdThrust();
      const newText = hold ? "HOLD" : "RELEASE → 0";
      if (deadman.textContent !== newText) {
        deadman.textContent = newText;
        deadman.setAttribute("fill", hold ? "#ffb020" : "#4a6b7a");
      }
    }
  }

  // ---- drag: direction (angle) + thrust (radius), knob-start only ----------
  function evPoint(ev) {
    const pt = svg.createSVGPoint();
    pt.x = ev.clientX; pt.y = ev.clientY;
    const p = pt.matrixTransform(svg.getScreenCTM().inverse());
    return { dx: p.x - C, dy: p.y - C };
  }
  svg.addEventListener("pointerdown", (ev) => {
    const { dx, dy } = evPoint(ev);
    const a = screenAngle(), t = clamp(Math.abs(ctl.state.thrust), 0, 1);
    const r = R_H_MIN + (R_H_MAX - R_H_MIN) * t;
    const kx = r * Math.sin(a * Math.PI / 180), ky = -r * Math.cos(a * Math.PI / 180);
    const distToKnob = Math.hypot(dx - kx, dy - ky);
    const distToCenter = Math.hypot(dx, dy);
    // Accept any touch within the dial face (R_OUT).
    if (distToCenter > R_OUT) return;
    ev.preventDefault();
    svg.setPointerCapture(ev.pointerId);
    if (distToKnob <= KNOB_R + 12) {
      // Fine-control knob grab: start dragging immediately.
      grabKind = "knob"; dragging = true; armed = false;
    } else {
      // Dial-grab: arm but wait for movement before sending.
      grabKind = "dial"; dragging = false; armed = true;
    }
    grabStart = { dx, dy };
    lastSent = Date.now();
  });
  svg.addEventListener("pointermove", (ev) => {
    if (!dragging && !armed) return;
    const { dx, dy } = evPoint(ev);
    if (armed && !dragging) {
      // Threshold: 6 viewBox units of movement to start a dial drag.
      if (Math.hypot(dx - grabStart.dx, dy - grabStart.dy) < 6) return;
      dragging = true; armed = false;
    }
    const a = wrap180(Math.atan2(dx, -dy) * 180 / Math.PI);
    // Radius -> thrust, with a snap-to-zero deadzone at the hub.
    let tgt = clamp((Math.hypot(dx, dy) - R_H_MIN) / (R_H_MAX - R_H_MIN), 0, 1);
    if (tgt < 0.05) tgt = 0;
    if (ctl.mode() !== "relative") ctl.state.steerBearing = norm360(a + heading);
    else ctl.state.steerNorm = a / 180;
    // Thrust target (for display): always the user's gesture.
    ctl.setThrust(Math.round(tgt * 100) / 100);
    const now = Date.now();
    if (now - lastSent >= 150) {
      // Grace ramp: slew-limit INCREASES only (decreases are always instant).
      const jump = tgt - sentThrust;
      if (jump > 0.25) {
        sentThrust = Math.min(tgt, sentThrust + 0.25);
      } else {
        sentThrust = tgt;   // instant decrease (or small increase)
      }
      // Override the ctl state with the ramped value for sending.
      const prevThrust = ctl.state.thrust;
      ctl.state.thrust = sentThrust;
      ctl.sendManual();
      ctl.state.thrust = prevThrust;   // restore display value
      lastSent = now;
    }
    render();
  });
  ["pointerup", "pointercancel"].forEach((n) => svg.addEventListener(n, (ev) => {
    if (!dragging && !armed) return;
    dragging = false; armed = false; grabKind = null;
    try { svg.releasePointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
    // Snap-to-zero deadman: thrust drops to 0 on release, unless HOLD is on.
    const hold = ctl.holdThrust && ctl.holdThrust();
    if (!hold) {
      ctl.setThrust(0);
      sentThrust = 0;
    }
    ctl.sendManual();   // final authoritative send (thrust 0 if snap)
    render();
  }));

  // ---- live updates ---------------------------------------------------------
  VA.onTelemetry((t) => {
    if (!t) return;
    if (Number.isFinite(t.heading_deg)) heading = t.heading_deg;
    // Actual head angle: prefer hardware feedback, else the applied command.
    const st = t.steering;
    if (st && Number.isFinite(st.angle_deg)) ghostDeg = wrap180(st.angle_deg);
    else if (t.motor && Number.isFinite(t.motor.steer_angle_deg)) ghostDeg = wrap180(t.motor.steer_angle_deg);
    // Zero our state when the boat stopped WITHOUT us: leaving manual (an
    // autopilot engaged), or STOP — which lands back IN manual with a zeroed
    // motor, so also reset when the server sits at 0/0 while our state is
    // stale (guarded by a quiet window so our own ramping commands, which the
    // governor slews toward, can't trigger it).
    if (!dragging && typeof t.mode === "string") {
      const m = t.motor || {};
      const serverZero = t.mode === "manual" &&
        Math.abs(m.thrust || 0) < 0.01 && Math.abs(m.steering || 0) < 0.01;
      const quiet = Date.now() - (ctl.state.lastSentMs || 0) > 1500;
      const stale = ctl.state.thrust !== 0 || ctl.state.steerNorm !== 0;
      if ((prevMode === "manual" && t.mode !== "manual") ||
          (serverZero && quiet && stale)) {
        ctl.setThrust(0);
        sentThrust = 0;
        ctl.state.steerNorm = 0;
        ctl.state.steerBearing = heading;
      }
      prevMode = t.mode;
    }
    if (!dragging) render();
  });
  ctl.onModeChange(() => render());
  ctl.onStateEdit(() => { if (!dragging) render(); });

  // Update deadman display when HOLD toggle changes.
  const holdToggle = document.getElementById("wheel-hold");
  if (holdToggle) {
    // Restore persisted state.
    try {
      holdToggle.checked = localStorage.getItem("vanchor-wheel-hold") === "true";
    } catch (e) { /* ignore */ }
    holdToggle.addEventListener("change", () => {
      try { localStorage.setItem("vanchor-wheel-hold", holdToggle.checked ? "true" : "false"); } catch (e) { /* ignore */ }
      render();
    });
  }

  render();
})();
