/* Vanchor-NG — PWA install card (WP13, item 39).
 *
 * Populates #install-body in the Data panel with a contextual install prompt:
 *   • Chromium/Edge (Android + Desktop): capture `beforeinstallprompt` and show
 *     a one-tap "Add to home screen" button.
 *   • iOS/iPadOS Safari: static 3-step instructions (Share → Add to Home Screen).
 *   • Already installed (display-mode: standalone): show "Already installed" hint.
 *   • Insecure origin (HTTP in a non-LAN context): amber warning that HTTPS is
 *     needed for the full install experience.
 *
 * The `#install-card` details element only exists after panel-data.html is
 * included, so this file runs after the DOM is ready (placed at end of <body>).
 *
 * No server-side changes — purely client-side detection + DOM construction.
 */
"use strict";

(function () {
  const installBody = document.getElementById("install-body");
  const offlineHint = document.getElementById("install-offline-hint");
  if (!installBody) return;

  // ---- detection -----------------------------------------------------------
  const isStandalone = window.matchMedia("(display-mode: standalone)").matches ||
                       window.navigator.standalone === true;  // iOS legacy prop
  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
  const isSecure = window.isSecureContext;
  // Android/Chromium prompt — captured before this script runs if the user
  // visited earlier; held in VA.installPrompt by the event listener at the end.

  // ---- helper: clear + populate install-body --------------------------------
  function _render(content) {
    installBody.innerHTML = "";
    if (typeof content === "string") {
      installBody.innerHTML = content;
    } else {
      installBody.appendChild(content);
    }
  }

  // ---- case 1: already installed (standalone) ------------------------------
  if (isStandalone) {
    _render('<p class="hint">Vanchor is installed as an app on this device.</p>');
    return;  // nothing more to do
  }

  // ---- case 2: Chromium/Edge — `beforeinstallprompt` available or pending -
  function _showChromiumPrompt(promptEvent) {
    const btn = document.createElement("button");
    btn.className = "btn-primary wide";
    btn.id = "install-prompt-btn";
    btn.textContent = "Add Vanchor to home screen";
    btn.addEventListener("click", function () {
      promptEvent.prompt();
      promptEvent.userChoice.then(function (choice) {
        if (choice.outcome === "accepted") {
          _render('<p class="hint">Vanchor is being added to your home screen.</p>');
        } else {
          btn.textContent = "Add Vanchor to home screen";
          btn.disabled = false;
        }
      });
      btn.disabled = true;
      btn.textContent = "Adding…";
    });
    const wrap = document.createElement("div");
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent =
      "Install Vanchor as an app for faster loading and home-screen access. " +
      "Works offline once installed.";
    wrap.appendChild(hint);
    wrap.appendChild(btn);
    _render(wrap);
  }

  if (VA.installPrompt) {
    _showChromiumPrompt(VA.installPrompt);
    return;
  }

  // ---- case 3: iOS / iPadOS — static steps ---------------------------------
  if (isIOS) {
    _render(
      "<ol class='install-steps'>" +
      "<li>Tap the <b>Share</b> button (the box with an arrow) at the bottom of Safari.</li>" +
      "<li>Scroll down and tap <b>Add to Home Screen</b>.</li>" +
      "<li>Tap <b>Add</b> to confirm.</li>" +
      "</ol>" +
      "<p class='hint'>Once installed, open Vanchor from your home screen for the full-screen experience.</p>"
    );
    return;
  }

  // ---- case 4: insecure context (HTTP, not LAN-exempt) --------------------
  if (!isSecure) {
    if (offlineHint) offlineHint.classList.remove("hidden");
    _render(
      "<p class='hint amber'>Install requires HTTPS. Open " +
      VA.escapeHtml("https://" + location.hostname + ":8443") +
      " and trust the boat's certificate, then come back here to install.</p>"
    );
    return;
  }

  // ---- case 5: browser supports PWA but no prompt yet (waiting) -----------
  _render(
    "<p class='hint'>Vanchor can be installed as an app. " +
    "Open this page in Chrome or Edge on Android or desktop for an install prompt, " +
    "or use Safari on iOS.</p>"
  );

  // If the prompt fires later (user browsed a while), update the card.
  window.addEventListener("va:installprompt", function (ev) {
    _showChromiumPrompt(ev.detail);
  });
})();

// ---- capture beforeinstallprompt early (outside IIFE so it fires before the
// card IIFE runs, even if the event fires during DOMContentLoaded) -----------
(function () {
  VA.installPrompt = null;
  window.addEventListener("beforeinstallprompt", function (ev) {
    ev.preventDefault();  // suppress the browser's native mini-infobar
    VA.installPrompt = ev;
    // Fire a custom event so the card IIFE can update if already rendered.
    window.dispatchEvent(new CustomEvent("va:installprompt", { detail: ev }));
  });
})();
