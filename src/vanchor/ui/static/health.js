/* Vanchor-NG — degraded-health banners + sensor-age diagnostics.
 *
 * Subscribes to telemetry.health and surfaces four degraded states:
 *
 *  - controller_fault (non-null)  → red alarm banner, top of stack (most serious)
 *  - heading_stale                → red alarm banner
 *  - fix_lost                     → red alarm banner
 *  - depth_stale                  → amber warn banner (less prominent)
 *
 * All four banners are dynamically created inside #safety-banners and toggled
 * .hidden as the flags change; they stack vertically in the severity order
 * listed above, above the existing safety.js banners.  Reuses the existing
 * .sbanner / .sbanner-alarm / .sbanner-warn styling — no new visual language.
 *
 * Sensor-age chips (fix / heading / depth / imu) are rendered into static
 * elements added to the "Live data" card in the settings drawer; they update
 * every telemetry frame but cause no layout shift.
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);

  // ---- banner factory -------------------------------------------------------
  // Build a minimal .sbanner element (id, CSS modifier class, default message).
  // For the fault banner the message is set dynamically each frame.
  function makeBannerEl(id, cls, msg) {
    const el = document.createElement("div");
    el.id = id;
    el.className = "sbanner " + cls + " hidden";
    el.setAttribute("role", "alert");
    const msgEl = document.createElement("span");
    msgEl.className = "sb-msg";
    msgEl.id = id + "-msg";
    if (msg) msgEl.textContent = msg;
    el.appendChild(msgEl);
    return el;
  }

  // Banner IDs (stable so DOM lookups are cheap each frame).
  const FAULT_ID      = "health-fault-banner";
  const HDG_STALE_ID  = "health-hdg-stale-banner";
  const FIX_LOST_ID   = "health-fix-lost-banner";
  const DEPTH_STALE_ID = "health-depth-stale-banner";

  const faultBanner      = makeBannerEl(FAULT_ID,       "sbanner-alarm", "");
  const hdgStaleBanner   = makeBannerEl(HDG_STALE_ID,   "sbanner-alarm", "COMPASS STALE — coasting");
  const fixLostBanner    = makeBannerEl(FIX_LOST_ID,    "sbanner-alarm", "GPS LOST");
  const depthStaleBanner = makeBannerEl(DEPTH_STALE_ID, "sbanner-warn",  "depth stale");

  // Insert into #safety-banners at the TOP, in severity order.
  // insertBefore(x, firstChild) reverses insertion order, so we insert
  // least-severe first so most-severe ends up visually first.
  const container = $("safety-banners");
  if (container) {
    const anchor = container.firstChild || null;
    container.insertBefore(depthStaleBanner, anchor);
    container.insertBefore(fixLostBanner,    depthStaleBanner);
    container.insertBefore(hdgStaleBanner,   fixLostBanner);
    container.insertBefore(faultBanner,      hdgStaleBanner);
  }

  function setBanner(el, show) {
    if (el) el.classList.toggle("hidden", !show);
  }

  // ---- telemetry subscriber ------------------------------------------------
  // Edge-triggered logging (VA.logAlert de-duplication handles the rest);
  // banner show/hide is driven directly by the flag each frame.
  let prevFault      = undefined;   // undefined = never seen
  let prevHdgStale   = false;
  let prevFixLost    = false;
  let prevDepthStale = false;

  VA.onTelemetry(function (t) {
    const h = (t && t.health) || null;
    if (!h) return;

    // ---- controller_fault ------------------------------------------------
    // Non-null string means the motor controller reported a problem; the value
    // is a short human-readable reason string from the backend.
    const fault = (h.controller_fault != null) ? String(h.controller_fault) : null;
    if (fault !== prevFault) {
      prevFault = fault;
      if (fault) {
        const msgEl = $(FAULT_ID + "-msg");
        if (msgEl) msgEl.textContent = "CONTROL FAULT — motor zeroed: " + fault;
        setBanner(faultBanner, true);
        if (VA.logAlert) VA.logAlert("alarm", "Control fault: " + fault, { level: "medium" });
      } else {
        setBanner(faultBanner, false);
      }
    }

    // ---- heading_stale ---------------------------------------------------
    const hdgStale = !!h.heading_stale;
    if (hdgStale !== prevHdgStale) {
      prevHdgStale = hdgStale;
      setBanner(hdgStaleBanner, hdgStale);
      if (hdgStale && VA.logAlert) VA.logAlert("alarm", "Compass stale — coasting on last heading", { level: "medium" });
    }

    // ---- fix_lost --------------------------------------------------------
    const fixLost = !!h.fix_lost;
    if (fixLost !== prevFixLost) {
      prevFixLost = fixLost;
      setBanner(fixLostBanner, fixLost);
      if (fixLost && VA.logAlert) VA.logAlert("alarm", "GPS fix lost", { level: "high" });
    }

    // ---- depth_stale -----------------------------------------------------
    const depthStale = !!h.depth_stale;
    if (depthStale !== prevDepthStale) {
      prevDepthStale = depthStale;
      setBanner(depthStaleBanner, depthStale);
      if (depthStale && VA.logAlert) VA.logAlert("warn", "Depth sensor stale", { kind: "depth" });
    }

    // ---- sensor age chips ------------------------------------------------
    renderAges(h);

    // ---- master status dot: worst-of health (A19) -------------------------
    // alarm if any critical flag; warn if any degraded flag; never green if any alarm.
    const t_full = t || {};
    const s = t_full.safety || {};
    const link = t_full.link || {};
    const bLvl = VA.battLevel ? VA.battLevel((t_full.battery || {}).soc_pct) : "ok";
    const isAlarm =
      !!(s.drag_alarm) ||
      !!(t_full.anchor_alarm && t_full.anchor_alarm.firing) ||
      !!h.fix_lost ||
      !!(h.controller_fault) ||
      !!(link.failsafe_engaged) ||
      !!(s.shallow_stop) ||
      !!(s.nogo_stop) ||
      bLvl === "crit";
    const isWarn =
      !!h.heading_stale ||
      !!h.depth_stale ||
      bLvl === "low" ||
      !link.client_connected ||
      !!document.body.dataset.stale;
    const overall = isAlarm ? "alarm" : isWarn ? "warn" : "ok";
    const connChip = $("chip-conn");
    if (connChip) connChip.dataset.health = overall;
  });

  // ---- sensor age display --------------------------------------------------
  // Formats a raw age in seconds to a compact string, or "—" when null/never.
  function fmtAge(v) {
    if (v === null || v === undefined) return "—";
    const s = Number(v);
    if (!Number.isFinite(s)) return "—";
    if (s < 10) return s.toFixed(1) + "s";
    return Math.round(s) + "s";
  }

  function renderAges(h) {
    VA.setText("health-age-fix",   fmtAge(h.fix_age_s));
    VA.setText("health-age-hdg",   fmtAge(h.heading_age_s));
    VA.setText("health-age-depth", fmtAge(h.depth_age_s));
    VA.setText("health-age-imu",   fmtAge(h.imu_age_s));
  }
})();
