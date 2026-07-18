/* Vanchor-NG — HUD & status module.
 *
 * Top status-bar instrument chips (connection, GPS fix, speed, heading, depth),
 * the glassy floating HUD (big speed / compass / depth / dist-to-anchor), the
 * safety banner, the live-data readout rows, and the remote-helm status mirror.
 *
 * Compass uses the continuous unwrapped angle so 359°→0° animates the short way.
 */
"use strict";

(function () {
  // ---- connection chip ---------------------------------------------------
  VA.onConnState(function (state, text) {
    const chip = document.getElementById("chip-conn");
    if (chip) chip.dataset.state = state;
    VA.setText("chip-conn-text", text);
  });

  // ---- GPS fix label -----------------------------------------------------
  function fixLabel(t) {
    if (t.has_fix === false) return "NO FIX";
    if (t.safety && t.safety.fix_lost) return "LOST";
    if (Number.isFinite(t.fix_seq) || t.has_fix === true || t.position) return "OK";
    return "—";
  }

  VA.onTelemetry(function renderHud(t) {
    // ---- top status-bar chips ----
    const fix = fixLabel(t);
    VA.setText("chip-fix-val", fix);
    const fixChip = document.getElementById("chip-fix");
    if (fixChip) fixChip.dataset.fix = (fix === "OK") ? "ok" : (fix === "—") ? "none" : "bad";

    const sog = VA.fin(t.sog_knots);
    VA.setText("chip-sog", sog === null ? "—" : sog.toFixed(1));
    const hdg = VA.fin(t.heading_deg);
    // Modulo 0-359: never display "360"
    VA.setText("chip-hdg-val", hdg === null ? "—" : String(((Math.round(hdg) % 360) + 360) % 360));
    const depth = VA.fin(t.depth_m);
    VA.setText("chip-depth-val", depth === null ? "—" : depth.toFixed(1));

    // ---- floating HUD ----
    VA.setText("hud-sog", sog === null ? "—" : sog.toFixed(2));
    VA.setText("hud-ms", sog === null ? "—" : (sog * 0.514444).toFixed(2));
    VA.setText("hud-hdg", hdg === null ? "—" : String(((Math.round(hdg) % 360) + 360) % 360));
    // North-up compass: the needle rotates to point at the heading (N stays up).
    // Quantized to 0.5° so heading jitter doesn't repaint the rose every frame. (perf)
    const rose = document.getElementById("hud-rose");
    if (rose && hdg !== null) {
      const rot = Math.round(VA.smoothAngle("compass-needle", hdg) * 2) / 2;
      if (rot !== renderHud._roseRot) {
        renderHud._roseRot = rot;
        rose.style.transform = `rotate(${rot}deg)`;
      }
    }
    VA.setText("hud-depth", depth === null ? "—" : depth.toFixed(1));
    // Dist-to-anchor: only meaningful when actively anchored
    const anchored = !!t.anchor && typeof t.mode === "string" && t.mode.startsWith("anchor");
    VA.setText("hud-anchor", anchored && Number.isFinite(t.distance_to_anchor_m) ? t.distance_to_anchor_m.toFixed(1) : "—");
    const hudAnchorWidget = document.querySelector('#hud .hud-widget[data-hud="anchor"]');
    if (hudAnchorWidget) hudAnchorWidget.classList.toggle("hidden", !anchored);
    // Live-data settings row: just show "—" when not anchored
    VA.setText("r-anchor", anchored && Number.isFinite(t.distance_to_anchor_m) ? VA.fmt(t.distance_to_anchor_m) + " m" : "—");

    // ---- live-data readout rows (settings drawer) ----
    VA.setText("r-mode", t.mode ?? "—");
    VA.setText("r-heading", VA.fmt(t.heading_deg) + "°");
    VA.setText("r-sog", VA.fmt(t.sog_knots, 2) + " kn");
    // r-anchor is set below, after the anchored check
    VA.setText("r-wp", VA.fmt(t.distance_to_waypoint_m) + " m");
    VA.setText("r-xte", VA.fmt(t.cross_track_m) + " m");
    VA.setText("r-brg", VA.fmt(t.bearing_to_dest) + "°");
    const motor = t.motor || {};
    VA.setText("r-thrust", VA.fmt(motor.thrust, 2));
    VA.setText("r-steer", VA.fmt(motor.steering, 2));

    // sensor rejection counts
    const s = t.sensors || {};
    const hr = Number.isFinite(s.heading_rejected) ? s.heading_rejected : "—";
    const pr = Number.isFinite(s.position_rejected) ? s.position_rejected : "—";
    VA.setText("sensors-rejected", `rejected: hdg ${hr}, pos ${pr}`);

    // last APB (follow-apb panel)
    if (t.last_apb) VA.setText("apb-last", t.last_apb);

    updateRemoteStatus(t);
  });

  // ---- remote-helm status mirror ----------------------------------------
  function updateRemoteStatus(t) {
    const overlay = document.getElementById("remote");
    if (!overlay || overlay.classList.contains("hidden")) return;
    VA.setText("rm-mode", VA.modeName ? VA.modeName(t.mode) : (t.mode ?? "—"));
    VA.setText("rm-hdg", Number.isFinite(t.heading_deg) ? String(((Math.round(t.heading_deg) % 360) + 360) % 360) : "—");
    const anchoredRm = !!t.anchor && typeof t.mode === "string" && t.mode.startsWith("anchor");
    VA.setText("rm-anchor", anchoredRm && Number.isFinite(t.distance_to_anchor_m) ? t.distance_to_anchor_m.toFixed(1) : "—");
    VA.setText("rm-depth", Number.isFinite(t.depth_m) ? t.depth_m.toFixed(1) : "—");
  }
})();
