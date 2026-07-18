/* Vanchor-NG — onboarding & sim honesty (WP3 + WP10, task 3).
 *
 * Owns: sim-notice dialog, coach marks, Get-started menu tile, tablet Helm
 * suggestion. Loads last (after views.js, wizard.js, hwwizard.js).
 *
 * localStorage keys:
 *   "vanchor-sim-ack"     = "1"  — this device acked the simulation notice.
 *   "vanchor-coach-grip"  = "1"  — grip coach mark shown/dismissed.
 *   "vanchor-coach-boat"  = "1"  — boat-marker pulse shown.
 *   "vanchor-coach-helm"  = "1"  — tablet Helm suggestion shown.
 *   "vanchor-wizard-done" = "1"  — instant-paint cache of prefs wizard_done.
 *
 * Prefs KV (server, /api/prefs):
 *   "onboarding" → { "wizard_done": bool }
 *
 * No motor commands; pure display + navigation to settings.
 */
"use strict";

(function () {
  /* ---- helpers ---- */
  function ls(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } }
  function $(id) { return document.getElementById(id); }
  function show(el) { if (el) el.classList.remove("hidden"); }
  function hide(el) { if (el) el.classList.add("hidden"); }

  /* ---- 1. Sim notice dialog ---- */

  let _simDialogOpen = false;
  let _simAutoShownThisSession = false;

  function closeSimNotice() {
    hide($("firstrun"));
    hide($("firstrun-scrim"));
    _simDialogOpen = false;
  }

  function openSimNotice() {
    const dialog = $("firstrun");
    const scrim  = $("firstrun-scrim");
    if (!dialog) return;

    // Hide "Set up my real boat" in read-only demo mode.
    const realBtn = $("firstrun-real");
    if (realBtn) {
      const ro = window.VA && VA.last && VA.last.demo_readonly;
      realBtn.classList.toggle("hidden", !!ro);
    }

    show(dialog); show(scrim);
    _simDialogOpen = true;
  }

  // Expose on VA so demo.js can call it from the pill tap.
  const VA = (window.VA = window.VA || {});
  VA.openSimNotice = openSimNotice;

  // "Set up my real boat" — ack + open hardware wizard.
  const realBtn = $("firstrun-real");
  if (realBtn) {
    realBtn.addEventListener("click", function () {
      lsSet("vanchor-sim-ack", "1");
      closeSimNotice();
      if (VA.openHwWizard) VA.openHwWizard();
    });
  }

  // "Play with the simulator" — ack + close.
  const simBtn = $("firstrun-sim");
  if (simBtn) {
    simBtn.addEventListener("click", function () {
      lsSet("vanchor-sim-ack", "1");
      closeSimNotice();
      // Trigger a re-render in demo.js so the pill compacts immediately.
      // demo.js re-reads ack on every telemetry frame; no action needed.
    });
  }

  // Scrim tap / Esc → close WITHOUT acking (pill stays full sentence).
  const scrimEl = $("firstrun-scrim");
  if (scrimEl) {
    scrimEl.addEventListener("click", function () {
      if (_simDialogOpen) closeSimNotice();
    });
  }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && _simDialogOpen) closeSimNotice();
  });

  // Auto-open once per session when sim_enabled and not yet acked and not read-only demo.
  VA.onTelemetry(function onboardSimCheck(t) {
    if (_simAutoShownThisSession) return;
    if (!t.sim_enabled) return;
    if (t.demo_readonly) return;
    if (ls("vanchor-sim-ack") === "1") return;
    _simAutoShownThisSession = true;
    openSimNotice();
  });

  /* ---- 2. Get-started tile ---- */

  // Pull server prefs once to check wizard_done (mirrors views.js pullServer).
  // Also use the local cache for instant-paint.
  let _wizardDone = (ls("vanchor-wizard-done") === "1");

  function _applyGetStarted() {
    const tile = $("cm-get-started");
    if (!tile) return;
    tile.classList.toggle("hidden", _wizardDone);
  }

  fetch("/api/prefs")
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (p) {
      if (p && p.onboarding && p.onboarding.wizard_done) {
        _wizardDone = true;
        lsSet("vanchor-wizard-done", "1");
      }
      _applyGetStarted();
    })
    .catch(function () { /* best-effort */ });

  _applyGetStarted();

  // Get-started tile click: close menu, open Boat-setup wizard.
  const gsTile = $("cm-get-started");
  if (gsTile) {
    gsTile.addEventListener("click", function () {
      // Close the settings menu (same path VA.showChart() uses).
      const closeBtn = $("settings-close");
      if (closeBtn) closeBtn.click();
      if (VA.openWizard) VA.openWizard();
    });
  }

  // Mark wizard complete on finish (extend wiz-finish button handler).
  // wizard.js already calls close() on wiz-finish; we piggyback.
  const wizFinish = $("wiz-finish");
  if (wizFinish) {
    wizFinish.addEventListener("click", function () {
      lsSet("vanchor-wizard-done", "1");
      _wizardDone = true;
      hide($("cm-get-started"));
      // PUT to server (whole onboarding object — shallow merge is safe for siblings).
      fetch("/api/prefs", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ onboarding: { wizard_done: true } }),
      }).catch(function () { /* best-effort */ });
    });
  }

  /* ---- 3. Coach marks ---- */

  // --- 3a. Grip coach bubble (mobile, after sim dialog is closed) ---
  function _showGripBubble() {
    if (!document.body.classList.contains("mobile")) return;
    if (ls("vanchor-coach-grip") === "1") return;
    const grip = $("sheet-grip");
    if (!grip) return;

    // Remove any existing bubble first.
    const existing = document.getElementById("coach-grip");
    if (existing) existing.remove();

    const bubble = document.createElement("div");
    bubble.id = "coach-grip";
    bubble.className = "coach-bubble";
    bubble.innerHTML = '<span class="coach-chev" aria-hidden="true">⌃</span> Swipe up for controls';
    document.body.appendChild(bubble);

    // Position above #sheet-grip.
    function _positionBubble() {
      const r = grip.getBoundingClientRect();
      bubble.style.left = (r.left + r.width / 2) + "px";
      bubble.style.top  = (r.top - 12) + "px";
    }
    _positionBubble();
    window.addEventListener("resize", _positionBubble);

    // Dismiss on tap.
    bubble.addEventListener("click", function () {
      lsSet("vanchor-coach-grip", "1");
      bubble.remove();
    });

    // Dismiss on first sheet-state change (swipe/tap).
    const obs = new MutationObserver(function () {
      lsSet("vanchor-coach-grip", "1");
      bubble.remove();
      obs.disconnect();
    });
    obs.observe(document.body, { attributes: true, attributeFilter: ["data-sheet"] });
  }

  // Show grip bubble after the sim dialog is dismissed (either via ack or scrim-tap).
  // We watch for the dialog becoming hidden while on mobile.
  (function () {
    const dialog = $("firstrun");
    if (!dialog) return;
    let _wasOpen = false;
    const obs = new MutationObserver(function () {
      const nowHidden = dialog.classList.contains("hidden");
      if (!_wasOpen && !nowHidden) { _wasOpen = true; }
      else if (_wasOpen && nowHidden) {
        _wasOpen = false;
        // Small delay so the dialog animation clears.
        setTimeout(_showGripBubble, 400);
      }
    });
    obs.observe(dialog, { attributes: true, attributeFilter: ["class"] });
  })();

  // Also show on load if sim ack already done (returning user, mobile).
  if (ls("vanchor-sim-ack") === "1" && document.body.classList.contains("mobile")) {
    setTimeout(_showGripBubble, 1000);
  }

  // --- 3b. Boat-marker pulse ---
  function _showBoatPulse() {
    if (ls("vanchor-coach-boat") === "1") return;
    // Find the boat-icon element (rendered by map-boat.js after first telemetry).
    const el = document.querySelector(".boat-icon");
    if (!el) return;
    lsSet("vanchor-coach-boat", "1");
    el.classList.add("coach-pulse");
    // Remove class after 3 animation iterations (~6 s at 2s each).
    setTimeout(function () { el.classList.remove("coach-pulse"); }, 6200);
  }

  // Show boat pulse on first sim telemetry (once the map is live).
  (function () {
    let _done = false;
    VA.onTelemetry(function _boatPulseCheck(t) {
      if (_done || !t.sim_enabled) return;
      if (document.body.dataset.view !== "chart" && document.body.dataset.view !== undefined
          && document.body.dataset.view !== "") {
        // Only pulse on chart view.
        if (document.body.dataset.view !== "chart") return;
      }
      _done = true;
      setTimeout(_showBoatPulse, 2000);
    });
  })();

  // --- 3c. Tablet Helm suggestion ---
  (function () {
    if (ls("vanchor-coach-helm") === "1") return;
    if (window.innerWidth < 1000) return;

    // Check that no view has been persisted yet.
    const lsViews = ls("vanchor-views");
    if (lsViews) return;  // already has a persisted view — don't suggest

    // Wait for server prefs to arrive (pullServer runs in views.js).
    // We can't block here, so we poll with a one-shot timeout.
    setTimeout(function () {
      if (ls("vanchor-coach-helm") === "1") return;
      // If a view has been persisted since page-load, don't show.
      if (ls("vanchor-views")) return;

      const switcher = $("view-switcher");
      if (!switcher) return;

      const bubble = document.createElement("div");
      bubble.id = "coach-helm";
      bubble.className = "coach-bubble coach-bubble-helm";
      bubble.textContent = "Tip: HELM view — big buttons for the console.";
      document.body.appendChild(bubble);

      function _posHelmBubble() {
        const r = switcher.getBoundingClientRect();
        bubble.style.left = (r.left + r.width / 2) + "px";
        bubble.style.top  = (r.bottom + 10) + "px";
      }
      _posHelmBubble();
      window.addEventListener("resize", _posHelmBubble);

      function _dismissHelm() {
        lsSet("vanchor-coach-helm", "1");
        bubble.remove();
      }

      bubble.addEventListener("click", _dismissHelm);

      // Dismiss on any view switch.
      document.querySelectorAll("#view-switcher .view-chip").forEach(function (btn) {
        btn.addEventListener("click", _dismissHelm, { once: true });
      });
    }, 1500);
  })();

})();
