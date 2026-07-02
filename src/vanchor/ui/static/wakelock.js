/* Vanchor-NG — Screen Wake Lock module.
 *
 * Acquires a Screen Wake Lock while a motor-engaged mode is active so the
 * helm control surface doesn't sleep on the water.  Releases the lock when
 * the boat is idle (mode = "stop", or manual with zero thrust) to save
 * battery when merely monitoring.
 *
 * Uses the Screen Wake Lock API (navigator.wakeLock.request("screen")).
 * Feature-detected silently; non-secure contexts (HTTP) and older browsers
 * (iOS Safari < 16.4) simply skip the whole module.
 *
 * Wake locks are automatically released by the browser when the tab goes
 * hidden; we re-acquire on visibilitychange if still in an engaged state.
 */
"use strict";

(function () {
  // ---- feature detection ------------------------------------------------
  if (typeof navigator === "undefined" || !navigator.wakeLock) {
    console.debug("[wakelock] Screen Wake Lock API not available — skipping.");
    return;
  }

  // ---- engaged-mode predicate -------------------------------------------
  // Returns true when the motor is actively driving the boat.
  //
  //   stop        → never engaged (always release)
  //   manual      → engaged only if thrust is nonzero; if thrust unknown,
  //                 treat as engaged (safe default = keep screen awake)
  //   any other mode (anchor_hold, anchor_ml, heading_hold, waypoint,
  //                   follow_apb, drift, contour_follow, orbit, trolling,
  //                   work_area, …) → always engaged
  function isEngaged(t) {
    const mode = t && t.mode;
    if (!mode || mode === "stop") return false;
    if (mode === "manual") {
      const thrust = t.motor && t.motor.thrust;
      // If thrust is a finite number equal to zero, the motor is idle.
      if (Number.isFinite(thrust) && thrust === 0) return false;
      // Thrust nonzero, or unavailable → keep awake (safe default).
      return true;
    }
    // All other guided/cruising modes actively drive the motor.
    return true;
  }

  // ---- wake lock state --------------------------------------------------
  let sentinel = null;   // WakeLockSentinel while held; null when released
  let wantLock = false;  // desired state (tracks across visibility changes)

  async function acquire() {
    if (sentinel) return;                             // already held
    if (document.visibilityState !== "visible") return; // browser rejects hidden
    try {
      sentinel = await navigator.wakeLock.request("screen");
      sentinel.addEventListener("release", function () {
        // Browser auto-releases on hide; clear our reference so acquire()
        // can be called again when the tab becomes visible.
        sentinel = null;
      });
      console.debug("[wakelock] acquired");
    } catch (err) {
      // Rejected when document is hidden, permission denied, or device refuses.
      console.debug("[wakelock] request() rejected:", err.message);
    }
  }

  async function release() {
    if (!sentinel) return;
    try { await sentinel.release(); } catch (_) { /* ignore */ }
    sentinel = null;
    console.debug("[wakelock] released");
  }

  // ---- telemetry subscription -------------------------------------------
  VA.onTelemetry(function (t) {
    const engaged = isEngaged(t);
    if (engaged === wantLock) return;  // no state change — nothing to do
    wantLock = engaged;
    if (engaged) {
      acquire();
    } else {
      release();
    }
  });

  // ---- re-acquire after tab becomes visible again -----------------------
  // The browser automatically drops the lock when the tab is hidden.
  // This listener restores it when the user returns to the tab.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible" && wantLock && !sentinel) {
      acquire();
    }
  });
})();
