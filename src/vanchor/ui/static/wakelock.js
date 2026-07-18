/* Vanchor-NG — Screen keep-awake module.
 *
 * Keeps the screen awake while a motor-engaged mode is active so the helm
 * control surface doesn't sleep on the water; releases when the boat is idle
 * (mode = "stop", or manual with zero thrust) to save battery when merely
 * monitoring.
 *
 * Two mechanisms, best-first:
 *   1. Screen Wake Lock API (navigator.wakeLock) — secure contexts only.
 *   2. Fallback for plain-HTTP LAN deployments (the normal on-boat case,
 *      where the API is unavailable): play a tiny muted inline video
 *      (vendored from NoSleep.js, MIT — see vendor/nosleep/LICENSE). Mobile
 *      browsers hold the screen awake while a video is playing.
 *
 * Neither mechanism can prevent a deliberate power-button lock — that is the
 * link-loss deadman's job (manual driving stops 20 s after the last client).
 */
"use strict";

(function () {
  if (!window.VA || typeof document === "undefined") return;

  const hasApi = typeof navigator !== "undefined" && !!navigator.wakeLock;

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
      if (Number.isFinite(thrust) && thrust === 0) return false;
      return true;
    }
    return true;
  }

  // ---- mechanism 1: Wake Lock API ----------------------------------------
  let sentinel = null;   // WakeLockSentinel while held; null when released

  async function apiAcquire() {
    if (sentinel) return;
    if (document.visibilityState !== "visible") return; // browser rejects hidden
    try {
      sentinel = await navigator.wakeLock.request("screen");
      sentinel.addEventListener("release", function () { sentinel = null; });
      console.debug("[wakelock] acquired (api)");
      if (VA.rum) VA.rum("wakelock", "acquired (api)");
    } catch (err) {
      console.debug("[wakelock] request() rejected:", err.message);
      videoAcquire();  // e.g. permissions policy / battery saver -> fall back
    }
  }

  async function apiRelease() {
    if (!sentinel) return;
    try { await sentinel.release(); } catch (_) { /* ignore */ }
    sentinel = null;
    console.debug("[wakelock] released (api)");
  }

  // ---- mechanism 2: muted-video fallback (NoSleep technique) -------------
  // A tiny silent video, played DETACHED (never appended to the DOM — that is
  // how NoSleep.js does it and it reliably counts as playback). loop is off;
  // a timeupdate handler seeks back near the start (the iOS-proof variant).
  let video = null;

  function makeVideo() {
    const v = document.createElement("video");
    v.setAttribute("muted", "");
    v.setAttribute("playsinline", "");
    v.muted = true;
    v.playsInline = true;
    v.setAttribute("title", "keep-awake");
    const webm = document.createElement("source");
    webm.src = "/static/vendor/nosleep/nosleep.webm";
    webm.type = "video/webm";
    const mp4 = document.createElement("source");
    mp4.src = "/static/vendor/nosleep/nosleep.mp4";
    mp4.type = "video/mp4";
    v.appendChild(webm);
    v.appendChild(mp4);
    v.addEventListener("timeupdate", function () {
      if (v.currentTime > 0.5) v.currentTime = Math.random() * 0.4;
    });
    return v;
  }

  function videoAcquire() {
    if (!video) video = makeVideo();
    if (!video.paused && !video.ended) return;          // already playing
    const p = video.play();
    if (p && p.then) {
      p.then(function () {
        console.debug("[wakelock] acquired (video fallback)");
        if (VA.rum) VA.rum("wakelock", "acquired (video fallback)");
      })
       .catch(function (err) { console.debug("[wakelock] video play rejected:", err && err.message); });
    }
  }

  function videoRelease() {
    if (video && !video.paused) {
      video.pause();
      console.debug("[wakelock] released (video fallback)");
    }
  }

  // ---- unified engage/release ---------------------------------------------
  let wantLock = false;  // desired state (tracks across visibility changes)

  // ---- user pref (localStorage, default "on") ----------------------------
  const PREF_KEY = "vanchor-wakelock";
  let userEnabled = localStorage.getItem(PREF_KEY) !== "off";  // default on

  function engage() {
    if (!userEnabled) return;
    if (hasApi) apiAcquire(); else videoAcquire();
  }
  function disengage() { apiRelease(); videoRelease(); }

  if (!hasApi) {
    console.debug("[wakelock] Wake Lock API unavailable (insecure context?) — using video fallback.");
  }

  // ---- public API ---------------------------------------------------------
  VA.wakelock = {
    active: function () { return wantLock && userEnabled; },
    available: hasApi || true,   // video fallback is always available
    setEnabled: function (bool) {
      userEnabled = !!bool;
      localStorage.setItem(PREF_KEY, userEnabled ? "on" : "off");
      if (!userEnabled) { disengage(); } else if (wantLock) { engage(); }
      _updateWakeStatus();
    },
  };

  // ---- wake-status text update --------------------------------------------
  function _updateWakeStatus() {
    const statusEl = document.getElementById("wake-status");
    const toggle   = document.getElementById("wake-toggle");
    if (!statusEl) return;
    if (!userEnabled) {
      statusEl.textContent = "Screen keep-awake is off.";
    } else if (!hasApi) {
      statusEl.textContent = "Using video fallback (HTTP context — no Wake Lock API).";
    } else {
      statusEl.textContent = "Screen will stay awake while a mode is driving.";
    }
    if (toggle) toggle.checked = userEnabled;
  }

  // ---- wire #wake-toggle --------------------------------------------------
  const wakeToggle = document.getElementById("wake-toggle");
  if (wakeToggle) {
    wakeToggle.checked = userEnabled;
    wakeToggle.addEventListener("change", function () {
      VA.wakelock.setEnabled(this.checked);
    });
  }

  // Initialize status text on load.
  _updateWakeStatus();

  // ---- telemetry subscription -------------------------------------------
  VA.onTelemetry(function (t) {
    const engaged = isEngaged(t);
    if (engaged === wantLock) return;  // no state change — nothing to do
    wantLock = engaged;
    if (engaged) engage(); else disengage();
  });

  // ---- re-engage after the tab becomes visible again ----------------------
  // The browser drops the API lock (and may pause the video) when the tab is
  // hidden; restore whichever mechanism is in use when the user returns.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible" && wantLock) engage();
  });
})();
