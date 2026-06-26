/* Vanchor-NG — GPS offset calibration (task #45, UI half).
 *
 * Settings → "Adjust GPS position": arms a map click and prompts the user to
 * tap their TRUE location; on tap sends `set_gps_offset {true_lat, true_lon}`.
 * The applied offset is shown from telemetry `gps_offset {dlat,dlon,active}`,
 * with a Reset (→ `clear_gps_offset`).
 *
 * Degrades gracefully: if the backend lacks the commands the controls still
 * arm/send, but the offset readout simply stays "—" with no telemetry.
 */
"use strict";

(function () {
  if (!window.VA || !VA.map) return;
  const $ = (id) => document.getElementById(id);

  const adjustBtn = $("gpscal-adjust");
  const resetBtn = $("gpscal-reset");
  const statusEl = $("gpscal-status");
  if (!adjustBtn) return;

  let armed = false;

  function setStatus(msg, kind) {
    if (!statusEl) return;
    statusEl.textContent = msg || "";
    statusEl.className = "hint" + (kind ? " " + kind : "");
  }

  function setArmed(on) {
    armed = !!on;
    adjustBtn.classList.toggle("active", armed);
    adjustBtn.textContent = armed ? "Tap your true location… (cancel)" : "📍 Adjust GPS position";
    const mapEl = $("map");
    if (mapEl) mapEl.classList.toggle("goto-arming", armed);
    if (armed) setStatus("Tap the map where the boat actually is.", "busy");
  }

  // Layer onto the map-click pipeline: consume the click only while armed so it
  // never fights the route / go-to / marker handlers.
  if (VA.map.addClickConsumer) {
    VA.map.addClickConsumer((lat, lon) => {
      if (!armed) return false;
      VA.send({ type: "set_gps_offset", true_lat: lat, true_lon: lon });
      setArmed(false);
      setStatus(`Offset set to true location ${VA.fmt(lat, 5)}, ${VA.fmt(lon, 5)}.`, "ok");
      return true;
    });
  }

  adjustBtn.addEventListener("click", () => setArmed(!armed));
  if (resetBtn) resetBtn.addEventListener("click", () => {
    VA.send({ type: "clear_gps_offset" });
    setStatus("Offset cleared.", "");
  });

  // ---- reflect telemetry gps_offset -------------------------------------
  VA.onTelemetry(function (t) {
    const o = t.gps_offset;
    const badge = $("gpscal-state");
    if (!o || typeof o !== "object") {
      VA.setText("gpscal-dlat", "—");
      VA.setText("gpscal-dlon", "—");
      if (badge) badge.textContent = "";
      return;
    }
    const active = !!o.active && (Number.isFinite(o.dlat) || Number.isFinite(o.dlon));
    VA.setText("gpscal-dlat", Number.isFinite(o.dlat) ? o.dlat.toFixed(6) + "°" : "—");
    VA.setText("gpscal-dlon", Number.isFinite(o.dlon) ? o.dlon.toFixed(6) + "°" : "—");
    if (badge) badge.textContent = active ? "● active" : "";
  });
})();
