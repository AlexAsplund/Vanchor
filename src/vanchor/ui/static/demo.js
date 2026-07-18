/* Vanchor-NG — simulation pill (WP3, task 3).
 *
 * Shows a fixed amber "SIMULATION — not your motor" pill (id=sim-indicator,
 * markup in index.html #map-pills) whenever telemetry carries sim_enabled:true.
 *
 * States:
 *   sim_enabled=false           → pill hidden.
 *   sim_enabled=true, no ack    → pill visible, full sentence.
 *   sim_enabled=true, acked     → pill visible, compact "SIMULATION".
 *   demo_readonly (any state)   → appends "· read-only" suffix.
 *
 * Tap → VA.openSimNotice() (defined in onboard.js).
 * No motor commands. Per-device ack via localStorage "vanchor-sim-ack".
 */
"use strict";

(function () {
  let _simShown = null;
  let _ro = null;
  let _acked = null;

  function _currentAck() {
    try { return localStorage.getItem("vanchor-sim-ack") === "1"; } catch (e) { return false; }
  }

  VA.onTelemetry(function renderDemo(t) {
    const simOn = !!t.sim_enabled;
    const roOn = !!t.demo_readonly;
    const acked = _currentAck();

    if (simOn === _simShown && roOn === _ro && acked === _acked) return;
    _simShown = simOn; _ro = roOn; _acked = acked;

    const el = document.getElementById("sim-indicator");
    if (!el) return;

    el.classList.toggle("hidden", !simOn);

    if (simOn) {
      const txt = document.getElementById("sim-indicator-text");
      if (txt) txt.textContent = acked ? "SIMULATION" : "SIMULATION — not your motor";
    }

    const suffix = document.getElementById("demo-ro-suffix");
    if (suffix) suffix.classList.toggle("hidden", !roOn);
  });

  // Wire the pill tap → open the sim notice dialog.
  const el = document.getElementById("sim-indicator");
  if (el) {
    el.addEventListener("click", function () {
      if (typeof VA !== "undefined" && VA.openSimNotice) VA.openSimNotice();
    });
  }
})();
