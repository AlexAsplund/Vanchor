/* Vanchor-NG — remote helm + boot module.
 *
 * The full-screen remote-helm overlay (stepped thrust/steer, jog pad, quick
 * anchor / stop / hold-heading actions), and the final boot step that opens the
 * telemetry WebSocket once every control module has registered its handlers.
 *
 * This file must load LAST: VA.connect() here kicks off telemetry, so all the
 * onTelemetry subscribers in the other modules must already be in place.
 */
"use strict";

(function () {
  const { $, send } = VA.ui;

  // ===== remote helm =======================================================
  let rmThrust = 0, rmSteer = 0;
  function clampUnit(v) { return Math.max(-1, Math.min(1, v)); }
  function rmUpdateManualState() { VA.setText("rm-manual-state", `thr ${rmThrust.toFixed(1)} · str ${rmSteer.toFixed(1)}`); }
  function rmSendManual() { send({ type: "manual", thrust: rmThrust, steering: rmSteer }); rmUpdateManualState(); }
  function setRemote(on) {
    const overlay = $("remote");
    if (overlay) overlay.classList.toggle("hidden", !on);
    const btn = $("remote-toggle");
    if (btn) btn.classList.toggle("active", on);
  }
  const remoteToggle = $("remote-toggle");
  if (remoteToggle) remoteToggle.addEventListener("click", () => setRemote(true));
  const rmExit = $("rm-exit");
  if (rmExit) rmExit.addEventListener("click", () => setRemote(false));
  const rmBind = (id, fn) => { const el = $(id); if (el) el.addEventListener("click", fn); };
  rmBind("rm-anchor-here", () => send({ type: "anchor_hold", radius_m: 5 }));
  rmBind("rm-stop", () => { rmThrust = 0; rmSteer = 0; rmUpdateManualState(); VA.sendCritical({ type: "stop" }); });
  [["rm-jog-fwd", "forward"], ["rm-jog-back", "back"], ["rm-jog-left", "left"], ["rm-jog-right", "right"]]
    .forEach(([id, direction]) => rmBind(id, () => send({ type: "jog", direction })));
  rmBind("rm-thr-up", () => { rmThrust = clampUnit(rmThrust + 0.2); rmSendManual(); });
  rmBind("rm-thr-dn", () => { rmThrust = clampUnit(rmThrust - 0.2); rmSendManual(); });
  rmBind("rm-str-l", () => { rmSteer = clampUnit(rmSteer - 0.2); rmSendManual(); });
  rmBind("rm-str-r", () => { rmSteer = clampUnit(rmSteer + 0.2); rmSendManual(); });
  rmUpdateManualState();

  // ===== boot ==============================================================
  // Open the telemetry socket last, after every module's onTelemetry handler is
  // registered (this file is loaded after the other split control modules).
  VA.connect();
})();
