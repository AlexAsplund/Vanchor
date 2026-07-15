/* Vanchor-NG — haptic feedback (Vibration API).
 *
 * A short tactile pulse on UI interactions, because on a rocking boat with wet
 * fingers you can't always tell whether a tap registered. One document-level
 * pointerdown listener covers every button-like control (buttons, mode tiles,
 * menu tiles, switch labels, segmented controls), so modules don't need to opt
 * in; destructive/stop controls get a heavier pulse, and safety ALARMS buzz a
 * distinct pattern via the VA.logAlert hook in alerts.js.
 *
 * Support: Android Chrome/PWA vibrates; iOS Safari has no Vibration API, so
 * the whole feature degrades to a no-op there (the Settings toggle says so).
 * Persisted in localStorage ("vanchor-haptics", default ON where supported).
 *
 * Other modules can pulse explicitly with VA.haptic("tap"|"press"|"heavy"|
 * "alert") — e.g. the waypoint long-press menus confirm the 3 s hold with
 * "press" the moment the menu opens.
 */
"use strict";

(function () {
  const VA = (window.VA = window.VA || {});
  const supported = typeof navigator !== "undefined" && "vibrate" in navigator;

  const KEY = "vanchor-haptics";
  let enabled = true;
  try { enabled = localStorage.getItem(KEY) !== "0"; } catch (e) { /* ignore */ }

  // Pulse patterns (ms). Kept short — feedback, not notification.
  const PATTERNS = {
    tap: 10,            // ordinary button press
    press: 25,          // long-press recognized / menu opened
    heavy: [20, 30, 45],// STOP / destructive actions
    alert: [90, 60, 90],// safety alarm logged
  };

  VA.haptic = function (kind) {
    if (!supported || !enabled) return;
    try { navigator.vibrate(PATTERNS[kind] || PATTERNS.tap); } catch (e) { /* ignore */ }
  };

  function setEnabled(on) {
    enabled = !!on;
    try { localStorage.setItem(KEY, enabled ? "1" : "0"); } catch (e) { /* ignore */ }
    if (enabled) VA.haptic("tap");   // confirm the toggle itself
  }

  // ---- global button feedback ---------------------------------------------
  // pointerdown (not click) so the pulse lands the instant the finger touches,
  // and a user gesture is in progress (required for vibrate()). Capture phase
  // so stopPropagation() in a widget's own handler can't starve it.
  const BUTTONISH =
    'button, [role="button"], .mode-btn, .cm-tile, label.switch, .seg > *, a.btn';
  const HEAVY = '[data-mode="stop"], .danger, .btn-stop';
  document.addEventListener("pointerdown", (e) => {
    if (!supported || !enabled) return;
    const el = e.target && e.target.closest ? e.target.closest(BUTTONISH) : null;
    if (!el || el.disabled || el.getAttribute("aria-disabled") === "true") return;
    VA.haptic(el.matches(HEAVY) ? "heavy" : "tap");
  }, true);

  // ---- settings toggle (Display → Appearance) -------------------------------
  const box = document.getElementById("haptics-toggle");
  if (box) {
    box.checked = supported && enabled;
    box.disabled = !supported;
    box.addEventListener("change", () => setEnabled(box.checked));
    const hint = document.getElementById("haptics-hint");
    if (hint && !supported) {
      hint.textContent = "Not supported by this device/browser (e.g. iPhone/iPad — Safari has no vibration API).";
    }
  }
})();
