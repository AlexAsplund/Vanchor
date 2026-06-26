/* Vanchor-NG — Teleport boat (task #90, SIM only).
 *
 * Settings → Simulator card: "Teleport boat" arms a map tap, and the next tap
 * sends `{type:"teleport", lat, lon}` to instantly relocate the simulated boat
 * (backend moves ground truth + resets velocity). A coords entry (lat/lon + Go)
 * is offered as an alternative. The current heading is carried along when known.
 *
 * Gating: the whole control lives inside #sim-card, which app.js already
 * shows only when telemetry.sim_enabled is true. As a belt-and-braces guard
 * we also disable the controls (and cancel any arming) whenever sim_enabled is
 * false, so on real hardware the command can never be armed/sent.
 *
 * Mirrors the existing armed-click pattern (gpscal.js / routing.js): consume
 * the map tap only while armed so it never fights go-to / markers / routing.
 * Degrades gracefully — if the backend ignores the command it is simply a
 * no-op on the boat.
 */
"use strict";

(function () {
  if (!window.VA || !VA.map) return;
  const $ = (id) => document.getElementById(id);

  const pickBtn = $("teleport-pick");
  const latIn = $("teleport-lat");
  const lonIn = $("teleport-lon");
  const goBtn = $("teleport-go");
  const statusEl = $("teleport-status");
  if (!pickBtn) return;

  let armed = false;
  let simOn = !!(VA.last && VA.last.sim_enabled);

  function setStatus(msg, kind) {
    if (!statusEl) return;
    statusEl.textContent = msg || "";
    statusEl.className = "hint" + (kind ? " " + kind : "");
  }

  function setArmed(on) {
    armed = !!on && simOn;
    pickBtn.classList.toggle("active", armed);
    pickBtn.textContent = armed ? "Tap map to teleport… (cancel)" : "🛰 Teleport boat";
    const mapEl = $("map");
    if (mapEl) mapEl.classList.toggle("goto-arming", armed);
    if (armed) setStatus("Tap the map to drop the boat there.", "busy");
  }

  // Carry the current heading along when telemetry knows it (else omit).
  function teleport(lat, lon) {
    if (!simOn) { setStatus("Teleport is simulation-only.", "err"); return false; }
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      setStatus("Need a valid latitude and longitude.", "err");
      return false;
    }
    const cmd = { type: "teleport", lat: lat, lon: lon };
    const hdg = VA.last && Number.isFinite(VA.last.heading_deg) ? VA.last.heading_deg : null;
    if (hdg !== null) cmd.heading = hdg;
    VA.send(cmd);
    setStatus(`Teleported to ${VA.fmt(lat, 5)}, ${VA.fmt(lon, 5)}.`, "ok");
    return true;
  }

  // Layer onto the map-click pipeline: consume the tap only while armed.
  if (VA.map.addClickConsumer) {
    VA.map.addClickConsumer((lat, lon) => {
      if (!armed) return false;
      teleport(lat, lon);
      if (latIn) latIn.value = VA.fmt(lat, 6);
      if (lonIn) lonIn.value = VA.fmt(lon, 6);
      setArmed(false);
      return true;
    });
  }

  pickBtn.addEventListener("click", () => setArmed(!armed));

  if (goBtn) goBtn.addEventListener("click", () => {
    const lat = latIn ? parseFloat(latIn.value) : NaN;
    const lon = lonIn ? parseFloat(lonIn.value) : NaN;
    if (teleport(lat, lon)) setArmed(false);
  });
  // Enter in either coord input triggers Go.
  [latIn, lonIn].forEach((el) => {
    if (!el) return;
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); if (goBtn) goBtn.click(); } });
  });

  // Belt-and-braces gate on sim_enabled (app.js already hides the whole card).
  function applyGate() {
    pickBtn.disabled = !simOn;
    if (goBtn) goBtn.disabled = !simOn;
    if (latIn) latIn.disabled = !simOn;
    if (lonIn) lonIn.disabled = !simOn;
    if (!simOn && armed) setArmed(false);
  }
  applyGate();

  VA.onTelemetry(function (t) {
    const on = !!(t && t.sim_enabled);
    if (on !== simOn) { simOn = on; applyGate(); }
  });
})();
