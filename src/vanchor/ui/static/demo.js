/* Vanchor-NG — demo-mode badge (adoption pack).
 *
 * Shows a fixed "DEMO" pill (id=demo-indicator, markup in index.html) whenever
 * telemetry carries demo_mode:true — i.e. the server was started with
 * `vanchor --demo`. Appends "· read-only" when demo_readonly is set. Pure
 * display; never sends commands.
 */
"use strict";

(function () {
  let shown = null;
  let ro = null;
  VA.onTelemetry(function renderDemo(t) {
    const on = !!t.demo_mode;
    const roOn = !!t.demo_readonly;
    if (on === shown && roOn === ro) return;
    shown = on; ro = roOn;
    const el = document.getElementById("demo-indicator");
    if (el) el.classList.toggle("hidden", !on);
    const suffix = document.getElementById("demo-ro-suffix");
    if (suffix) suffix.classList.toggle("hidden", !roOn);
  });
})();
