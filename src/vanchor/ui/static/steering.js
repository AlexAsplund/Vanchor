/* Vanchor-NG — steering gauge module.
 *
 * A semicircular gauge visualising the closed-loop steering unit:
 *   • commanded angle (ghost needle, from steering.target_deg / commanded)
 *   • feedback angle  (solid glowing needle, from steering.angle_deg)
 *   • cable-wrap limit arc spanning ±range_deg/2, with a fill that grows toward
 *     the wrapped end and turns amber→red as steering.wrap_pct approaches ±100.
 *
 * The gauge maps the steering range onto a 180° arc (top half), 0° pointing up.
 * Reads everything defensively; missing fields show "—" and centre the needles.
 */
"use strict";

(function () {
  const CX = 100, CY = 100, R = 70;     // gauge geometry (matches viewBox 200x130)
  const ARC_DEG = 160;                  // visual sweep used for the gauge (±80°)

  // polar → cartesian on the gauge. `deg` is the *gauge* angle where 0 = up,
  // positive = clockwise (starboard), matching steering sign convention.
  function pt(deg, r) {
    const a = (deg - 90) * Math.PI / 180; // -90 so 0° points up
    return [CX + r * Math.cos(a), CY + r * Math.sin(a)];
  }
  function arcPath(fromDeg, toDeg, r) {
    const [x1, y1] = pt(fromDeg, r);
    const [x2, y2] = pt(toDeg, r);
    const large = Math.abs(toDeg - fromDeg) > 180 ? 1 : 0;
    const sweep = toDeg > fromDeg ? 1 : 0;
    return `M ${x1.toFixed(2)} ${y1.toFixed(2)} A ${r} ${r} 0 ${large} ${sweep} ${x2.toFixed(2)} ${y2.toFixed(2)}`;
  }

  // Build the static range track + tick marks once (range can change, so we
  // also refresh them when range_deg changes).
  let builtRange = null;
  function buildTicks(rangeDeg) {
    const ticks = document.getElementById("sg-ticks");
    if (!ticks) return;
    ticks.innerHTML = "";
    // Five ticks across the visual sweep: -half .. +half.
    for (let i = 0; i <= 4; i++) {
      const g = -ARC_DEG / 2 + (ARC_DEG * i) / 4;
      const [x1, y1] = pt(g, R + 2);
      const [x2, y2] = pt(g, R + 9);
      const ln = document.createElementNS("http://www.w3.org/2000/svg", "line");
      ln.setAttribute("x1", x1.toFixed(2)); ln.setAttribute("y1", y1.toFixed(2));
      ln.setAttribute("x2", x2.toFixed(2)); ln.setAttribute("y2", y2.toFixed(2));
      ln.setAttribute("class", i === 2 ? "sg-tick sg-tick-center" : "sg-tick");
      ticks.appendChild(ln);
    }
    const track = document.getElementById("sg-track");
    if (track) track.setAttribute("d", arcPath(-ARC_DEG / 2, ARC_DEG / 2, R));
    builtRange = rangeDeg;
  }

  // Map a physical steering angle (deg) to the gauge sweep, clamped to ±half.
  function toGauge(angleDeg, rangeDeg) {
    const half = (rangeDeg || 90) / 2;
    const clamped = Math.max(-half, Math.min(half, angleDeg));
    return (clamped / half) * (ARC_DEG / 2);
  }

  function setNeedle(id, gaugeDeg) {
    const el = document.getElementById(id);
    if (!el) return;
    const [x, y] = pt(gaugeDeg, R - 6);
    el.setAttribute("x2", x.toFixed(2));
    el.setAttribute("y2", y.toFixed(2));
  }

  VA.onTelemetry(function renderSteering(t) {
    const s = t.steering || {};
    const motor = t.motor || {};
    // range for the gauge: prefer steering.range_deg, fall back to boat profile.
    const rangeDeg = Number.isFinite(s.range_deg) ? s.range_deg
      : (t.boat && Number.isFinite(t.boat.steer_range_deg) ? t.boat.steer_range_deg : 90);
    if (rangeDeg !== builtRange) buildTicks(rangeDeg);

    // Commanded angle: prefer target_deg; else derive from commanded(-1..1).
    let cmdDeg = null;
    if (Number.isFinite(s.target_deg)) cmdDeg = s.target_deg;
    else if (Number.isFinite(s.commanded)) cmdDeg = s.commanded * (rangeDeg / 2);
    else if (Number.isFinite(motor.steer_angle_deg)) cmdDeg = motor.steer_angle_deg;

    // Feedback angle (actual measured shaft position).
    const fbDeg = Number.isFinite(s.angle_deg) ? s.angle_deg
      : (Number.isFinite(motor.steer_angle_deg) ? motor.steer_angle_deg : null);

    setNeedle("sg-cmd", cmdDeg === null ? 0 : toGauge(cmdDeg, rangeDeg));
    setNeedle("sg-angle", fbDeg === null ? 0 : toGauge(fbDeg, rangeDeg));

    VA.setText("sg-cmd-val", cmdDeg === null ? "—" : Math.round(cmdDeg) + "°");
    VA.setText("sg-angle-val", fbDeg === null ? "—" : Math.round(fbDeg) + "°");

    // Cable-wrap arc: fill grows from centre toward the wrapped side; color
    // escalates as |wrap_pct| → 100.
    const wrap = Number.isFinite(s.wrap_pct) ? Math.max(-100, Math.min(100, s.wrap_pct)) : null;
    const wrapEl = document.getElementById("sg-wrap");
    if (wrapEl) {
      if (wrap === null) {
        wrapEl.setAttribute("d", "");
      } else {
        const end = (wrap / 100) * (ARC_DEG / 2);
        wrapEl.setAttribute("d", arcPath(0, end, R));
        const mag = Math.abs(wrap);
        const cls = mag > 85 ? "danger" : mag > 60 ? "warn" : "";
        wrapEl.setAttribute("class", "sg-wrap " + cls);
      }
    }
    VA.setText("sg-wrap-val", wrap === null ? "—" : Math.round(wrap) + "%");
    const wrapStat = document.querySelector(".sg-wrap-stat");
    if (wrapStat) {
      const mag = wrap === null ? 0 : Math.abs(wrap);
      wrapStat.dataset.level = mag > 85 ? "danger" : mag > 60 ? "warn" : "ok";
    }

    // feedback OK pill
    const fb = document.getElementById("sg-feedback");
    if (fb) {
      if (s.feedback_ok === false) { fb.textContent = "NO FB"; fb.dataset.ok = "bad"; }
      else if (s.feedback_ok === true) { fb.textContent = "FB OK"; fb.dataset.ok = "ok"; }
      else { fb.textContent = ""; fb.dataset.ok = "none"; }
    }
  });
})();
